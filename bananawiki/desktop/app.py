"""The launcher window (tkinter, from the standard library).

One window: choose the data folder, choose whether other devices may connect,
start and stop the wiki, see its addresses (with a QR code for phones and
tablets) and the setup code, and back up or restore the data. Slow work
(starting, backups) runs on a worker thread; its result comes back to the Tk
thread through a queue, because Tk must only be touched from its own thread.
"""

from __future__ import annotations

import os
import platform
import queue
import subprocess
import threading
import time
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import DesktopError, backup, i18n, preferences
from .datafolder import DataFolder
from .server import ServerInfo, WikiServer

ICON = Path(__file__).resolve().parent / "static" / "icon.png"
POLL_MS = 100
HEALTH_POLL_MS = 1000
WRAP = 460


def open_in_file_manager(path: Path) -> None:
    system = platform.system()
    if system == "Windows":
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606 - opens a folder the user chose
    elif system == "Darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class LauncherWindow:
    def __init__(self) -> None:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.tk, self.ttk, self.filedialog, self.messagebox = tk, ttk, filedialog, messagebox
        self.prefs = preferences.load()
        self.t = i18n.Translator(self.prefs.language)
        self.server: WikiServer | None = None
        self.info: ServerInfo | None = None
        self.busy = False
        self.qr_image: Any = None
        self.qr_url: str | None = None
        self._results: queue.Queue[tuple[Callable[..., None], Any, BaseException | None]] = queue.Queue()
        self._texts: list[tuple[Any, str]] = []

        _enable_high_dpi()
        self.window = tk.Tk()
        self.window.minsize(520, 560)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        if ICON.is_file():
            self._icon = tk.PhotoImage(file=str(ICON))
            self.window.iconphoto(True, self._icon)
        self.share_var = tk.BooleanVar(value=self.prefs.share_on_lan)
        self.status_var = tk.StringVar()
        self.folder_var = tk.StringVar(value=self.prefs.data_dir)
        self.local_var = tk.StringVar()
        self.lan_var = tk.StringVar()
        self.token_var = tk.StringVar()
        self.message_var = tk.StringVar()
        self._build()
        self._retranslate()
        self._refresh()
        self.window.after(POLL_MS, self._drain_results)
        self.window.after(HEALTH_POLL_MS, self._watch_server)

    # ── Layout ────────────────────────────────────────────────────────────

    def _label(self, parent: Any, key: str, **options: Any) -> Any:
        widget = self.ttk.Label(parent, wraplength=WRAP, justify="left", **options)
        self._texts.append((widget, key))
        return widget

    def _button(self, parent: Any, key: str, command: Callable[[], None], **options: Any) -> Any:
        widget = self.ttk.Button(parent, command=command, **options)
        self._texts.append((widget, key))
        return widget

    def _section(self, parent: Any, key: str, *, optional: bool = False) -> Any:
        """A titled box; an optional one sits in an always-packed holder so it keeps its place."""
        if optional:
            parent = self.ttk.Frame(parent)
            parent.pack(fill="x")
        frame = self.ttk.LabelFrame(parent, padding=10)
        self._texts.append((frame, key))
        if not optional:
            frame.pack(fill="x", pady=(10, 0))
        return frame

    def _build(self) -> None:
        ttk = self.ttk
        style = ttk.Style(self.window)
        style.configure("Title.TLabel", font=("TkDefaultFont", 18, "bold"))
        style.configure("Status.TLabel", font=("TkDefaultFont", 11, "bold"))
        style.configure("Mono.TEntry", font=("TkFixedFont", 10))

        outer = ttk.Frame(self.window, padding=16)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="BananaWiki", style="Title.TLabel").pack(side="left")
        self.language_box = ttk.Combobox(header, state="readonly", width=10, values=list(i18n.LANGUAGES.values()))
        self.language_box.set(i18n.LANGUAGES[self.t.language])
        self.language_box.bind("<<ComboboxSelected>>", self._language_changed)
        self.language_box.pack(side="right")
        self._label(outer, "app.subtitle").pack(fill="x", pady=(4, 0))

        folder = self._section(outer, "folder.title")
        ttk.Label(folder, textvariable=self.folder_var, wraplength=WRAP).pack(fill="x")
        row = ttk.Frame(folder)
        row.pack(fill="x", pady=(8, 0))
        self.change_button = self._button(row, "folder.change", self.change_folder)
        self.change_button.pack(side="left")
        self._button(row, "folder.open", self.open_folder).pack(side="left", padx=(8, 0))

        control = self._section(outer, "server.title")
        self.share_check = ttk.Checkbutton(control, variable=self.share_var, command=self._share_toggled)
        self._texts.append((self.share_check, "server.share"))
        self.share_check.pack(anchor="w")
        row = ttk.Frame(control)
        row.pack(fill="x", pady=(10, 0))
        self.start_button = self._button(row, "server.start", self.start)
        self.start_button.pack(side="left")
        self.stop_button = self._button(row, "server.stop", self.stop)
        self.stop_button.pack(side="left", padx=(8, 0))
        self.open_button = self._button(row, "server.open", self.open_browser)
        self.open_button.pack(side="left", padx=(8, 0))
        ttk.Label(control, textvariable=self.status_var, style="Status.TLabel", wraplength=WRAP).pack(
            fill="x", pady=(10, 0))

        self.address_frame = self._section(outer, "address.title", optional=True)
        self._address_row(self.address_frame, "address.local", self.local_var)
        lan_holder = ttk.Frame(self.address_frame)
        lan_holder.pack(fill="x")
        self.lan_row = self._address_row(lan_holder, "address.lan", self.lan_var)
        self.qr_label = ttk.Label(self.address_frame)
        self.qr_label.pack(anchor="w", pady=(6, 0))

        self.setup_frame = self._section(outer, "setup.title", optional=True)
        self._label(self.setup_frame, "setup.explain").pack(fill="x")
        self._address_row(self.setup_frame, None, self.token_var)

        data = self._section(outer, "data.title")
        row = ttk.Frame(data)
        row.pack(fill="x")
        self.backup_button = self._button(row, "data.backup", self.backup)
        self.backup_button.pack(side="left")
        self.restore_button = self._button(row, "data.restore", self.restore)
        self.restore_button.pack(side="left", padx=(8, 0))
        self.reset_button = self._button(row, "data.reset", self.reset)
        self.reset_button.pack(side="left", padx=(8, 0))

        ttk.Label(outer, textvariable=self.message_var, wraplength=WRAP, justify="left").pack(
            fill="x", pady=(10, 0))

    def _address_row(self, parent: Any, key: str | None, variable: Any) -> Any:
        row = self.ttk.Frame(parent)
        row.pack(fill="x", pady=(4, 0))
        if key:
            label = self.ttk.Label(row, width=16)
            self._texts.append((label, key))
            label.pack(side="left")
        entry = self.ttk.Entry(row, textvariable=variable, state="readonly", style="Mono.TEntry")
        entry.pack(side="left", fill="x", expand=True)
        self._button(row, "action.copy", lambda: self.copy(variable.get())).pack(side="left", padx=(8, 0))
        return row

    def _retranslate(self) -> None:
        self.window.title(self.t("app.title"))
        for widget, key in self._texts:
            widget.configure(text=self.t(key))
        self._refresh()

    # ── State ─────────────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self.server is not None and self.server.running

    def _refresh(self) -> None:
        running = self.running
        idle = not self.busy

        def enable(widget: Any, condition: bool) -> None:
            widget.state(["!disabled"] if condition else ["disabled"])

        enable(self.start_button, idle and not running)
        enable(self.stop_button, idle and running)
        enable(self.open_button, running)
        enable(self.change_button, idle and not running)
        enable(self.share_check, idle and not running)
        enable(self.backup_button, idle)
        enable(self.restore_button, idle and not running)
        enable(self.reset_button, idle and not running)
        if running and self.info:
            if idle:
                self.status_var.set(self.t("status.running_lan" if self.info.shared else "status.running"))
            self.local_var.set(self.info.local_url)
            self.lan_var.set(self.info.lan_url or self.t("address.no_network"))
        else:
            if idle:
                self.status_var.set(self.t("status.stopped"))
            self.local_var.set("")
            self.lan_var.set("")
        self._refresh_setup()
        self._show(self.address_frame, running)
        self._show(self.lan_row, running and bool(self.info and self.info.shared))
        self._update_qr()

    def _refresh_setup(self) -> None:
        """Show the setup code until the first administrator has been created."""
        show = self.running and self.info is not None and not self.folder().setup_done()
        self.token_var.set(self.info.setup_token if show and self.info else "")
        self._show(self.setup_frame, show)

    @staticmethod
    def _show(widget: Any, visible: bool) -> None:
        if visible and not widget.winfo_manager():
            widget.pack(fill="x", pady=(10, 0))
        elif not visible and widget.winfo_manager():
            widget.pack_forget()

    def _update_qr(self) -> None:
        url = self.info.lan_url if self.running and self.info else None
        if url == self.qr_url:
            return
        self.qr_url = url
        self.qr_image = _qr_photo(url) if url else None
        self.qr_label.configure(image=self.qr_image or "")

    def folder(self) -> DataFolder:
        return DataFolder(self.folder_var.get())

    def _save_prefs(self) -> None:
        try:
            preferences.save(self.prefs)
        except OSError as error:
            self.message_var.set(self.t.error(error))

    # ── Background work ───────────────────────────────────────────────────

    def _in_background(self, status_key: str, work: Callable[[], Any], done: Callable[[Any], None]) -> None:
        self.busy = True
        self.status_var.set(self.t(status_key))
        self.message_var.set("")
        self._refresh()

        def run() -> None:
            try:
                self._results.put((done, work(), None))
            except BaseException as error:  # noqa: BLE001 - shown to the user
                self._results.put((done, None, error))

        threading.Thread(target=run, name="bananawiki-desktop-task", daemon=True).start()

    def _drain_results(self) -> None:
        try:
            while True:
                done, result, error = self._results.get_nowait()
                self.busy = False
                if error is not None:
                    self._refresh()
                    self.status_var.set(self.t("status.failed"))
                    self.messagebox.showerror(self.t("app.title"), self.t.error(error), parent=self.window)
                else:
                    done(result)
                    self._refresh()
        except queue.Empty:
            pass
        self.window.after(POLL_MS, self._drain_results)

    def _watch_server(self) -> None:
        if self.server is not None and not self.busy:
            if self.server.running:
                self._refresh_setup()
            else:
                self.server.stop()
                self.server, self.info = None, None
                self._refresh()
                self.status_var.set(self.t("status.crashed"))
        self.window.after(HEALTH_POLL_MS, self._watch_server)

    # ── Actions ───────────────────────────────────────────────────────────

    def _language_changed(self, _event: Any = None) -> None:
        names = {name: code for code, name in i18n.LANGUAGES.items()}
        self.prefs.language = names.get(self.language_box.get(), i18n.DEFAULT_LANGUAGE)
        self.t = i18n.Translator(self.prefs.language)
        self._save_prefs()
        self._retranslate()

    def _share_toggled(self) -> None:
        if self.share_var.get() and not self.messagebox.askyesno(
                self.t("share.confirm_title"), self.t("share.confirm"), icon="warning", default="no",
                parent=self.window):
            self.share_var.set(False)
        self.prefs.share_on_lan = bool(self.share_var.get())
        self._save_prefs()

    def change_folder(self) -> None:
        chosen = self.filedialog.askdirectory(parent=self.window, initialdir=self.folder_var.get(),
                                              title=self.t("folder.choose"), mustexist=False)
        if not chosen:
            return
        folder = DataFolder(chosen)
        try:
            folder.check()
        except DesktopError as error:
            if error.key != "folder_not_empty":
                self.messagebox.showerror(self.t("app.title"), self.t.error(error), parent=self.window)
                return
            inside = DataFolder(Path(chosen) / "BananaWiki")
            if not self.messagebox.askyesno(self.t("folder.title"),
                                            self.t("folder.use_subfolder", path=str(inside.root)),
                                            parent=self.window):
                return
            folder = inside
        self.folder_var.set(str(folder.root))
        self.prefs.data_dir = str(folder.root)
        self._save_prefs()
        self._refresh()

    def open_folder(self) -> None:
        folder = self.folder()
        try:
            folder.prepare()
            open_in_file_manager(folder.root)
        except (DesktopError, OSError) as error:
            self.messagebox.showerror(self.t("app.title"), self.t.error(error), parent=self.window)

    def start(self) -> None:
        server = WikiServer(self.folder(), share_on_lan=bool(self.share_var.get()), port=self.prefs.port,
                            language=self.t.language)

        def started(info: ServerInfo) -> None:
            self.server, self.info = server, info
            webbrowser.open(info.local_url)

        self._in_background("status.starting", server.start, started)

    def stop(self) -> None:
        server = self.server

        def stopped(_result: Any) -> None:
            self.server, self.info = None, None

        if server is not None:
            self._in_background("status.stopping", server.stop, stopped)

    def open_browser(self) -> None:
        if self.info:
            webbrowser.open(self.info.local_url)

    def copy(self, text: str) -> None:
        if text:
            self.window.clipboard_clear()
            self.window.clipboard_append(text)
            self.message_var.set(self.t("action.copied"))

    def backup(self) -> None:
        target = self.filedialog.asksaveasfilename(
            parent=self.window, title=self.t("data.backup"), defaultextension=".zip",
            initialfile=f"bananawiki-backup-{time.strftime('%Y%m%d-%H%M')}.zip",
            filetypes=[(self.t("data.zip_files"), "*.zip")])
        if not target:
            return
        folder = self.folder()
        self._in_background("status.backing_up", lambda: backup.create_backup(folder, target),
                            lambda path: self.message_var.set(self.t("data.backup_done", path=str(path))))

    def restore(self) -> None:
        source = self.filedialog.askopenfilename(parent=self.window, title=self.t("data.restore"),
                                                 filetypes=[(self.t("data.zip_files"), "*.zip")])
        if not source or not self.messagebox.askyesno(self.t("data.restore"), self.t("data.restore_confirm"),
                                                      icon="warning", default="no", parent=self.window):
            return
        folder = self.folder()
        self._in_background("status.restoring", lambda: backup.restore_backup(folder, source),
                            lambda path: self.message_var.set(self.t("data.restore_done", path=str(path))))

    def reset(self) -> None:
        if not self.messagebox.askyesno(self.t("data.reset"), self.t("data.reset_confirm"), icon="warning",
                                        default="no", parent=self.window):
            return
        folder = self.folder()
        self._in_background("status.resetting", lambda: backup.reset_data(folder),
                            lambda _result: self.message_var.set(self.t("data.reset_done")))

    def close(self) -> None:
        if self.busy:  # never cut a backup, restore or start short
            self.message_var.set(self.t("status.wait"))
            return
        if self.server is not None:
            self.server.stop()
        self.window.destroy()

    def run(self) -> None:
        self.window.mainloop()


def _enable_high_dpi() -> None:
    if platform.system() != "Windows":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass


def _qr_photo(text: str) -> Any:
    """A QR code for *text* as a Tk image, or None when qrcode/Pillow are missing."""
    try:
        import qrcode
        from PIL import ImageTk
    except ImportError:
        return None
    code = qrcode.QRCode(border=2, box_size=5)
    code.add_data(text)
    code.make(fit=True)
    return ImageTk.PhotoImage(code.make_image(fill_color="black", back_color="white").convert("RGB"))


def run() -> int:
    """Open the launcher window; returns the process exit status."""
    try:
        LauncherWindow().run()
    except Exception as error:  # noqa: BLE001 - last resort: tell the user instead of vanishing
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("BananaWiki", i18n.Translator(i18n.system_language()).error(error))
            root.destroy()
        except Exception:  # noqa: BLE001 - no display at all
            raise error from None
        return 1
    return 0
