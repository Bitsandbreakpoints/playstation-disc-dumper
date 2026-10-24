"""Playstation Disc Dumper - a simple interface for converting PlayStation games to and from .chd files.

Uses MAME's official chdman (tools/chdman.exe). Turns .cue/.bin and .iso images
into .chd (ripping a physical disc first if needed) and .chd back into .bin/.cue or .iso.
"""

import ctypes
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import discio

FROZEN = getattr(sys, "frozen", False)
# When packaged with PyInstaller, bundled files live in a temp dir (_MEIPASS) while
# user-facing paths (default output folder) should sit next to the .exe.
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
CHDMAN = BUNDLE_DIR / "tools" / "chdman.exe"
ICON = BUNDLE_DIR / "assets" / "icon.ico"
SETTINGS_FILE = Path(os.environ.get("APPDATA", APP_DIR)) / "PlaystationDiscDumper" / "settings.json"
LEGACY_SETTINGS_FILE = SETTINGS_FILE.parent.with_name("CHDMaker") / "settings.json"  # pre-rename location
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
IMAGE_TYPES = (".cue", ".iso", ".chd")

BG = "#000000"
PANEL = "#0d0d0d"
CARD = "#141414"
CARD_HI = "#222222"
BORDER = "#333333"
TEXT = "#ffffff"
MUTED = "#999999"
# Accents taken from the PlayStation logo: red, yellow, teal and blue
ACCENT = "#d70028"
ACCENT_HI = "#f0193f"
ON_ACCENT = "#ffffff"  # text on ACCENT backgrounds
SUCCESS = "#03a496"
WARN = "#e6bb00"
ERROR = "#f0193f"
BLUE = "#3068ac"

FONT = "Segoe UI"


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def safe_filename(name):
    return re.sub(r'[<>:"/\\|?*]+', "", name).strip(" .") or "Game"


