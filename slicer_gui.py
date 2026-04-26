"""
slicer_gui.py — standalone launcher for the Slicer.

Usage
-----
    python slicer_gui.py [path/to/frame.png]

If a PNG path is given on the command line the slicer opens immediately.
Drag-and-drop onto the launcher requires the optional tkinterdnd2 package:
    pip install tkinterdnd2

Multiple SlicerWindows can be open simultaneously.
The process stays alive until the launcher itself is closed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _HAS_DND = True
except ImportError:
    TkinterDnD = None  # type: ignore[assignment,misc]
    DND_FILES  = None
    _HAS_DND   = False

from constants import (
    BG, BG_PANEL, BG_CARD, BG_SEL,
    FG, FG_DIM, ACCENT,
)
from slicer_window import SlicerWindow

# ── prefs ─────────────────────────────────────────────────────────────────────

_PREFS_PATH  = Path.home() / ".gooey-sprites" / "prefs.json"
_MAX_RECENT  = 12
_DEFAULT_OUT = (Path.home() / "Documents"
                if (Path.home() / "Documents").exists()
                else Path.home())


def _load_prefs() -> dict:
    try:
        return json.loads(_PREFS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_prefs(prefs: dict) -> None:
    try:
        _PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PREFS_PATH.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── app ───────────────────────────────────────────────────────────────────────

class SlicerApp:
    """Launcher window — persists for the lifetime of the process."""

    def __init__(self, root: tk.Tk, open_path: Path | None = None):
        self._root = root
        root.title("Slicer")
        root.configure(bg=BG)
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", root.destroy)

        prefs = _load_prefs()
        saved_out = prefs.get("slicer_launcher_out", str(_DEFAULT_OUT))
        self._v_out = tk.StringVar(value=saved_out)
        self._recent: list[str] = prefs.get("slicer_recent", [])

        self._build()

        if open_path is not None:
            root.after(100, lambda: self._open_frame(open_path))

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build(self):
        PAD = 10

        # ── output dir ───────────────────────���────────────────────────────────
        out_f = tk.Frame(self._root, bg=BG_PANEL, padx=PAD, pady=8)
        out_f.pack(fill=tk.X)

        tk.Label(out_f, text="Project output dir", bg=BG_PANEL, fg=FG_DIM,
                 font=("", 7, "bold")).pack(anchor=tk.W)
        row = tk.Frame(out_f, bg=BG_PANEL)
        row.pack(fill=tk.X, pady=(2, 0))
        tk.Entry(row, textvariable=self._v_out, width=36,
                 bg=BG_CARD, fg=FG, insertbackground=FG,
                 font=("Consolas", 8), relief=tk.FLAT).pack(side=tk.LEFT)
        tk.Button(row, text="Browse…", command=self._browse_out,
                  bg=BG_CARD, fg=FG_DIM, relief=tk.FLAT,
                  padx=6, pady=2, font=("", 8),
                  cursor="hand2").pack(side=tk.LEFT, padx=(4, 0))

        # ── open button + drop zone ──────────────────────────────────��────────
        mid = tk.Frame(self._root, bg=BG, padx=PAD, pady=PAD)
        mid.pack(fill=tk.X)

        tk.Button(mid, text="Open Frame…", command=self._cmd_open,
                  bg=ACCENT, fg=BG,
                  activebackground=BG_SEL, activeforeground=FG,
                  relief=tk.FLAT, padx=12, pady=8,
                  font=("", 11, "bold"),
                  cursor="hand2").pack(fill=tk.X)
        tk.Button(mid, text="Open Project (.slicer.json)…",
                  command=self._cmd_open_project,
                  bg=BG_CARD, fg=FG_DIM,
                  activebackground=BG_SEL, activeforeground=FG,
                  relief=tk.FLAT, padx=12, pady=5,
                  font=("", 9),
                  cursor="hand2").pack(fill=tk.X, pady=(4, 0))

        if _HAS_DND:
            drop_text = "— or drop a PNG here —"
            drop_cursor = "hand2"
        else:
            drop_text = "— or drop a PNG here —\n(pip install tkinterdnd2 to enable)"
            drop_cursor = "arrow"

        self._drop_zone = tk.Label(
            mid, text=drop_text,
            bg=BG_CARD, fg=FG_DIM, font=("", 8),
            relief=tk.FLAT, padx=10, pady=18,
            cursor=drop_cursor)
        self._drop_zone.pack(fill=tk.X, pady=(8, 0))

        if _HAS_DND:
            self._drop_zone.drop_target_register(DND_FILES)
            self._drop_zone.dnd_bind("<<Drop>>", self._on_drop)
            self._drop_zone.bind(
                "<Enter>",
                lambda _: self._drop_zone.config(bg=BG_SEL, fg=ACCENT))
            self._drop_zone.bind(
                "<Leave>",
                lambda _: self._drop_zone.config(bg=BG_CARD, fg=FG_DIM))

        # ── recent files ──────────────────────────────────────────────────────
        rec_f = tk.Frame(self._root, bg=BG, padx=PAD)
        rec_f.pack(fill=tk.BOTH, expand=True, pady=(0, PAD))

        hdr = tk.Frame(rec_f, bg=BG)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="Recent", bg=BG, fg=FG_DIM,
                 font=("", 7, "bold")).pack(side=tk.LEFT)
        tk.Button(hdr, text="Clear", command=self._clear_recent,
                  bg=BG, fg=FG_DIM, relief=tk.FLAT,
                  font=("", 7), padx=4, pady=0,
                  cursor="hand2").pack(side=tk.RIGHT)

        lf = tk.Frame(rec_f, bg=BG)
        lf.pack(fill=tk.BOTH, expand=True, pady=(2, 0))
        vsb = tk.Scrollbar(lf, orient=tk.VERTICAL, bg=BG_CARD,
                           troughcolor=BG, relief=tk.FLAT)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._lb = tk.Listbox(
            lf, yscrollcommand=vsb.set, height=10,
            bg=BG_CARD, fg=FG,
            selectbackground=BG_SEL, selectforeground=ACCENT,
            font=("Consolas", 8), relief=tk.FLAT, activestyle="none")
        self._lb.pack(fill=tk.BOTH, expand=True)
        vsb.config(command=self._lb.yview)

        self._lb.bind("<Double-Button-1>", self._open_recent)
        self._lb.bind("<Return>",          self._open_recent)

        # tooltip showing full path on hover
        self._lb.bind("<Motion>",  self._lb_motion)
        self._lb.bind("<Leave>",   lambda _: self._hide_lb_tip())
        self._lb_tip: tk.Toplevel | None = None
        self._lb_tip_idx: int = -1

        self._rebuild_recent()

    # ── recent list ───────────────────────────────────────────────────────────

    def _rebuild_recent(self):
        self._lb.delete(0, tk.END)
        for p_str in self._recent:
            p = Path(p_str)
            self._lb.insert(tk.END, f"  {p.parent.name} / {p.name}")
            self._lb.itemconfig(tk.END, fg=(FG if p.exists() else FG_DIM))

    def _push_recent(self, path: Path):
        s = str(path)
        if s in self._recent:
            self._recent.remove(s)
        self._recent.insert(0, s)
        self._recent = self._recent[:_MAX_RECENT]
        prefs = _load_prefs()
        prefs["slicer_recent"]      = self._recent
        prefs["slicer_launcher_out"] = self._v_out.get()
        _save_prefs(prefs)
        self._rebuild_recent()

    def _clear_recent(self):
        self._recent.clear()
        prefs = _load_prefs()
        prefs["slicer_recent"] = []
        _save_prefs(prefs)
        self._rebuild_recent()

    # ── listbox path tooltip ──────────────────────────────────────────────────

    def _lb_motion(self, event):
        idx = self._lb.nearest(event.y)
        if idx == self._lb_tip_idx:
            return
        self._hide_lb_tip()
        self._lb_tip_idx = idx
        if idx < 0 or idx >= len(self._recent):
            return
        win = tk.Toplevel(self._root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BG_CARD)
        tk.Label(win, text=self._recent[idx],
                 bg=BG_CARD, fg=FG, font=("Consolas", 8),
                 padx=6, pady=4).pack()
        win.update_idletasks()
        win.geometry(f"+{event.x_root + 14}+{event.y_root - win.winfo_height() // 2}")
        self._lb_tip = win

    def _hide_lb_tip(self):
        if self._lb_tip:
            try:
                self._lb_tip.destroy()
            except Exception:
                pass
            self._lb_tip = None
        self._lb_tip_idx = -1

    # ── actions ───────────────────────────────────────────────────────────────

    def _browse_out(self):
        chosen = filedialog.askdirectory(
            title="Choose project output folder",
            initialdir=self._v_out.get(),
            mustexist=False,
            parent=self._root)
        if chosen:
            self._v_out.set(chosen)
            prefs = _load_prefs()
            prefs["slicer_launcher_out"] = chosen
            _save_prefs(prefs)

    def _cmd_open(self):
        prefs   = _load_prefs()
        initial = prefs.get("slicer_last_open_dir", str(Path.home()))
        path    = filedialog.askopenfilename(
            title="Open source frame",
            initialdir=initial,
            filetypes=[("PNG images", "*.png"), ("All files", "*.*")],
            parent=self._root)
        if not path:
            return
        p = Path(path)
        prefs["slicer_last_open_dir"] = str(p.parent)
        _save_prefs(prefs)
        self._open_frame(p)

    def _open_recent(self, _event=None):
        sel = self._lb.curselection()
        if not sel:
            return
        p = Path(self._recent[sel[0]])
        if not p.exists():
            messagebox.showwarning("File Not Found",
                                   f"'{p}' no longer exists.",
                                   parent=self._root)
            self._rebuild_recent()
            return
        self._open_frame(p)

    def _cmd_open_project(self):
        prefs   = _load_prefs()
        initial = prefs.get("slicer_last_project_dir", str(Path.home()))
        path    = filedialog.askopenfilename(
            title="Open Slicer Project",
            initialdir=initial,
            filetypes=[("Slicer JSON", "*.slicer.json *.json"),
                       ("All files", "*.*")],
            parent=self._root)
        if not path:
            return
        slicer_json = Path(path)
        prefs["slicer_last_project_dir"] = str(slicer_json.parent)
        _save_prefs(prefs)

        # Locate source PNG from the slicer.json's "source_frame" field.
        # Try the conventional relative path first; fall back to a file dialog.
        try:
            data = json.loads(slicer_json.read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("Open Project",
                                 f"Cannot read slicer file:\n{exc}",
                                 parent=self._root)
            return

        source_name = data.get("source_frame", "")
        png_path: Path | None = None

        if source_name:
            # Conventional layout: <anim_dir>/slices/<stem>/slicer.json
            # PNG lives at: <anim_dir>/<source_frame>
            for candidate in [
                slicer_json.parent / source_name,
                slicer_json.parent.parent / source_name,
                slicer_json.parent.parent.parent / source_name,
            ]:
                if candidate.exists():
                    png_path = candidate
                    break

        if png_path is None:
            # Couldn't find automatically — ask the user
            label = f"Locate '{source_name}'" if source_name else "Locate source frame PNG"
            found = filedialog.askopenfilename(
                title=label,
                initialdir=str(slicer_json.parent),
                filetypes=[("PNG images", "*.png"), ("All files", "*.*")],
                parent=self._root)
            if not found:
                return
            png_path = Path(found)

        self._open_frame(png_path, slicer_json=slicer_json)

    def _on_drop(self, event):
        # tkinterdnd2 delivers brace-quoted paths; tk.splitlist handles them
        for p_str in self._root.tk.splitlist(event.data.strip()):
            p = Path(p_str)
            if p.suffix.lower() == ".png" and p.exists():
                self._open_frame(p)
            elif p.suffix.lower() in (".json",) and p.exists():
                # Allow dropping a slicer.json directly
                self._cmd_open_project_from_path(p)

    def _cmd_open_project_from_path(self, slicer_json: Path):
        """Open project from a slicer.json path (used by drag-and-drop)."""
        # Re-use cmd logic by temporarily faking the dialog result
        prefs = _load_prefs()
        prefs["slicer_last_project_dir"] = str(slicer_json.parent)
        _save_prefs(prefs)

        try:
            data = json.loads(slicer_json.read_text(encoding="utf-8"))
        except Exception:
            return

        source_name = data.get("source_frame", "")
        png_path: Path | None = None
        if source_name:
            for candidate in [
                slicer_json.parent / source_name,
                slicer_json.parent.parent / source_name,
                slicer_json.parent.parent.parent / source_name,
            ]:
                if candidate.exists():
                    png_path = candidate
                    break
        if png_path:
            self._open_frame(png_path, slicer_json=slicer_json)

    def _open_frame(self, path: Path, slicer_json: Path | None = None):
        out_dir = Path(self._v_out.get().strip() or str(_DEFAULT_OUT))
        out_dir.mkdir(parents=True, exist_ok=True)
        SlicerWindow(self._root, path, out_dir, slicer_json=slicer_json)
        self._push_recent(path)


# ── entry point ──────────────────────────────��────────────────────────────────

def main() -> None:
    open_path: Path | None = None
    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
        if p.exists() and p.suffix.lower() == ".png":
            open_path = p
        else:
            print(f"Warning: '{sys.argv[1]}' is not a valid PNG — ignoring.",
                  file=sys.stderr)

    root = TkinterDnD.Tk() if _HAS_DND else tk.Tk()
    SlicerApp(root, open_path)
    root.geometry("420x560")
    root.mainloop()


if __name__ == "__main__":
    main()
