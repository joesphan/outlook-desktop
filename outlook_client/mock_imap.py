"""In-process IMAP server used by --mock and the smoke test.

It serves a small canned mailbox over plain TCP so the client, the parser and the
TUI can be exercised end to end without touching a real account. Not a general
IMAP implementation: only the commands this client issues.
"""

from __future__ import annotations

import socket
import threading

MAILBOXES: dict[str, list[dict]] = {
    "INBOX": [
        {
            "uid": 5,
            "subject": "Payroll is late for ROCKET EDA",
            "sender": "Gusto <no-reply@gusto.com>",
            "date": "Thu, 6 Nov 2026 04:50:00 +0000",
            "flags": "",
            "body": "Hi Joesphan,\n\nRun your payroll today 1 day late every 8th of the month.\n\n— Gusto",
        },
        {
            "uid": 4,
            "subject": "This only happens once a year - don't miss out!",
            "sender": "Melanie Arzouman <m@example.com>",
            "date": "Thu, 6 Nov 2026 09:54:00 +0000",
            "flags": "\\Seen",
            "content_type": "text/html; charset=utf-8",
            "body": (
                "<html><body><p>Hi Joesphan,</p>"
                "<p>Making sure my previous email <b>didn't slip through</b>.</p>"
                "<p>&mdash; Melanie</p></body></html>"
            ),
        },
        {
            "uid": 3,
            "subject": "EDA Q1 2026 QBO items need to verify",
            "sender": "JUN QIAN <jun@example.com>",
            "date": "Thu, 6 Nov 2026 01:10:00 +0000",
            "flags": "\\Seen",
            "body": "Please verify the items before the close.",
        },
        {
            "uid": 2,
            "subject": "Order status updated: ORD-CB86F6092F3E7E",
            "sender": "Racing Custom Parts <orders@example.com>",
            "date": "Wed, 5 Nov 2026 02:11:00 +0000",
            "flags": "\\Seen",
            "body": "Your order status has changed.",
        },
    ],
    "Sent Items": [
        {
            "uid": 1,
            "subject": "Re: PCB revision",
            "sender": "Joesphan Lu <joe@example.com>",
            "date": "Tue, 4 Nov 2026 18:02:00 +0000",
            "flags": "\\Seen",
            "body": "Attaching the revised stack-up.",
        }
    ],
    "Archive": [],
}


class MockImapServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.listen(5)
        self.host, self.port = self._sock.getsockname()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "MockImapServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "MockImapServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    # -- per-connection -----------------------------------------------------
    def _handle(self, conn: socket.socket) -> None:
        current = "INBOX"
        try:
            conn.sendall(b"* OK Mock IMAP4 service ready\r\n")
            buffer = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buffer += chunk
                while b"\r\n" in buffer:
                    line, buffer = buffer.split(b"\r\n", 1)
                    if not line.strip():
                        continue
                    if not self._dispatch(conn, line.decode(errors="replace"), current):
                        return
                    current = self._current
        except (ConnectionResetError, OSError):
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    _current = "INBOX"

    def _dispatch(self, conn: socket.socket, line: str, current: str) -> bool:
        parts = line.split()
        if len(parts) < 2:
            return True
        tag, verb, args = parts[0], parts[1].upper(), parts[2:]

        def send(text: str) -> None:
            conn.sendall(text.encode())

        if verb == "CAPABILITY":
            send("* CAPABILITY IMAP4REV1 AUTH=PLAIN UIDPLUS\r\n")
            send(f"{tag} OK CAPABILITY completed\r\n")
        elif verb == "LOGIN":
            send(f"{tag} OK LOGIN completed\r\n")
        elif verb == "NOOP":
            send(f"{tag} OK NOOP completed\r\n")
        elif verb == "LIST":
            for name in MAILBOXES:
                send(f'* LIST (\\HasNoChildren) "/" "{name}"\r\n')
            send(f"{tag} OK LIST completed\r\n")
        elif verb in ("SELECT", "EXAMINE"):
            raw_name = " ".join(args).strip()
            name = raw_name.strip('"') if raw_name else "INBOX"
            # imaplib may send a literal length for the mailbox name; ignore that case.
            if not name.isdigit():
                current = name
            self._current = current
            messages = MAILBOXES.get(current, [])
            send(f"* {len(messages)} EXISTS\r\n")
            send("* 0 RECENT\r\n")
            send("* FLAGS (\\Seen \\Answered)\r\n")
            send("* OK [UIDVALIDITY 1] UIDs valid\r\n")
            send(f"{tag} OK [READ-ONLY] SELECT completed\r\n")
        elif verb == "UID":
            sub = args[0].upper() if args else ""
            if sub == "SEARCH":
                messages = sorted(MAILBOXES.get(current, []), key=lambda m: m["uid"])
                uids = " ".join(str(m["uid"]) for m in messages)
                send(f"* SEARCH {uids}\r\n")
                send(f"{tag} OK SEARCH completed\r\n")
            elif sub == "FETCH":
                self._fetch(conn, tag, current, args)
            else:
                send(f"{tag} BAD unsupported UID subcommand\r\n")
        elif verb == "LOGOUT":
            send("* BYE logging out\r\n")
            send(f"{tag} OK LOGOUT completed\r\n")
            return False
        else:
            send(f"{tag} BAD unsupported command {verb}\r\n")
        return True

    def _fetch(self, conn: socket.socket, tag: str, current: str, args: list[str]) -> None:
        messages = MAILBOXES.get(current, [])
        try:
            uid = int(args[1])
        except (IndexError, ValueError):
            conn.sendall(f"{tag} BAD bad uid\r\n".encode())
            return
        record = next((m for m in messages if m["uid"] == uid), None)
        if record is None:
            conn.sendall(f"{tag} OK FETCH completed\r\n".encode())
            return

        query = " ".join(args[2:]).upper()
        conn.sendall(f"* 1 FETCH (UID {uid} FLAGS ({record['flags']}) ".encode())

        content_type = record.get("content_type", "text/plain; charset=utf-8")
        header_block = (
            f"Subject: {record['subject']}\r\n"
            f"From: {record['sender']}\r\n"
            f"Date: {record['date']}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"MIME-Version: 1.0\r\n\r\n"
        )

        if "BODY.PEEK[]" in query or "BODY[]" in query:
            label, payload = "BODY[]", (header_block + record["body"]).encode()
        elif "[TEXT]" in query:
            label, payload = "BODY[TEXT]", record["body"].encode()
        else:
            label, payload = "BODY[HEADER.FIELDS (SUBJECT FROM DATE)]", (
                f"Subject: {record['subject']}\r\n"
                f"From: {record['sender']}\r\n"
                f"Date: {record['date']}\r\n\r\n"
            ).encode()

        conn.sendall(label.encode() + b" {" + str(len(payload)).encode() + b"}\r\n")
        conn.sendall(payload)
        conn.sendall(b")\r\n")
        conn.sendall(f"{tag} OK FETCH completed\r\n".encode())
