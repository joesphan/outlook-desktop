"""Minimal IMAP client used by the TUI.

Stdlib only. Supports:
  - Microsoft (outlook.com / M365): SASL XOAUTH2, because the service advertises
    LOGINDISABLED and no longer accepts basic auth or app passwords.
  - Google: AUTH=PLAIN via app password, or XOAUTH2 if a token is supplied.
"""

from __future__ import annotations

import email
import html as html_lib
import imaplib
import re
import ssl
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime


class AuthError(RuntimeError):
    pass


@dataclass
class MessageSummary:
    uid: str
    subject: str
    sender: str
    date: str
    unread: bool


def _decode(value: str | bytes | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _strip_tags(subject: str) -> str:
    # OWA prefixes the in-list ad row with an "Ad" label; a real message never has it.
    return re.sub(r"^\s*\[?ad\]?\s*", "", subject, flags=re.IGNORECASE)


class ImapSession:
    def __init__(
        self,
        host: str,
        user: str,
        *,
        port: int = 993,
        use_tls: bool = True,
        token: str | None = None,
        password: str | None = None,
    ) -> None:
        self.host = host
        self.user = user
        self.port = port
        self.use_tls = use_tls
        self.token = token
        self.password = password
        self._imap: imaplib.IMAP4 | None = None
        self.capabilities: list[str] = []

    # -- connection ---------------------------------------------------------
    def connect(self) -> None:
        if self.use_tls:
            ctx = ssl.create_default_context()
            self._imap = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ctx)
        else:
            self._imap = imaplib.IMAP4(self.host, self.port)

        self.capabilities = [str(c) for c in self._imap.capabilities]
        self._authenticate()

    def _authenticate(self) -> None:
        assert self._imap is not None
        if self.token:
            try:
                self._imap.authenticate("XOAUTH2", self._xoauth2)
            except imaplib.IMAP4.error as exc:
                raise AuthError(f"XOAUTH2 rejected for {self.user}: {exc}") from exc
            return

        if self.password:
            if "LOGINDISABLED" in self.capabilities:
                raise AuthError(
                    "server advertises LOGINDISABLED: basic auth is off, an OAuth token is required"
                )
            try:
                self._imap.login(self.user, self.password)
            except imaplib.IMAP4.error as exc:
                raise AuthError(f"login rejected for {self.user}: {exc}") from exc
            return

        raise AuthError("no credential supplied (need token or password)")

    def _xoauth2(self, _challenge: bytes | None = None) -> bytes:
        return f"user={self.user}\x01auth=Bearer {self.token}\x01\x01".encode()

    def close(self) -> None:
        if self._imap is not None:
            try:
                self._imap.logout()
            except Exception:
                pass
            self._imap = None

    # -- operations ---------------------------------------------------------
    def folders(self) -> list[str]:
        assert self._imap is not None
        status, data = self._imap.list()
        if status != "OK":
            return []
        out: list[str] = []
        for raw in data or []:
            if raw is None:
                continue
            line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            # imaplib hands back: (\HasNoChildren) "/" "Sent Items"
            match = re.match(r'^\([^)]*\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+?)\s*$', line)
            if not match:
                continue
            name = match.group("name")
            if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
                name = name[1:-1]
            if name:
                out.append(name)
        return out or ["INBOX"]

    def select(self, mailbox: str = "INBOX") -> int:
        assert self._imap is not None
        # imaplib does not quote arguments; names with spaces (Sent Items,
        # [Gmail]/All Mail) must be quoted or the server sees extra tokens.
        arg = mailbox
        if any(ch in mailbox for ch in ' "()[]'):
            arg = '"' + mailbox.replace('"', '\\"') + '"'
        status, data = self._imap.select(arg, readonly=True)
        if status != "OK":
            raise RuntimeError(f"cannot select {mailbox}: {data}")
        try:
            return int(data[0])
        except (TypeError, ValueError, IndexError):
            return 0

    def messages(self, limit: int = 50) -> list[MessageSummary]:
        assert self._imap is not None
        status, data = self._imap.uid("search", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()
        uids = uids[-limit:][::-1]

        out: list[MessageSummary] = []
        for uid in uids:
            fields = "(FLAGS BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])"
            status, parts = self._imap.uid("fetch", uid, fields)
            if status != "OK" or not parts:
                continue
            flags = b""
            header = b""
            for part in parts:
                if isinstance(part, tuple):
                    flags += part[0]
                    header += part[1]
            text = header.decode(errors="replace")
            subject = _decode(_header_value(text, "Subject"))
            sender = _decode(_header_value(text, "From"))
            date_raw = _header_value(text, "Date")
            out.append(
                MessageSummary(
                    uid=uid.decode(),
                    subject=_strip_tags(subject),
                    sender=sender,
                    date=_short_date(date_raw),
                    unread=b"\\Seen" not in flags,
                )
            )
        return out

    def body_text(self, uid: str) -> str:
        """Fetch the whole message and return the best plain-text rendering."""
        assert self._imap is not None
        status, parts = self._imap.uid("fetch", uid, "(BODY.PEEK[])")
        if status != "OK" or not parts:
            return ""
        raw = b"".join(part[1] for part in parts if isinstance(part, tuple))
        if not raw:
            return ""
        return _extract_text(email.message_from_bytes(raw))


_BLOCK_TAGS = re.compile(r"(?i)</?(p|div|br|tr|li|h[1-6]|table|blockquote)\b[^>]*>")
_DROP_TAGS = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_ANY_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t]{2,}")


def _html_to_text(markup: str) -> str:
    text = _DROP_TAGS.sub("", markup)
    text = _BLOCK_TAGS.sub("\n", text)
    text = _ANY_TAG.sub("", text)
    text = html_lib.unescape(text)
    lines = [_SPACES.sub(" ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line:
            out.append(line)
        elif out and out[-1] != "":
            out.append("")
    return "\n".join(out).strip()


def _extract_text(message: email.message.Message) -> str:
    plain: tuple[bytes, str] | None = None
    markup: tuple[bytes, str] | None = None

    parts = list(message.walk()) if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        if ctype == "text/plain" and plain is None:
            plain = (payload, charset)
        elif ctype == "text/html" and markup is None:
            markup = (payload, charset)

    if plain is not None:
        return plain[0].decode(plain[1], errors="replace").strip()
    if markup is not None:
        return _html_to_text(markup[0].decode(markup[1], errors="replace"))
    return ""


def _header_value(block: str, name: str) -> str:
    for line in block.splitlines():
        if line.lower().startswith(name.lower() + ":"):
            return line.split(":", 1)[1].strip()
    return ""


def probe_capabilities(host: str, port: int = 993) -> list[str]:
    """Open TLS and return the server's CAPABILITY list without authenticating."""
    context = ssl.create_default_context()
    imap = imaplib.IMAP4_SSL(host, port, ssl_context=context)
    try:
        return [str(capability) for capability in imap.capabilities]
    finally:
        try:
            imap.shutdown()
        except Exception:
            pass


def _short_date(raw: str) -> str:
    try:
        return parsedate_to_datetime(raw).strftime("%m-%d %H:%M")
    except Exception:
        return raw[:16]