def chd_kind(path):
    """"cd" or "dvd" from a CHD's metadata, None if it isn't a disc CHD chdman can read."""
    try:
        out = subprocess.run([str(CHDMAN), "info", "-i", str(path)], capture_output=True, text=True,
                             creationflags=NO_WINDOW, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    if "Tag='DVD '" in out:
        return "dvd"
    if re.search(r"Tag='(CHT2|CHTR|CHCD|CHGD|CHGT)'", out):
        return "cd"
    return None


@dataclass
class Job:
    source: Path
    kind: str  # "cd" or "dvd"
    name: str
    size: int
    serial: str = None
    error: str = None
    iid: str = None
    done: bool = False

    @property
    def extract(self):
        """True when this job turns a .chd back into .bin/.cue (or .iso) instead of creating one."""
        return self.source.suffix.lower() == ".chd"

    def outputs(self, out):
        """All files an extraction to out (.cue or .iso) produces."""
        return [out] if self.kind == "dvd" else [out, out.with_suffix(".bin")]


class DropTarget:
    """Accept files dragged from Explorer onto a Tk window (Win32 WM_DROPFILES).

    The window procedure runs inside Tcl's event loop while tkinter has released the
    GIL, so calling back into tkinter from there crashes the interpreter. callback
    must therefore not touch Tk; it should only hand the paths off (e.g. to a queue).
    """

    def __init__(self, widget, callback):
        self.ok = False
        if os.name != "nt":
            return
        try:
            user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
            from ctypes import wintypes
            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                         wintypes.WPARAM, wintypes.LPARAM)
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            user32.CallWindowProcW.restype = ctypes.c_ssize_t
            user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                               wintypes.WPARAM, wintypes.LPARAM]
            shell32.DragQueryFileW.restype = wintypes.UINT
            shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_wchar_p, wintypes.UINT]
            shell32.DragFinish.argtypes = [ctypes.c_void_p]
            shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]

            def proc(hwnd, msg, wparam, lparam):
                if msg == 0x0233:  # WM_DROPFILES
                    files = []
                    for i in range(shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)):
                        length = shell32.DragQueryFileW(wparam, i, None, 0)
                        buf = ctypes.create_unicode_buffer(length + 1)
                        shell32.DragQueryFileW(wparam, i, buf, length + 1)
                        files.append(buf.value)
                    shell32.DragFinish(wparam)
                    callback(files)
                    return 0
                return user32.CallWindowProcW(self.old, hwnd, msg, wparam, lparam)

            hwnd = widget.winfo_id()
            self.proc = WNDPROC(proc)
            shell32.DragAcceptFiles(hwnd, True)
            self.old = user32.SetWindowLongPtrW(hwnd, -4, ctypes.cast(self.proc, ctypes.c_void_p))
            self.ok = True
        except Exception:
            pass


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.jobs = []
        self.worker = None
        self.cancel_flag = threading.Event()
        self.proc = None
        self.disc_info = None
        self.settings = self.load_settings()

        root.title("Playstation Disc Dumper")
        root.configure(bg=BG)
        scale = root.winfo_fpixels("1i") / 96
        root.geometry(f"{int(1000 * scale)}x{int(760 * scale)}")
        root.minsize(int(860 * scale), int(680 * scale))
        self.set_icon()
        self.setup_style()
        self.build()

        self.chdman_version = self.detect_chdman()
        self.drop = DropTarget(root, lambda files: self.events.put(("drop", files)))
        self.update_empty_hint()
        self.refresh_drives()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll_events)

    # ------------------------------------------------------------------ setup

    def set_icon(self):
        if ICON.exists():
            self.root.iconbitmap(default=str(ICON))
            return
        icon = tk.PhotoImage(width=32, height=32)
        icon.put(ACCENT, to=(0, 0, 32, 32))
        icon.put(BG, to=(10, 10, 22, 22))
        icon.put(TEXT, to=(14, 14, 18, 18))
        self.root.iconphoto(True, icon)
        self._icon = icon

    def setup_style(self):
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=TEXT, font=(FONT, 10), bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, troughcolor=CARD, focuscolor=ACCENT)
        s.configure("TFrame", background=BG)
        s.configure("Card.TFrame", background=CARD)
        s.configure("TLabel", background=BG, foreground=TEXT)
        s.configure("Card.TLabel", background=CARD, foreground=TEXT)
        s.configure("CardMuted.TLabel", background=CARD, foreground=MUTED)
        s.configure("Muted.TLabel", background=BG, foreground=MUTED)
        s.configure("Title.TLabel", background=BG, foreground=TEXT, font=(FONT, 20, "bold"))
        s.configure("Section.TLabel", background=BG, foreground=MUTED, font=(FONT, 9, "bold"))
        s.configure("Value.TLabel", background=CARD, foreground=TEXT, font=(FONT, 11, "bold"))

        s.configure("TButton", background=CARD_HI, foreground=TEXT, borderwidth=0, padding=(14, 8),
                    font=(FONT, 10))
        s.map("TButton", background=[("disabled", CARD), ("pressed", BORDER), ("active", "#2c2c2c")],
              foreground=[("disabled", "#555555")])
        s.configure("Accent.TButton", background=ACCENT, foreground=ON_ACCENT, padding=(28, 11),
                    font=(FONT, 11, "bold"))
        s.map("Accent.TButton", background=[("disabled", "#262626"), ("pressed", "#a8001f"),
                                            ("active", ACCENT_HI)],
              foreground=[("disabled", "#666666")])
        s.configure("Danger.TButton", background=CARD_HI, foreground=ERROR, padding=(18, 11),
                    font=(FONT, 11))
        s.map("Danger.TButton", background=[("disabled", CARD), ("active", "#2c2c2c")],
              foreground=[("disabled", "#555555")])

        s.configure("TCheckbutton", background=BG, foreground=TEXT, indicatorbackground=CARD,
                    indicatorforeground=TEXT, indicatormargin=(0, 0, 8, 0))
        s.map("TCheckbutton", background=[("active", BG)],
              indicatorbackground=[("selected", ACCENT), ("active", CARD_HI)],
              indicatorforeground=[("selected", ON_ACCENT)])
        s.configure("Card.TCheckbutton", background=CARD)
        s.map("Card.TCheckbutton", background=[("active", CARD)])

        s.configure("TEntry", fieldbackground=CARD, foreground=TEXT, insertcolor=TEXT, padding=(8, 6),
                    bordercolor=BORDER)
        s.map("TEntry", bordercolor=[("focus", ACCENT)], fieldbackground=[("disabled", PANEL)],
              foreground=[("disabled", MUTED)])
        s.configure("TCombobox", fieldbackground=CARD_HI, background=CARD_HI, foreground=TEXT,
                    arrowcolor=TEXT, padding=(8, 6), bordercolor=BORDER)
        s.map("TCombobox", fieldbackground=[("readonly", CARD_HI)], selectbackground=[("readonly", CARD_HI)],
              selectforeground=[("readonly", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", ON_ACCENT)
        self.root.option_add("*TCombobox*Listbox.font", (FONT, 10))

        s.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT, rowheight=34,
                    borderwidth=0, font=(FONT, 10))
        s.map("Treeview", background=[("selected", "#3a3a3a")], foreground=[("selected", TEXT)])
        s.configure("Treeview.Heading", background=PANEL, foreground=MUTED, font=(FONT, 9, "bold"),
                    borderwidth=0, padding=(10, 8), relief="flat")
        s.map("Treeview.Heading", background=[("active", PANEL)])
        s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

        s.configure("Accent.Horizontal.TProgressbar", background=ACCENT, troughcolor=CARD,
                    bordercolor=CARD, lightcolor=ACCENT, darkcolor=ACCENT, thickness=10)
        s.layout("Vertical.TScrollbar", [("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
        s.configure("Vertical.TScrollbar", background=CARD_HI, troughcolor=CARD, bordercolor=CARD,
                    lightcolor=CARD_HI, darkcolor=CARD_HI, gripcount=0, arrowsize=10)
        s.map("Vertical.TScrollbar", background=[("active", BORDER)])

    def build(self):
        outer = ttk.Frame(self.root, padding=(26, 20, 26, 18))
        outer.pack(fill="both", expand=True)

        # Header
        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="Playstation Disc Dumper", style="Title.TLabel").pack(side="left")
        self.version_label = ttk.Label(header, text="", style="Muted.TLabel")
        self.version_label.pack(side="left", padx=(14, 0), pady=(8, 0))

        self.tabs = {}
        tabbar = tk.Frame(header, bg=PANEL, padx=4, pady=4)
        tabbar.pack(side="right")
        for key, label in (("files", "Files (.cue / .iso / .chd)"), ("disc", "Physical disc")):
            t = tk.Label(tabbar, text=label, font=(FONT, 10), padx=16, pady=7, cursor="hand2")
            t.pack(side="left")
            t.bind("<Button-1>", lambda e, k=key: self.show_tab(k))
            self.tabs[key] = t
        ttk.Label(outer, text="Convert PlayStation games between .bin/.cue and compressed .chd files for "
                              "DuckStation, RetroArch, Recompilations and more.",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 16))

        # Bottom first so it keeps its space when the window is small
        self.build_bottom(outer)
        self.pages = ttk.Frame(outer)
        self.pages.pack(fill="both", expand=True)
        self.page_files = self.build_files_page(self.pages)
        self.page_disc = self.build_disc_page(self.pages)
        self.show_tab("files")

    def build_files_page(self, parent):
        page = ttk.Frame(parent)
        bar = ttk.Frame(page)
        bar.pack(fill="x", pady=(0, 10))
        self.btn_add = ttk.Button(bar, text="＋  Add files", command=self.pick_files)
        self.btn_add.pack(side="left")
        self.btn_add_dir = ttk.Button(bar, text="＋  Add folder", command=self.pick_folder)
        self.btn_add_dir.pack(side="left", padx=8)
        self.btn_clear = ttk.Button(bar, text="Clear list", command=self.clear_jobs)
        self.btn_clear.pack(side="right")
        self.btn_remove = ttk.Button(bar, text="Remove", command=self.remove_selected)
        self.btn_remove.pack(side="right", padx=8)

        box = tk.Frame(page, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        box.pack(fill="both", expand=True)
        cols = ("serial", "type", "convert", "size", "status")
        self.tree = ttk.Treeview(box, columns=cols, selectmode="extended")
        self.tree.heading("#0", text="  GAME", anchor="w")
        for c, label, w, anchor in (("serial", "SERIAL", 110, "w"), ("type", "TYPE", 60, "w"),
                                    ("convert", "CONVERT", 130, "w"), ("size", "SIZE", 90, "e"),
                                    ("status", "STATUS", 200, "w")):
            self.tree.heading(c, text=label, anchor=anchor)
            self.tree.column(c, width=w, minwidth=60, anchor=anchor, stretch=False)
        self.tree.column("#0", width=320, minwidth=160, stretch=True)
        for tag, color in (("ok", SUCCESS), ("err", ERROR), ("warn", WARN), ("run", BLUE)):
            self.tree.tag_configure(tag, foreground=color)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Delete>", lambda e: self.remove_selected())

        self.empty_hint = tk.Frame(box, bg=CARD)
        tk.Label(self.empty_hint, text="⤓", bg=CARD, fg=ACCENT, font=(FONT, 34)).pack()
        tk.Label(self.empty_hint, text="Drop .cue, .iso or .chd files here", bg=CARD, fg=TEXT,
                 font=(FONT, 13, "bold")).pack(pady=(2, 4))
        tk.Label(self.empty_hint, text="…or use “Add files”. Whole folders work too.",
                 bg=CARD, fg=MUTED, font=(FONT, 10)).pack()
        for w in (self.empty_hint, *self.empty_hint.winfo_children()):
            w.bind("<Button-1>", lambda e: self.pick_files())
        return page

    def build_disc_page(self, parent):
        page = ttk.Frame(parent)
        bar = ttk.Frame(page)
        bar.pack(fill="x", pady=(0, 10))
        ttk.Label(bar, text="Drive").pack(side="left", padx=(0, 10))
        self.drive_var = tk.StringVar()
        self.drive_combo = ttk.Combobox(bar, textvariable=self.drive_var, state="readonly", width=34,
                                        font=(FONT, 10))
        self.drive_combo.pack(side="left")
        self.drive_combo.bind("<<ComboboxSelected>>", lambda e: self.reset_disc_info())
        self.btn_refresh = ttk.Button(bar, text="↻", width=3, command=self.refresh_drives)
        self.btn_refresh.pack(side="left", padx=8)
        self.btn_read = ttk.Button(bar, text="Read disc", command=self.read_disc)
        self.btn_read.pack(side="left")

        card = tk.Frame(page, bg=CARD, highlightthickness=1, highlightbackground=BORDER, padx=24, pady=20)
        card.pack(fill="both", expand=True)
        grid = ttk.Frame(card, style="Card.TFrame")
        grid.pack(fill="x")
        self.disc_fields = {}
        for i, (key, label) in enumerate((("type", "DISC"), ("serial", "SERIAL NUMBER"),
                                          ("volume", "VOLUME NAME"), ("size", "SIZE"))):
            cell = ttk.Frame(grid, style="Card.TFrame")
            cell.grid(row=0, column=i, sticky="w", padx=(0, 40))
            ttk.Label(cell, text=label, style="CardMuted.TLabel", font=(FONT, 8, "bold")).pack(anchor="w")
            v = ttk.Label(cell, text="—", style="Value.TLabel")
            v.pack(anchor="w", pady=(2, 0))
            self.disc_fields[key] = v
        self.disc_status = ttk.Label(card, text="Insert a disc and click “Read disc”.",
                                     style="CardMuted.TLabel")
        self.disc_status.pack(anchor="w", pady=(16, 18))

        tk.Frame(card, bg=BORDER, height=1).pack(fill="x", pady=(0, 18))
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="File name", style="Card.TLabel").pack(side="left", padx=(0, 12))
        self.disc_name = tk.StringVar()
        self.disc_name_entry = ttk.Entry(row, textvariable=self.disc_name, font=(FONT, 10))
        self.disc_name_entry.pack(side="left", fill="x", expand=True)
        ttk.Label(row, text=".chd", style="CardMuted.TLabel").pack(side="left", padx=(6, 0))
        self.keep_rip = tk.BooleanVar(value=self.settings.get("keep_rip", False))
        ttk.Checkbutton(card, text="Keep .bin/.cue (or .iso) after converting", variable=self.keep_rip,
                        style="Card.TCheckbutton").pack(anchor="w", pady=(14, 0))
        ttk.Label(card, text="The disc is read raw (2352 bytes/sector) including audio tracks and then compressed "
                             "with chdman.\nPAL games with LibCrypt protection need an .sbi file next to the .chd file "
                             "in the emulator.", style="CardMuted.TLabel", justify="left").pack(anchor="w", pady=(14, 0))
        return page

    def build_bottom(self, outer):
        bottom = ttk.Frame(outer)
        bottom.pack(side="bottom", fill="x", pady=(16, 0))

        out = ttk.Frame(bottom)
        out.pack(fill="x")
        ttk.Label(out, text="SAVE TO", style="Section.TLabel").pack(side="left", padx=(0, 12))
        self.default_out = str(APP_DIR / "CHD")
        self.out_dir = tk.StringVar(value=self.settings.get("out_dir") or self.default_out)
        self.out_entry = ttk.Entry(out, textvariable=self.out_dir, font=(FONT, 10))
        self.out_entry.pack(side="left", fill="x", expand=True)
        self.out_entry.bind("<Button-1>", self.use_out_dir)
        self.btn_browse = ttk.Button(out, text="Browse…", command=self.pick_out_dir)
        self.btn_browse.pack(side="left", padx=(8, 0))
        self.btn_open_out = ttk.Button(out, text="Open", command=self.open_out_dir)
        self.btn_open_out.pack(side="left", padx=(8, 0))

        opts = ttk.Frame(bottom)
        opts.pack(fill="x", pady=(10, 0))
        self.same_dir = tk.BooleanVar(value=self.settings.get("same_dir", True))
        self.chk_same = ttk.Checkbutton(opts, text="Save next to the source file instead",
                                        variable=self.same_dir, command=self.update_out_state)
        self.chk_same.pack(side="left")

        prog = tk.Frame(bottom, bg=PANEL, padx=18, pady=14)
        prog.pack(fill="x", pady=(16, 0))
        top = tk.Frame(prog, bg=PANEL)
        top.pack(fill="x")
        self.status_label = tk.Label(top, text="Ready.", bg=PANEL, fg=TEXT, font=(FONT, 10, "bold"),
                                     anchor="w")
        self.status_label.pack(side="left", fill="x", expand=True)
        self.detail_label = tk.Label(top, text="", bg=PANEL, fg=MUTED, font=(FONT, 9))
        self.detail_label.pack(side="right")
        self.progress = ttk.Progressbar(prog, style="Accent.Horizontal.TProgressbar", maximum=100)
        self.progress.pack(fill="x", pady=(10, 0))

        actions = ttk.Frame(bottom)
        actions.pack(fill="x", pady=(14, 0))
        self.log_toggle = tk.Label(actions, text="▸ Show log", bg=BG, fg=MUTED, font=(FONT, 9), cursor="hand2")
        self.log_toggle.pack(side="left")
        self.log_toggle.bind("<Button-1>", lambda e: self.toggle_log())
        self.btn_start = ttk.Button(actions, text="Create CHD", style="Accent.TButton", command=self.start)
        self.btn_start.pack(side="right")
        self.btn_cancel = ttk.Button(actions, text="Cancel", style="Danger.TButton", command=self.cancel,
                                     state="disabled")
        self.btn_cancel.pack(side="right", padx=10)

        self.log_frame = tk.Frame(bottom, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        self.log = tk.Text(self.log_frame, height=6, bg=CARD, fg=MUTED, insertbackground=TEXT, relief="flat",
                           font=("Cascadia Mono", 9), wrap="word", padx=10, pady=8, state="disabled")
        self.log.pack(fill="both", expand=True)
        self.update_out_state()

    # -------------------------------------------------------------- UI logic

    def show_tab(self, key):
        if self.worker and self.worker.is_alive():
            return
        self.current_tab = key
        for k, t in self.tabs.items():
            active = k == key
            t.configure(bg=ACCENT if active else PANEL, fg=ON_ACCENT if active else MUTED,
                        font=(FONT, 10, "bold" if active else "normal"))
        self.page_files.pack_forget()
        self.page_disc.pack_forget()
        (self.page_files if key == "files" else self.page_disc).pack(fill="both", expand=True)
        self.update_out_state()
        self.update_start_state()

    def toggle_log(self):
        if self.log_frame.winfo_ismapped():
            self.log_frame.pack_forget()
            self.log_toggle.configure(text="▸ Show log")
        else:
            self.log_frame.pack(fill="x", pady=(10, 0))
            self.log_toggle.configure(text="▾ Hide log")

    def write_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_status(self, text, detail="", pct=None):
        self.status_label.configure(text=text)
        self.detail_label.configure(text=detail)
        if pct is not None:
            self.progress["value"] = pct

    def update_out_state(self):
        files_mode = getattr(self, "current_tab", "files") == "files"
        self.out_entry.configure(state="disabled" if files_mode and self.same_dir.get() else "normal")
        self.chk_same.configure(state="normal" if files_mode else "disabled")

    def use_out_dir(self, event=None):
        """Picking or clicking the output folder means "save there", not next to the source."""
        if self.same_dir.get() and not (self.worker and self.worker.is_alive()):
            self.same_dir.set(False)
            self.update_out_state()
            self.out_entry.focus_set()

    def update_empty_hint(self):
        if self.jobs:
            self.empty_hint.place_forget()
        else:
            self.empty_hint.place(relx=0.5, rely=0.55, anchor="center")
        self.update_start_state()

    def update_start_state(self):
        busy = bool(self.worker and self.worker.is_alive())
        if busy:
            self.btn_start.configure(state="disabled")
        elif getattr(self, "current_tab", "files") == "files":
            ready = any(not j.error and not j.done for j in self.jobs)
            self.btn_start.configure(state="normal" if ready else "disabled", text="Convert")
        else:
            self.btn_start.configure(state="normal" if self.disc_info else "disabled", text="Rip & create CHD")

    def set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for b in (self.btn_add, self.btn_add_dir, self.btn_remove, self.btn_clear, self.btn_read,
                  self.btn_refresh, self.btn_browse, self.chk_same):
            b.configure(state=state)
        self.drive_combo.configure(state="disabled" if busy else "readonly")
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        if busy:
            self.btn_start.configure(state="disabled")
        else:
            self.update_out_state()
            self.update_start_state()

    # ----------------------------------------------------------- file queue

    def pick_files(self):
        if self.worker and self.worker.is_alive():
            return
        paths = filedialog.askopenfilenames(
            title="Choose games",
            filetypes=[("Disc images", "*.cue *.iso *.chd"), ("CUE files", "*.cue"), ("ISO files", "*.iso"),
                       ("CHD files", "*.chd"), ("All files", "*.*")])
        self.add_paths(paths)

    def pick_folder(self):
        folder = filedialog.askdirectory(title="Choose a folder with games")
        if folder:
            self.add_paths([folder])

    def add_paths(self, paths):
        if self.worker and self.worker.is_alive():
            return
        if paths and self.current_tab != "files":
            self.show_tab("files")
        found = []
        for p in map(Path, paths):
            if p.is_dir():
                found += sorted(f for f in p.rglob("*") if f.suffix.lower() in IMAGE_TYPES)
            elif p.suffix.lower() in IMAGE_TYPES:
                found.append(p)
            elif p.suffix.lower() == ".bin":
                cue = p.with_suffix(".cue")
                if cue.exists():
                    found.append(cue)
                else:
                    messagebox.showwarning("Missing .cue",
                                           f"{p.name} has no .cue file next to it.\n\n"
                                           "chdman needs the .cue file to know the track layout.")
        known = {j.source.resolve() for j in self.jobs}
        for f in found:
            if f.resolve() not in known:
                known.add(f.resolve())
                self.add_job(f)
        self.update_empty_hint()

    def add_job(self, path):
        kind = "cd"
        error = None
        if path.suffix.lower() == ".cue":
            try:
                cue = discio.parse_cue(path)
                size = cue.size
                if cue.missing:
                    error = f"Missing {cue.missing[0]}"
                elif not cue.files:
                    error = "Invalid .cue file"
            except OSError as e:
                size, error = 0, str(e)
        elif path.suffix.lower() == ".chd":
            size = path.stat().st_size
            kind = chd_kind(path)
            if not kind:
                kind, error = "cd", "Not a CD/DVD CHD"
        else:
            size = path.stat().st_size
            # PS2/PSP ISOs are DVDs; a CD-sized ISO is an old-style PS1 dump
            kind = "dvd" if size > 900 * 1024 * 1024 else "cd"
        job = Job(path, kind, path.stem, size, error=error)
        # The serial sits inside the image's filesystem, which a compressed .chd doesn't expose
        if not error and not job.extract:
            job.serial, _ = discio.image_serial(path)
        target = (".iso" if kind == "dvd" else ".bin/.cue") if job.extract else ".chd"
        job.iid = self.tree.insert("", "end", text="  " + job.name,
                                   values=(job.serial or "—", kind.upper(), f"{path.suffix.lower()} → {target}",
                                           human_size(size), error or "Waiting"),
                                   tags=("err",) if error else ())
        self.jobs.append(job)

    def remove_selected(self):
        if self.worker and self.worker.is_alive():
            return
        sel = set(self.tree.selection())
        self.jobs = [j for j in self.jobs if j.iid not in sel]
        self.tree.delete(*sel)
        self.update_empty_hint()

    def clear_jobs(self):
        if self.worker and self.worker.is_alive():
            return
        self.tree.delete(*self.tree.get_children())
        self.jobs.clear()
        self.update_empty_hint()

    def set_job_status(self, job, text, tag=None):
        self.tree.set(job.iid, "status", text)
        self.tree.item(job.iid, tags=(tag,) if tag else ())

    # ----------------------------------------------------------------- disc

    def refresh_drives(self):
        drives = discio.list_optical_drives()
        self.drive_combo["values"] = drives or ["No CD/DVD drive found"]
        self.drive_var.set(drives[0] if drives else "No CD/DVD drive found")
        self.drives = drives
        self.reset_disc_info()
        if drives:
            threading.Thread(target=self._label_drives, args=(drives,), daemon=True).start()

    def _label_drives(self, drives):
        labels = []
        for d in drives:
            try:
                with discio.OpticalDrive(d) as drv:
                    labels.append(f"{d}  {drv.inquiry()}")
            except Exception:
                labels.append(d)
        self.events.put(("drive_labels", labels))

    def selected_drive(self):
        value = self.drive_var.get()
        return value[:2] if value[:2] in self.drives else None

    def reset_disc_info(self):
        self.disc_info = None
        for v in self.disc_fields.values():
            v.configure(text="—")
        self.update_start_state()

    def read_disc(self):
        drive = self.selected_drive()
        if not drive:
            messagebox.showinfo("No drive", "No CD/DVD drive was found on this computer.")
            return
        self.reset_disc_info()
        self.disc_status.configure(text=f"Reading the disc in {drive}…", foreground=MUTED)
        self.btn_read.configure(state="disabled")

        def work():
            try:
                self.events.put(("disc_info", discio.read_disc_info(drive)))
            except Exception as e:
                self.events.put(("disc_error", str(e)))
        threading.Thread(target=work, daemon=True).start()

    def show_disc_info(self, info):
        self.disc_info = info
        self.disc_fields["type"].configure(text="DVD" if info.is_dvd else "CD")
        self.disc_fields["serial"].configure(text=info.serial or "unknown")
        self.disc_fields["volume"].configure(text=info.volume or "—")
        self.disc_fields["size"].configure(text=human_size(info.size))
        self.disc_status.configure(text=info.summary + ("" if info.serial else
                                   " · no PlayStation signature found, but the disc can still be ripped."),
                                   foreground=SUCCESS if info.serial else WARN)
        self.disc_name.set(safe_filename(" ".join(x for x in (info.volume, f"[{info.serial}]" if info.serial
                                                              else None) if x) or "Game"))
        self.btn_read.configure(state="normal")
        self.update_start_state()

    # ---------------------------------------------------------------- run

    def output_for(self, job):
        folder = job.source.parent if self.current_tab == "files" and self.same_dir.get() else Path(self.out_dir.get())
        suffix = (".iso" if job.kind == "dvd" else ".cue") if job.extract else ".chd"
        return folder / (safe_filename(job.name) + suffix)

    def start(self):
        if not CHDMAN.exists():
            messagebox.showerror("chdman missing", f"Cannot find {CHDMAN}")
            return
        if not (self.current_tab == "files" and self.same_dir.get()) and not self.out_dir.get().strip():
            messagebox.showwarning("No folder", "Choose a folder to save to.")
            return
        self.save_settings()
        self.cancel_flag.clear()

        if self.current_tab == "files":
            jobs = [j for j in self.jobs if not j.error and not j.done]
            for j in jobs:
                self.set_job_status(j, "Waiting")
            target, args = self.run_files, (jobs,)
        else:
            if not self.disc_info:
                return
            name = safe_filename(self.disc_name.get())
            out_dir = Path(self.out_dir.get())
            needed = self.disc_info.size * 1.7  # raw image + compressed CHD
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                free = shutil.disk_usage(out_dir).free
            except OSError as e:
                messagebox.showerror("Error", str(e))
                return
            if free < needed and not messagebox.askyesno(
                    "Low disk space", f"Only {human_size(free)} is free but ripping needs "
                                        f"around {human_size(needed)}. Continue anyway?"):
                return
            target, args = self.run_disc, (self.disc_info, out_dir, name, self.keep_rip.get())

        self.set_busy(True)
        self.worker = threading.Thread(target=self.guarded, args=(target, *args), daemon=True)
        self.worker.start()

    def cancel(self):
        self.cancel_flag.set()
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
        self.set_status("Cancelling…")

    def guarded(self, target, *args):
        try:
            target(*args)
        except Exception as e:  # never leave the UI stuck in busy state
            self.events.put(("log", f"Unexpected error: {e!r}"))
            self.events.put(("status", f"Error: {e}", "", 0))
        finally:
            self.proc = None
            self.events.put(("finished",))

    def run_chdman(self, args, label, base_pct=0.0, span=100.0):
        """Run chdman, streaming its percentage into the progress bar. Returns (ok, last_line)."""
        cmd = [str(CHDMAN), *map(str, args)]
        self.events.put(("log", "> " + subprocess.list2cmdline(cmd)))
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        buf, last, ratio = b"", "", ""
        while True:
            chunk = self.proc.stdout.read1(512)
            if not chunk:
                break
            buf += chunk
            *lines, buf = re.split(rb"[\r\n]", buf)
            for raw in lines:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                m = re.search(r"([\d.]+)% complete", line)
                if m:
                    r = re.search(r"ratio=([\d.]+%)", line)
                    ratio = f"compression {r.group(1)}" if r else ratio
                    self.events.put(("status", label, ratio, base_pct + float(m.group(1)) * span / 100))
                else:
                    self.events.put(("log", line))
                    # chdman ends failures with a generic "Fatal error occurred: 1"; keep the real cause
                    if not line.startswith("Fatal error occurred"):
                        last = line
        code = self.proc.wait()
        if buf.strip():
            line = buf.decode("utf-8", "replace").strip()
            self.events.put(("log", line))
            if not line.startswith("Fatal error occurred"):
                last = line
        # "Error reading CHD file (C:\long\path.chd): Decompression error" -> drop the path
        return code == 0 and not self.cancel_flag.is_set(), re.sub(r" \(.*\): ", ": ", last)

    def convert(self, job, out, prefix="", start_pct=0.0):
        """Create and verify the CHD for a job, replacing any existing file; progress fills start_pct..100.

        Raises on failure or InterruptedError on cancel.
        """
        out.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp name so a failed or cancelled run never leaves a broken .chd behind
        tmp = out.with_name(out.stem + ".tmp.chd")
        args = ["createdvd" if job.kind == "dvd" else "createcd", "-i", job.source, "-o", tmp, "-f"]
        if job.kind == "dvd":
            args += ["-hs", "2048"]  # PCSX2's recommended hunk size for fast seeking
        room = 100 - start_pct
        create_span = room * 0.85
        ok, last = self.run_chdman(args, f"{prefix}Compressing {job.name}…", start_pct, create_span)
        if not ok:
            tmp.unlink(missing_ok=True)
            if self.cancel_flag.is_set():
                raise InterruptedError
            raise RuntimeError(last or "chdman failed")
        ok, last = self.run_chdman(["verify", "-i", tmp], f"{prefix}Verifying {job.name}…",
                                   start_pct + create_span, room - create_span)
        if not ok:
            tmp.unlink(missing_ok=True)
            if self.cancel_flag.is_set():
                raise InterruptedError
            raise RuntimeError("Verification failed: " + last)
        os.replace(tmp, out)

    def extract(self, job, out, prefix=""):
        """Verify a .chd, then turn it back into .bin/.cue (CD) or .iso (DVD). Same contract as convert()."""
        # Verify first so a corrupt CHD never produces a broken image
        ok, last = self.run_chdman(["verify", "-i", job.source], f"{prefix}Verifying {job.name}…", 0, 40)
        if not ok:
            if self.cancel_flag.is_set():
                raise InterruptedError
            raise RuntimeError("Verification failed: " + last)
        outputs = job.outputs(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        # Extract into a fresh temp folder and move the finished files in, so a failed or cancelled
        # run never leaves a half-written image behind (the .cue refers to the .bin by name only)
        tmp = Path(tempfile.mkdtemp(prefix=".extract-", dir=out.parent))
        try:
            if job.kind == "dvd":
                args = ["extractdvd", "-i", job.source, "-o", tmp / out.name]
            else:
                args = ["extractcd", "-i", job.source, "-o", tmp / out.name, "-ob", tmp / outputs[1].name]
            ok, last = self.run_chdman(args, f"{prefix}Extracting {job.name}…", 40, 60)
            if not ok:
                if self.cancel_flag.is_set():
                    raise InterruptedError
                raise RuntimeError(last or "chdman failed")
            for f in outputs:
                os.replace(tmp / f.name, f)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def run_files(self, jobs):
        done = failed = 0
        saved = 0
        for i, job in enumerate(jobs):
            if self.cancel_flag.is_set():
                break
            out = self.output_for(job)
            self.events.put(("job", job, "Extracting…" if job.extract else "Compressing…", "run"))
            try:
                step = self.extract if job.extract else self.convert
                step(job, out, f"[{i + 1}/{len(jobs)}] " if len(jobs) > 1 else "")
            except InterruptedError:
                self.events.put(("job", job, "Cancelled", "warn"))
                break
            except Exception as e:
                failed += 1
                self.events.put(("job", job, f"Error: {e}", "err"))
                self.events.put(("log", f"Error for {job.name}: {e}"))
                continue
            job.done = True
            done += 1
            if job.extract:
                size = sum(f.stat().st_size for f in job.outputs(out))
                self.events.put(("job", job, f"Done · {human_size(size)}", "ok"))
                self.events.put(("log", f"Done: {out}"))
                continue
            chd = out.stat().st_size
            saved += max(job.size - chd, 0)
            pct = max(100 - chd * 100 / job.size, 0) if job.size else 0
            self.events.put(("job", job, f"Done · {human_size(chd)} (−{pct:.0f}%)", "ok"))
            self.events.put(("log", f"Done: {out}"))

        if self.cancel_flag.is_set():
            self.events.put(("status", "Cancelled.", f"{done} done", 0))
        else:
            parts = [f"{done} done"] + ([f"{failed} failed"] if failed else [])
            self.events.put(("status", "Finished! " + ", ".join(parts) + ".",
                             f"saved {human_size(saved)}" if saved else "", 100))

    def run_disc(self, info, out_dir, name, keep):
        base = out_dir / name
        out = base.with_suffix(".chd")
        span = 60.0
        self.events.put(("log", f"Ripping {info.drive} ({info.summary}) to {base}"))

        def progress(done, total, bps):
            speed = bps / (176400 if not info.is_dvd else 1385000)
            eta = (total - done) * (discio.DVD_SECTOR if info.is_dvd else discio.RAW_SECTOR) / max(bps, 1)
            self.events.put(("status", f"Reading disc… {done * 100 / total:.0f}%",
                             f"{bps / 1048576:.1f} MB/s ({speed:.1f}x) · {int(eta // 60)}:{int(eta % 60):02d} left",
                             done * span / total))

        image = None
        try:
            image, bad = discio.rip_disc(info, base, progress, self.cancel_flag.is_set)
            if bad:
                self.events.put(("log", f"Warning: {bad} sectors could not be read and were filled with zeros."))
            job = Job(image, "dvd" if info.is_dvd else "cd", name, info.size)
            self.convert(job, out, start_pct=span)
            msg = f"Finished! {out.name} · {human_size(out.stat().st_size)}"
            self.events.put(("status", msg, f"{bad} unreadable sectors" if bad else "", 100))
            self.events.put(("log", f"Done: {out}"))
            if bad:
                self.events.put(("warn", "Read errors",
                                 f"{bad} sectors could not be read and were filled with zeros.\n\n"
                                 "Clean the disc and try again if the game does not work."))
        except InterruptedError:
            self.events.put(("status", "Cancelled.", "", 0))
        except Exception as e:
            self.events.put(("status", f"Error: {e}", "", 0))
            self.events.put(("log", f"Error: {e}"))
        finally:
            if image and (not keep or self.cancel_flag.is_set()):
                for ext in (".bin", ".cue", ".iso"):
                    base.with_suffix(ext).unlink(missing_ok=True)
            elif not image:
                for ext in (".bin", ".iso"):
                    base.with_suffix(ext).unlink(missing_ok=True)

    # -------------------------------------------------------------- events

    def poll_events(self):
        try:
            while True:
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "status":
                    self.set_status(*ev[1:])
                elif kind == "log":
                    self.write_log(ev[1])
                elif kind == "job":
                    self.set_job_status(*ev[1:])
                elif kind == "drop":
                    self.add_paths(ev[1])
                elif kind == "warn":
                    messagebox.showwarning(ev[1], ev[2])
                elif kind == "finished":
                    self.set_busy(False)
                elif kind == "disc_info":
                    self.show_disc_info(ev[1])
                elif kind == "disc_error":
                    self.disc_status.configure(text=ev[1], foreground=ERROR)
                    self.btn_read.configure(state="normal")
                elif kind == "drive_labels":
                    current = self.selected_drive()
                    self.drive_combo["values"] = ev[1]
                    for label in ev[1]:
                        if label.startswith(current or "?"):
                            self.drive_var.set(label)
        except queue.Empty:
            pass
        self.root.after(80, self.poll_events)

    # ------------------------------------------------------------ settings

    def detect_chdman(self):
        if not CHDMAN.exists():
            self.version_label.configure(text="chdman missing!", foreground=ERROR)
            return None
        try:
            out = subprocess.run([str(CHDMAN)], capture_output=True, text=True, creationflags=NO_WINDOW,
                                 timeout=10).stdout
            m = re.search(r"manager ([\d.]+)", out)
            version = m.group(1) if m else "?"
        except (OSError, subprocess.TimeoutExpired):
            version = "?"
        self.version_label.configure(text=f"chdman {version}")
        return version

    def pick_out_dir(self):
        folder = filedialog.askdirectory(title="Save CHD files to", initialdir=self.out_dir.get() or None)
        if folder:
            self.out_dir.set(os.path.normpath(folder))
            self.use_out_dir()

    def open_out_dir(self):
        if self.current_tab == "files" and self.same_dir.get():
            sel = self.tree.selection() or [j.iid for j in self.jobs[:1]]
            job = next((j for j in self.jobs if j.iid in sel), None)
            folder = job.source.parent if job else None
        else:
            folder = Path(self.out_dir.get())
            folder.mkdir(parents=True, exist_ok=True)
        if folder and folder.exists():
            os.startfile(folder)

    def load_settings(self):
        for f in (SETTINGS_FILE, LEGACY_SETTINGS_FILE):
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        return {}

    def save_settings(self):
        # Only remember a folder the user picked, so the default follows the .exe if it's moved
        out_dir = self.out_dir.get() if self.out_dir.get() != self.default_out else None
        data = {"out_dir": out_dir, "same_dir": self.same_dir.get(), "keep_rip": self.keep_rip.get()}
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Cancel?", "A conversion is in progress. Cancel it and close?"):
                return
            self.cancel()
            time.sleep(0.3)
        self.save_settings()
        self.root.destroy()


def main():
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PlaystationDiscDumper")
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
