"""Three-pane desktop mail window (tkinter, no external dependencies).

Shape follows the familiar mail client: command bar, folder tree, message list,
reading pane, status bar. There is no ad slot in this client because nothing
renders one — the layout has no space allocated for it.

Microsoft artwork, icons and fonts are not included; only the conventional
three-pane arrangement is reproduced.
"""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

# Match physical pixels so window geometry and screen grabs agree on scaled displays.
if sys.platform == "win32":
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

from .config import Account, default_config_path, load_config, write_config
from .imap_session import ImapSession, MessageSummary
from .setup_detect import guess_provider, registration_hint

def display_name(sender: str) -> str:
    """'Gusto <no-reply@gusto.com>' -> 'Gusto'."""
    name = sender.split("<", 1)[0].strip().strip('"').strip()
    return name or sender.strip("<> ").strip()


BG = "#faf9f8"
SIDE_BG = "#f3f2f1"
TOOLBAR_BG = "#ffffff"


class MailWindow(tk.Tk):
    def __init__(
        self,
        session: ImapSession,
        account_label: str,
        *,
        screenshot: str | None = None,
    ) -> None:
        super().__init__()
        self.session = session
        self.lock = threading.Lock()
        self._results: queue.Queue = queue.Queue()
        self.account_label = account_label
        self.messages: list[MessageSummary] = []
        self.filtered: list[MessageSummary] = []
        self.current_folder = "INBOX"

        self.title(f"Mail - {account_label}")
        self.geometry("1320x840+140+90" if screenshot else "1320x840")
        self.minsize(900, 560)
        self.configure(bg=BG)

        self._style()
        self._build()
        self._wire_events()
        self.after(40, self._drain)

        self.after(60, self.reload_folders)
        self.after(120, self._place_sashes)
        if screenshot:
            self.after(2800, lambda: self._grab(screenshot))

    # ---------------------------------------------------------------- chrome
    def _style(self) -> None:
        style = ttk.Style(self)
        for theme in ("vista", "winnative", "clam"):
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue
        style.configure("TFrame", background=BG)
        style.configure("Toolbar.TFrame", background=TOOLBAR_BG)
        style.configure("Toolbar.TLabel", background=TOOLBAR_BG)
        style.configure("Title.TLabel", font=("Segoe UI", 15, "bold"), background=BG)
        style.configure("Meta.TLabel", foreground="#605e5c", background=BG, font=("Segoe UI", 9))
        style.configure("Status.TLabel", background=SIDE_BG, foreground="#323130", padding=(12, 5))
        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", rowheight=26)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

    def _build(self) -> None:
        bar = ttk.Frame(self, style="Toolbar.TFrame")
        bar.pack(side="top", fill="x")
        inner = ttk.Frame(bar, style="Toolbar.TFrame")
        inner.pack(fill="x", padx=12, pady=8)

        for text in ("New", "Reply", "Forward", "Archive", "Delete"):
            ttk.Button(inner, text=text, width=9, command=self._read_only).pack(side="left", padx=(0, 4))

        ttk.Separator(inner, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(inner, text="Refresh", width=9, command=self.reload_messages).pack(side="left")

        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(inner, textvariable=self.search_var, width=34)
        self.search_entry.pack(side="right")
        ttk.Label(inner, text="Search", style="Toolbar.TLabel").pack(side="right", padx=(0, 6))

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        self.status = ttk.Label(self, text="starting...", style="Status.TLabel", anchor="w")
        self.status.pack(side="bottom", fill="x")

        panes = self.panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Frame(panes, width=250)
        self.folders = ttk.Treeview(left, show="tree", selectmode="browse")
        self.folders.pack(fill="both", expand=True)
        panes.add(left, weight=0)

        middle = ttk.Frame(panes)
        self.listbox = ttk.Treeview(
            middle, columns=("from", "subject", "received"), show="headings", selectmode="browse"
        )
        self.listbox.heading("from", text="From")
        self.listbox.heading("subject", text="Subject")
        self.listbox.heading("received", text="Received")
        self.listbox.column("from", width=175, stretch=False)
        self.listbox.column("subject", width=280, stretch=True)
        self.listbox.column("received", width=115, stretch=False)
        self.listbox.tag_configure("unread", font=("Segoe UI", 9, "bold"))
        scroll = ttk.Scrollbar(middle, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.listbox.pack(fill="both", expand=True)
        panes.add(middle, weight=3)

        right = ttk.Frame(panes)
        self.subject_label = ttk.Label(
            right, text="", style="Title.TLabel", wraplength=620, justify="left"
        )
        self.subject_label.pack(anchor="w", padx=14, pady=(14, 2))
        self.meta_label = ttk.Label(right, text="", style="Meta.TLabel", wraplength=620, justify="left")
        self.meta_label.pack(anchor="w", padx=14)
        ttk.Separator(right).pack(fill="x", padx=14, pady=(8, 6))
        self.body = tk.Text(
            right,
            wrap="word",
            relief="flat",
            borderwidth=0,
            padx=14,
            pady=4,
            bg=BG,
            font=("Segoe UI", 10),
            spacing1=2,
            spacing3=4,
        )
        self.body.pack(fill="both", expand=True)
        self.body.configure(state="disabled")
        right.bind(
            "<Configure>",
            lambda event: (
                self.subject_label.configure(wraplength=max(200, event.width - 36)),
                self.meta_label.configure(wraplength=max(200, event.width - 36)),
            ),
        )
        panes.add(right, weight=4)



    def _wire_events(self) -> None:
        self.folders.bind("<<TreeviewSelect>>", self._on_folder)
        self.listbox.bind("<<TreeviewSelect>>", self._on_select)
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        self.bind("<F5>", lambda _e: self.reload_messages())
        self.bind("<Control-f>", lambda _e: self.search_entry.focus_set())
        self.bind("<Escape>", lambda _e: self.search_var.set(""))

    # ------------------------------------------------------------------ data
    def _submit(self, work, on_done) -> None:
        """Run IMAP work off the UI thread; deliver the result via _drain.

        Tk is not thread-safe: calling after() from a worker raises
        "main thread is not in main loop", so results go through a queue.
        """

        def runner() -> None:
            try:
                with self.lock:
                    result = work()
            except Exception as exc:  # surface it, never swallow
                self._results.put((None, exc))
                return
            self._results.put((on_done, result))

        threading.Thread(target=runner, daemon=True).start()

    def _drain(self) -> None:
        try:
            while True:
                callback, payload = self._results.get_nowait()
                if callback is None:
                    self._fail(payload)
                else:
                    callback(payload)
        except queue.Empty:
            pass
        except tk.TclError:
            return
        self.after(40, self._drain)

    def _fail(self, exc: Exception) -> None:
        self.status.configure(text=f"{type(exc).__name__}: {exc}")

    def _place_sashes(self) -> None:
        try:
            width = self.winfo_width()
            self.panes.sashpos(0, 250)
            self.panes.sashpos(1, max(420, width - 440))
        except tk.TclError:
            pass

    def reload_folders(self) -> None:
        self._submit(lambda: self.session.folders(), self._show_folders)

    def _show_folders(self, folders: list[str]) -> None:
        self.folders.delete(*self.folders.get_children())
        for name in folders:
            self.folders.insert("", "end", iid=name, text=f"  {name}")
        target = "INBOX" if "INBOX" in folders else folders[0]
        self.folders.selection_set(target)
        self.folders.focus(target)

    def _on_folder(self, _event: object = None) -> None:
        selection = self.folders.selection()
        if selection:
            self.current_folder = selection[0]
            self.reload_messages()

    def reload_messages(self) -> None:
        folder = self.current_folder
        self.status.configure(text=f"Loading {folder}...")
        self._submit(
            lambda: (self.session.select(folder), self.session.messages())[1],
            lambda rows: self._show_messages(folder, rows),
        )

    def _show_messages(self, folder: str, rows: list[MessageSummary]) -> None:
        self.messages = rows
        self._apply_filter()
        unread = sum(1 for row in rows if row.unread)
        self.status.configure(
            text=f"{len(rows)} items   -   {unread} unread   -   {folder}   -   {self.account_label}"
        )

    def _apply_filter(self) -> None:
        needle = self.search_var.get().strip().lower()
        self.filtered = [
            message
            for message in self.messages
            if not needle or needle in message.subject.lower() or needle in message.sender.lower()
        ]
        self.listbox.delete(*self.listbox.get_children())
        for message in self.filtered:
            self.listbox.insert(
                "",
                "end",
                iid=message.uid,
                values=(display_name(message.sender), message.subject, message.date),
                tags=("unread",) if message.unread else (),
            )
        if self.filtered:
            first = self.filtered[0].uid
            self.listbox.selection_set(first)
            self.listbox.focus(first)

    def _on_select(self, _event: object = None) -> None:
        selection = self.listbox.selection()
        if not selection:
            return
        uid = selection[0]
        message = next((m for m in self.filtered if m.uid == uid), None)
        if message is None:
            return
        self.subject_label.configure(text=message.subject or "(no subject)")
        self.meta_label.configure(text=f"{message.sender}    {message.date}")
        self._set_body("Loading...")
        self._submit(lambda: self.session.body_text(uid), self._set_body)

    def _set_body(self, text: str) -> None:
        self.body.configure(state="normal")
        self.body.delete("1.0", "end")
        self.body.insert("1.0", text or "(empty message)")
        self.body.configure(state="disabled")

    def _read_only(self) -> None:
        self.status.configure(
            text="Read-only client: compose, reply and delete are not implemented."
        )

    def _grab(self, path: str) -> None:
        try:
            import time

            from PIL import ImageGrab

            self.deiconify()
            self.lift()
            self.attributes("-topmost", True)
            self.update_idletasks()
            self.update()
            time.sleep(0.4)
            self.update()
            x, y = self.winfo_rootx(), self.winfo_rooty()
            box = (x, y, x + self.winfo_width(), y + self.winfo_height())
            ImageGrab.grab(bbox=box).save(path)
            print(f"screenshot saved: {path} box={box}")
        except Exception as exc:
            print(f"screenshot failed: {exc}")
        self.after(150, self.destroy)


class AccountDialog(tk.Toplevel):
    """Add-an-account form, the GUI equivalent of the console wizard."""

    def __init__(self, parent: tk.Misc, config_path) -> None:
        super().__init__(parent)
        self.title("Add account")
        self.resizable(False, False)
        self.config_path = config_path
        self.result: Account | None = None

        self.email = tk.StringVar()
        self.host = tk.StringVar(value="")
        self.credential = tk.StringVar()
        self.name = tk.StringVar()
        self.provider = tk.StringVar(value="microsoft")

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        self._row(frame, 0, "Email address", ttk.Entry(frame, textvariable=self.email, width=44))
        self.provider_label = ttk.Label(frame, text="")
        self.provider_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.host_label = ttk.Label(frame, text="IMAP host")
        self.host_entry = ttk.Entry(frame, textvariable=self.host, width=44)
        self.host_label.grid(row=2, column=0, sticky="w", pady=4)
        self.host_entry.grid(row=2, column=1, sticky="we", pady=4)

        self.cred_label = ttk.Label(frame, text="Application (client) ID")
        self.cred_entry = ttk.Entry(frame, textvariable=self.credential, width=44, show="")
        self.cred_label.grid(row=3, column=0, sticky="w", pady=4)
        self.cred_entry.grid(row=3, column=1, sticky="we", pady=4)

        self.hint = ttk.Label(frame, text="", wraplength=430, justify="left", foreground="#605e5c")
        self.hint.grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self._row(frame, 5, "Name", ttk.Entry(frame, textvariable=self.name, width=44))

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="Save", command=self._save).pack(side="right")

        self.email.trace_add("write", lambda *_: self._detect())
        self._detect()
        self.transient(parent)
        self.grab_set()

    def _row(self, parent, row: int, label: str, widget) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        widget.grid(row=row, column=1, sticky="we", pady=4)

    def _detect(self) -> None:
        email = self.email.get().strip()
        provider = guess_provider(email) if "@" in email else "microsoft"
        self.provider.set(provider)
        self.provider_label.configure(text=f"Detected provider: {provider}")

        if provider == "google":
            self.host.set("imap.gmail.com")
            self.cred_label.configure(text="Google app password")
            self.cred_entry.configure(show="*")
            self.hint.configure(text="Create one at myaccount.google.com/apppasswords (needs 2-Step Verification).")
        elif provider == "microsoft":
            self.host.set("outlook.office365.com")
            self.cred_label.configure(text="Application (client) ID")
            self.cred_entry.configure(show="")
            self.hint.configure(text=registration_hint())
        else:
            if not self.host.get():
                self.host.set("")
            self.cred_label.configure(text="Password")
            self.cred_entry.configure(show="*")
            self.hint.configure(text="Any IMAP server on port 993 (implicit TLS).")

    def _save(self) -> None:
        email = self.email.get().strip()
        credential = self.credential.get().strip()
        if "@" not in email:
            messagebox.showerror("Add account", "Enter a complete email address.", parent=self)
            return
        if not credential:
            messagebox.showerror("Add account", "The credential field is required.", parent=self)
            return

        provider = self.provider.get()
        account = Account(
            name=self.name.get().strip() or email.split("@")[0],
            provider=provider,
            host=self.host.get().strip() or "outlook.office365.com",
            user=email,
            client_id=credential if provider == "microsoft" else None,
            tenant="consumers" if email.rsplit("@", 1)[-1].lower() in {
                "outlook.com", "hotmail.com", "live.com", "msn.com"
            } else "organizations",
            app_password=None if provider == "microsoft" else credential,
        )

        existing = load_config(str(self.config_path), missing_ok=True)
        merged = [item for item in existing if item.user.lower() != email.lower()] + [account]
        write_config(merged, self.config_path)
        self.result = account
        self.destroy()


def ask_for_account(parent: tk.Misc | None, config_path=None) -> Account | None:
    config_path = config_path or default_config_path()
    if parent is None:
        root = tk.Tk()
        root.withdraw()
        dialog = AccountDialog(root, config_path)
        root.wait_window(dialog)
        root.destroy()
        return dialog.result
    dialog = AccountDialog(parent, config_path)
    parent.wait_window(dialog)
    return dialog.result
