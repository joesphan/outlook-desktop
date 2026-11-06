"""Entry point: python -m outlook_client [--mock] [--account NAME] [--setup] [--screenshot PATH]"""

from __future__ import annotations

import argparse
import sys
import webbrowser

from .auth import DeviceCodeAuth
from .config import Account, default_config_path, load_config, resolve_config_path
from .gui import MailWindow, ask_for_account
from .imap_session import AuthError, ImapSession


def build_session(account: Account) -> ImapSession:
    if account.provider == "microsoft":
        if not account.client_id:
            raise AuthError("this Microsoft account has no application (client) ID configured")
        token = DeviceCodeAuth(account.client_id, account.user, account.tenant).acquire(
            on_code=_show_code
        )
        session = ImapSession(account.host, account.user, port=account.port, token=token)
    else:
        if not account.app_password:
            raise AuthError("this account has no password configured")
        session = ImapSession(
            account.host, account.user, port=account.port, password=account.app_password
        )
    session.connect()
    return session


def _show_code(blob: dict) -> None:
    uri = blob.get("verification_uri", "https://microsoft.com/devicelogin")
    code = blob.get("user_code", "?")
    print(f"\n  Sign in: {uri}")
    print(f"  Code:    {code}\n")
    try:
        webbrowser.open(uri)
    except Exception:
        pass


def pick(accounts: list[Account], selector: str | None) -> Account:
    if selector:
        wanted = selector.lower()
        for account in accounts:
            if wanted in (account.name.lower(), account.user.lower()):
                return account
        raise ValueError(f"no account matching {selector!r}")
    return accounts[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="outlook_client", description="Desktop mail client with a three-pane window."
    )
    parser.add_argument("--config", help="path to config.json")
    parser.add_argument("--account", help="account name or address")
    parser.add_argument("--setup", action="store_true", help="add an account in a dialog, then exit")
    parser.add_argument("--mock", action="store_true", help="open the window with a canned mailbox")
    parser.add_argument("--screenshot", help="save a PNG of the window, then exit")
    args = parser.parse_args(argv)

    if args.setup:
        saved = ask_for_account(None, resolve_config_path(args.config) or default_config_path())
        print(f"saved: {saved.user}" if saved else "cancelled")
        return 0 if saved else 1

    if args.mock:
        from .mock_imap import MockImapServer

        server = MockImapServer().start()
        session = ImapSession(
            server.host, "mock@local", port=server.port, use_tls=False, password="mock"
        )
        try:
            session.connect()
            MailWindow(session, "mock@local", screenshot=args.screenshot).mainloop()
        finally:
            session.close()
            server.stop()
        return 0

    path = resolve_config_path(args.config) or default_config_path()
    try:
        accounts = load_config(args.config)
    except FileNotFoundError:
        print("No accounts yet - opening the add-account dialog.", file=sys.stderr)
        created = ask_for_account(None, path)
        if not created:
            return 1
        accounts = [created]
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        account = pick(accounts, args.account)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        session = build_session(account)
    except AuthError as exc:
        print(f"auth: {exc}", file=sys.stderr)
        return 3
    except (OSError, RuntimeError) as exc:
        print(f"connect: {exc}", file=sys.stderr)
        return 4

    try:
        MailWindow(session, account.user, screenshot=args.screenshot).mainloop()
    except KeyboardInterrupt:
        return 130
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
