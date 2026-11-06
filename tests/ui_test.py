"""UI smoke test: builds the real window against the mock mailbox.

Run: python tests/ui_test.py
A window is created and destroyed; it needs a desktop session.
"""

from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from outlook_client.gui import MailWindow
from outlook_client.imap_session import ImapSession
from outlook_client.mock_imap import MockImapServer

CHECKS: list[tuple[str, bool]] = []


def check(label: str, condition: bool) -> None:
    CHECKS.append((label, bool(condition)))
    print(("  ok   " if condition else "  FAIL ") + label)


def pump(app: MailWindow, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.02)


def main() -> int:
    with MockImapServer() as server:
        session = ImapSession(
            server.host, "mock@local", port=server.port, use_tls=False, password="mock"
        )
        session.connect()
        app = MailWindow(session, "mock@local")
        try:
            pump(app, 2.0)

            rows = app.listbox.get_children()
            check("four messages listed", len(rows) == 4)
            check("first row auto-selected", app.listbox.selection()[:1] == (rows[0],))
            check("status bar reports the folder", "4 items" in app.status.cget("text"))

            pump(app, 1.0)
            body = app.body.get("1.0", "end")
            check("reading pane shows the body", "Run your payroll today" in body)
            check("reading pane shows the subject", "Payroll is late" in app.subject_label.cget("text"))
            check("reading pane shows the sender", "Gusto" in app.meta_label.cget("text"))

            app.search_var.set("QBO")
            pump(app, 0.6)
            check("search filters the list", len(app.listbox.get_children()) == 1)

            app.search_var.set("")
            pump(app, 0.6)
            check("clearing search restores the list", len(app.listbox.get_children()) == 4)

            app.folders.selection_set("Sent Items")
            app._on_folder()
            pump(app, 1.2)
            check("switching folder reloads", len(app.listbox.get_children()) == 1)

            app.folders.selection_set("INBOX")
            app._on_folder()
            pump(app, 1.2)

            app.search_var.set("melanie")
            pump(app, 0.6)
            app.listbox.selection_set(app.listbox.get_children()[0])
            pump(app, 1.0)
            body = app.body.get("1.0", "end")
            check("html body is converted to text", "didn't slip through" in body)
            check("html tags stripped", "<p>" not in body and "&mdash;" not in body)
        finally:
            app.destroy()
            session.close()

    failed = [label for label, ok in CHECKS if not ok]
    print()
    if failed:
        print(f"FAILED {len(failed)}/{len(CHECKS)}")
        return 1
    print(f"PASS {len(CHECKS)}/{len(CHECKS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
