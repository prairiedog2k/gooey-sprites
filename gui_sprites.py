#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui_sprites.py — GUI front-end for extract_sprites.py

Usage:
  python gui_sprites.py
  python gui_sprites.py path/to/sheet.gif
  python gui_sprites.py path/to/sheet.gif --output ./sprites
  python gui_sprites.py myproject.ssproj

Project files (.ssproj) are JSON and store the GIF path, output folder,
and extraction settings so a session can be resumed in one click.
"""

import argparse
import json
import sys
import threading
from pathlib import Path

PROJECT_EXT     = ".ssproj"
PROJECT_VERSION = 1

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk

# ── colours / metrics ─────────────────────────────────────────────────────────

BG        = "#1e1e2e"
BG_PANEL  = "#181825"
BG_CARD   = "#262637"
BG_SEL    = "#313166"
FG        = "#cdd6f4"
FG_DIM    = "#6c7086"
ACCENT    = "#89b4fa"
RED       = "#f38ba8"
GREEN     = "#a6e3a1"
YELLOW    = "#f9e2af"

THUMB_H   = 128   # pixel height for frame thumbnails
MIN_SCALE = 1.0   # never shrink thumbnails below natural size
MAX_SCALE = 6.0   # maximum upscale for tiny sprites
CHECKER   = ((60, 60, 60), (40, 40, 40))  # transparency checker bg

# ── helpers ───────────────────────────────────────────────────────────────────

def _make_thumb(png_path: Path, scale: float) -> ImageTk.PhotoImage:
    """Load a PNG and scale it by `scale`, composited over a checker background."""
    img = Image.open(png_path).convert("RGBA")
    w = max(1, round(img.width  * scale))
    h = max(1, round(img.height * scale))
    img = img.resize((w, h), Image.NEAREST)

    # checker background
    checker = Image.new("RGBA", (w, h))
    bsize = max(8, h // 8)
    from PIL import ImageDraw
    draw = ImageDraw.Draw(checker)
    for row in range(0, h, bsize):
        for col in range(0, w, bsize):
            color = CHECKER[(row // bsize + col // bsize) % 2]
            draw.rectangle([col, row, col + bsize - 1, row + bsize - 1], fill=color)
    composited = Image.alpha_composite(checker, img)
    return ImageTk.PhotoImage(composited)


# ── project file helpers ──────────────────────────────────────────────────────

def _read_project(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version", 1) != PROJECT_VERSION:
        raise ValueError(f"Unsupported project version: {data.get('version')}")
    return data


def _write_project(path: Path, gif: str, output: str, gap: int, tol: int,
                   animations: list[str] | None = None):
    # Store paths relative to the project file so the folder can be moved.
    proj_dir = path.parent
    def _rel(p: str) -> str:
        try:
            return str(Path(p).relative_to(proj_dir))
        except ValueError:
            return p  # keep absolute if on a different drive

    data = {
        "version":    PROJECT_VERSION,
        "gif":        _rel(gif)    if gif    else "",
        "output":     _rel(output) if output else "",
        "gap":        gap,
        "tol":        tol,
        "animations": animations or [],
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _resolve_project_paths(data: dict, proj_dir: Path) -> tuple[str, str]:
    """Return (gif_abs, output_abs) resolving relative paths against proj_dir."""
    def _abs(p: str) -> str:
        if not p:
            return ""
        pp = Path(p)
        return str((proj_dir / pp).resolve()) if not pp.is_absolute() else p
    return _abs(data.get("gif", "")), _abs(data.get("output", ""))



# ── main application ──────────────────────────────────────────────────────────

class SpriteGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Sprite Sheet Extractor")
        self.root.configure(bg=BG)
        self.root.minsize(900, 600)

        # State
        self.v_gif    = tk.StringVar(value="")
        self.v_out    = tk.StringVar(value="./sprites")
        self.v_status = tk.StringVar(value="Ready.")
        self.v_gap    = tk.IntVar(value=4)
        self.v_tol    = tk.IntVar(value=20)

        self._anim_dirs: list[Path] = []
        self.selected_anim: Path | None = None
        self.selected_frames: set[int] = set()
        self._frame_images: list[ImageTk.PhotoImage] = []
        self._frame_cells:  list[tk.Frame] = []
        self._last_clicked: int | None = None
        self._project_path: Path | None = None   # currently open .ssproj
        self._managed_anims: list[str] = []      # folder names this project created

        # preview state
        self._pv_frames:   list[Path] = []
        self._pv_current:  int        = 0
        self._pv_playing:  bool       = False
        self._pv_after_id             = None
        self._pv_delay    = tk.IntVar(value=100)   # ms per frame
        self._pv_loop     = tk.BooleanVar(value=True)
        self._pv_photo                = None       # keep photo ref
        self._pv_toplevel: tk.Toplevel | None = None  # when detached
        self._pv_canvas:   tk.Canvas  | None = None
        self._pv_lbl_counter: tk.Label | None = None
        self._pv_btn_play:    tk.Button | None = None
        self._pv_lbl_delay:   tk.Label | None = None
        self._pv_max_w:   int = 1  # natural pixel size of widest frame
        self._pv_max_h:   int = 1  # natural pixel size of tallest frame

        # drag-to-reorder state
        self._drag_src:      int | None = None
        self._drag_dst:      int | None = None
        self._drag_active:   bool       = False
        self._drag_start_xy: tuple      = (0, 0)
        self._drag_indicator: tk.Frame | None = None

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        """Exit cleanly: cancel callbacks, stop the event loop, destroy widgets."""
        self._pv_playing = False
        if self._pv_after_id:
            try:
                self.root.after_cancel(self._pv_after_id)
            except Exception:
                pass
            self._pv_after_id = None
        self._pv_null_refs()
        try:
            self.root.quit()     # exits mainloop()
            self.root.destroy()  # releases Tcl/Tk resources
        except Exception:
            pass

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

    def _build_menu(self):
        menubar = tk.Menu(self.root, bg=BG_PANEL, fg=FG,
                          activebackground=BG_SEL, activeforeground=ACCENT,
                          relief=tk.FLAT, borderwidth=0)
        self.root.config(menu=menubar)

        file_menu = tk.Menu(menubar, tearoff=False,
                            bg=BG_PANEL, fg=FG,
                            activebackground=BG_SEL, activeforeground=ACCENT,
                            relief=tk.FLAT)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="New Project",       command=self._new_project,
                              accelerator="Ctrl+N")
        file_menu.add_command(label="Open Project…",     command=self._open_project,
                              accelerator="Ctrl+O")
        file_menu.add_separator()
        file_menu.add_command(label="Save Project",      command=self._save_project,
                              accelerator="Ctrl+S")
        file_menu.add_command(label="Save Project As…",  command=self._save_project_as,
                              accelerator="Ctrl+Shift+S")
        file_menu.add_separator()
        file_menu.add_command(label="Exit",              command=self._on_close)

        self.root.bind_all("<Control-n>", lambda _: self._new_project())
        self.root.bind_all("<Control-o>", lambda _: self._open_project())
        self.root.bind_all("<Control-s>", lambda _: self._save_project())
        self.root.bind_all("<Control-S>", lambda _: self._save_project_as())

    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg=BG_PANEL, pady=6, padx=8)
        bar.pack(fill=tk.X)

        # row 1 – file paths
        r1 = tk.Frame(bar, bg=BG_PANEL)
        r1.pack(fill=tk.X, pady=2)
        self._label(r1, "Sprite Sheet GIF:", width=16).pack(side=tk.LEFT)
        tk.Entry(r1, textvariable=self.v_gif, bg=BG_CARD, fg=FG,
                 insertbackground=FG, relief=tk.FLAT, width=55,
                 font=("Consolas", 9)).pack(side=tk.LEFT, padx=4)
        self._btn(r1, "Browse…", self._browse_gif).pack(side=tk.LEFT)

        r2 = tk.Frame(bar, bg=BG_PANEL)
        r2.pack(fill=tk.X, pady=2)
        self._label(r2, "Output Folder:", width=16).pack(side=tk.LEFT)
        tk.Entry(r2, textvariable=self.v_out, bg=BG_CARD, fg=FG,
                 insertbackground=FG, relief=tk.FLAT, width=55,
                 font=("Consolas", 9)).pack(side=tk.LEFT, padx=4)
        self._btn(r2, "Browse…", self._browse_out).pack(side=tk.LEFT)

        # row 2 – actions + options
        r3 = tk.Frame(bar, bg=BG_PANEL)
        r3.pack(fill=tk.X, pady=4)
        self._btn(r3, "Extract All",          self._extract_all,   GREEN).pack(side=tk.LEFT, padx=(0, 4))
        self._btn(r3, "Load Output Folder",   self._load_output,   ACCENT).pack(side=tk.LEFT, padx=4)
        self._label(r3, "  Gap:").pack(side=tk.LEFT)
        tk.Spinbox(r3, from_=0, to=20, textvariable=self.v_gap, width=4,
                   bg=BG_CARD, fg=FG, buttonbackground=BG_CARD,
                   relief=tk.FLAT).pack(side=tk.LEFT)
        self._label(r3, "  Tol:").pack(side=tk.LEFT)
        tk.Spinbox(r3, from_=0, to=100, textvariable=self.v_tol, width=4,
                   bg=BG_CARD, fg=FG, buttonbackground=BG_CARD,
                   relief=tk.FLAT).pack(side=tk.LEFT)

    def _build_body(self):
        pane = tk.PanedWindow(self.root, orient=tk.HORIZONTAL,
                              bg=BG, sashwidth=5, sashrelief=tk.FLAT,
                              sashpad=2)
        pane.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        # ── left: animation list ──────────────────────────────────────────────
        left = tk.Frame(pane, bg=BG_PANEL, width=220)
        pane.add(left, minsize=160)

        tk.Label(left, text="Animations", bg=BG_PANEL, fg=ACCENT,
                 font=("", 10, "bold"), pady=6).pack(fill=tk.X, padx=8)

        lf = tk.Frame(left, bg=BG_PANEL)
        lf.pack(fill=tk.BOTH, expand=True, padx=4, pady=(0, 4))

        sb = tk.Scrollbar(lf, bg=BG_CARD, troughcolor=BG_PANEL, relief=tk.FLAT)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        self.anim_list = tk.Listbox(lf, yscrollcommand=sb.set,
                                    bg=BG_CARD, fg=FG, relief=tk.FLAT,
                                    selectbackground=BG_SEL,
                                    selectforeground=ACCENT,
                                    activestyle="none",
                                    font=("Consolas", 9),
                                    borderwidth=0, highlightthickness=0)
        self.anim_list.pack(fill=tk.BOTH, expand=True)
        sb.config(command=self.anim_list.yview)
        self.anim_list.bind("<<ListboxSelect>>", self._on_anim_select)
        self.anim_list.bind("<Delete>",          lambda _: self._delete_anim())
        self.anim_list.bind("<Button-3>",        self._anim_right_click)

        # rename button under the list
        btn_row = tk.Frame(left, bg=BG_PANEL)
        btn_row.pack(pady=4)
        self._btn(btn_row, "Rename…",  self._rename_folder, YELLOW, small=True).pack(side=tk.LEFT, padx=2)
        self._btn(btn_row, "Delete",   self._delete_anim,   RED,    small=True).pack(side=tk.LEFT, padx=2)

        # ── centre: frame viewer ─────────────────────────────────────────────
        right = tk.Frame(pane, bg=BG)
        pane.add(right)

        # ── right: animation preview (built after body widgets) ───────────────
        self._pv_pane = tk.Frame(pane, bg=BG_PANEL, width=260)
        pane.add(self._pv_pane, minsize=180)

        hdr = tk.Frame(right, bg=BG_PANEL, pady=4)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="Frames", bg=BG_PANEL, fg=ACCENT,
                 font=("", 10, "bold")).pack(side=tk.LEFT, padx=8)
        self.lbl_anim = tk.Label(hdr, text="", bg=BG_PANEL,
                                 fg=FG_DIM, font=("Consolas", 9))
        self.lbl_anim.pack(side=tk.LEFT)
        self.lbl_sel = tk.Label(hdr, text="", bg=BG_PANEL,
                                fg=FG_DIM, font=("", 9))
        self.lbl_sel.pack(side=tk.RIGHT, padx=8)

        # canvas + scrollbars
        cf = tk.Frame(right, bg=BG)
        cf.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(cf, bg=BG_PANEL,
                                highlightthickness=0)
        hbar = tk.Scrollbar(cf, orient=tk.HORIZONTAL,
                            command=self.canvas.xview,
                            bg=BG_CARD, troughcolor=BG_PANEL, relief=tk.FLAT)
        vbar = tk.Scrollbar(cf, orient=tk.VERTICAL,
                            command=self.canvas.yview,
                            bg=BG_CARD, troughcolor=BG_PANEL, relief=tk.FLAT)
        self.canvas.configure(xscrollcommand=hbar.set,
                              yscrollcommand=vbar.set)
        hbar.pack(side=tk.BOTTOM, fill=tk.X)
        vbar.pack(side=tk.RIGHT,  fill=tk.Y)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.frame_holder = tk.Frame(self.canvas, bg=BG_PANEL)
        self._canvas_win = self.canvas.create_window(
            (4, 4), window=self.frame_holder, anchor=tk.NW)
        self.frame_holder.bind(
            "<Configure>",
            lambda e: self.canvas.configure(
                scrollregion=self.canvas.bbox("all")))
        # mousewheel scrolling
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        # ── bottom toolbar ─────────────────────────────────────────────────────
        bot = tk.Frame(right, bg=BG_PANEL, pady=6, padx=8)
        bot.pack(fill=tk.X)
        self._btn(bot, "Merge Selected",    self._merge_frames,           ACCENT ).pack(side=tk.LEFT, padx=4)
        self._btn(bot, "Split Selected",    self._split_frame,            RED   ).pack(side=tk.LEFT, padx=4)
        self._btn(bot, "Duplicate",         self._duplicate_frame,        YELLOW).pack(side=tk.LEFT, padx=4)
        self._btn(bot, "Delete Frame(s)",   self._delete_selected_frames, RED   ).pack(side=tk.LEFT, padx=4)
        tk.Label(bot, text="Click to select  |  Ctrl+click to add/remove  |  Shift+click to range-select",
                 bg=BG_PANEL, fg=FG_DIM, font=("", 8)).pack(side=tk.LEFT, padx=12)

        # build the preview panel into its pane
        self._build_preview_panel(self._pv_pane)

    def _build_statusbar(self):
        sb = tk.Frame(self.root, bg=BG_PANEL, pady=3)
        sb.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Label(sb, textvariable=self.v_status, bg=BG_PANEL, fg=FG_DIM,
                 font=("", 9), anchor=tk.W).pack(side=tk.LEFT, padx=8)

    # ── widget helpers ────────────────────────────────────────────────────────

    def _label(self, parent, text, width=None, **kw):
        kw.setdefault("bg",   BG_PANEL)
        kw.setdefault("fg",   FG)
        kw.setdefault("font", ("", 9))
        if width:
            kw["width"] = width
            kw["anchor"] = tk.W
        return tk.Label(parent, text=text, **kw)

    def _btn(self, parent, text, cmd, color=None, small=False):
        if color is None:
            color = FG_DIM
        font = ("", 8) if small else ("", 9)
        return tk.Button(parent, text=text, command=cmd,
                         bg=BG_CARD, fg=color, activeforeground=color,
                         activebackground=BG_SEL,
                         relief=tk.FLAT, font=font,
                         padx=8, pady=3, cursor="hand2",
                         borderwidth=0, highlightthickness=0)

    # ── file dialogs / IO ─────────────────────────────────────────────────────

    # ── project file commands ─────────────────────────────────────────────────

    def _update_title(self):
        if self._project_path:
            self.root.title(f"Sprite Sheet Extractor — {self._project_path.name}")
        else:
            self.root.title("Sprite Sheet Extractor")

    def _new_project(self):
        self._project_path = None
        self.v_gif.set("")
        self.v_out.set("./sprites")
        self.v_gap.set(4)
        self.v_tol.set(20)
        self._anim_dirs.clear()
        self.anim_list.delete(0, tk.END)
        self.selected_anim = None
        self.selected_frames.clear()
        for w in self.frame_holder.winfo_children():
            w.destroy()
        self._frame_images.clear()
        self._frame_cells.clear()
        self.lbl_anim.config(text="")
        self.lbl_sel.config(text="")
        self._managed_anims.clear()
        self._update_title()
        self._set_status("New project.")

    def _open_project(self):
        path = filedialog.askopenfilename(
            title="Open Project",
            filetypes=[("Sprite Sheet Project", f"*{PROJECT_EXT}"),
                       ("All files", "*.*")])
        if not path:
            return
        self._load_project_file(Path(path))

    def _load_project_file(self, path: Path):
        try:
            data = _read_project(path)
        except Exception as exc:
            messagebox.showerror("Open Project", f"Could not read project:\n{exc}")
            return
        gif_abs, out_abs = _resolve_project_paths(data, path.parent)
        self._project_path = path
        self.v_gif.set(gif_abs)
        self.v_out.set(out_abs or "./sprites")
        self.v_gap.set(int(data.get("gap", 4)))
        self.v_tol.set(int(data.get("tol", 20)))
        self._managed_anims = list(data.get("animations", []))
        self._update_title()
        self._set_status(f"Opened '{path.name}'.")
        self._load_output()

    def _save_project(self):
        if self._project_path is None:
            self._save_project_as()
        else:
            self._write_current_project(self._project_path)

    def _save_project_as(self):
        path = filedialog.asksaveasfilename(
            title="Save Project As",
            defaultextension=PROJECT_EXT,
            filetypes=[("Sprite Sheet Project", f"*{PROJECT_EXT}"),
                       ("All files", "*.*")])
        if not path:
            return
        self._project_path = Path(path)
        self._write_current_project(self._project_path)
        self._update_title()

    def _write_current_project(self, path: Path):
        try:
            _write_project(path,
                           gif=self.v_gif.get().strip(),
                           output=self.v_out.get().strip(),
                           gap=self.v_gap.get(),
                           tol=self.v_tol.get(),
                           animations=self._managed_anims)
            self._set_status(f"Project saved to '{path}'.")
        except Exception as exc:
            messagebox.showerror("Save Project", f"Could not save project:\n{exc}")

    def _browse_gif(self):
        p = filedialog.askopenfilename(
            filetypes=[("GIF files", "*.gif"), ("All files", "*.*")])
        if p:
            self.v_gif.set(p)

    def _browse_out(self):
        p = filedialog.askdirectory()
        if p:
            self.v_out.set(p)

    def _maybe_autoload(self):
        """If output folder already has extracted animations, load them."""
        out = Path(self.v_out.get())
        if out.exists() and any(True for d in out.iterdir()
                                if d.is_dir() and (d / "frames.json").exists()):
            self._load_output()

    def _set_status(self, msg: str):
        self.v_status.set(msg)
        self.root.update_idletasks()

    # ── extraction ────────────────────────────────────────────────────────────

    def _extract_all(self):
        gif = self.v_gif.get().strip()
        if not gif:
            messagebox.showwarning("No GIF", "Please select a sprite sheet GIF first.")
            return
        out_root = Path(self.v_out.get().strip() or "./sprites")

        # Delete all previously-managed animation folders silently.
        if out_root.exists() and self._managed_anims:
            import shutil
            for name in self._managed_anims:
                target = out_root / name
                if target.is_dir():
                    shutil.rmtree(target)

        self._set_status("Extracting… (this may take a moment)")
        tol = self.v_tol.get()
        gap = self.v_gap.get()

        def run():
            try:
                from extract_sprites import SpriteSheet, save_animation
                sheet   = SpriteSheet(gif, tol=tol)
                results = sheet.extract_all(max_intra_gap=gap)
                folders: list[str] = []
                for n, (_, sprites, frames) in enumerate(results, 1):
                    folder  = f"unknown-{n:03d}"
                    out_dir = out_root / folder
                    save_animation(out_dir, sprites, frames, gif, sheet.bg, sheet.tol)
                    folders.append(folder)
                    self.root.after(0, lambda c=len(folders), f=folder:
                                    self._set_status(f"Saved {c}: {f}"))
                self.root.after(0, lambda fl=folders:
                                self._finish_extract(str(out_root), fl))
            except Exception as exc:
                self.root.after(0, lambda e=exc: (
                    self._set_status(f"Error: {e}"),
                    messagebox.showerror("Extraction Error", str(e))
                ))

        threading.Thread(target=run, daemon=True).start()

    def _finish_extract(self, out: str, folders: list[str]):
        self._managed_anims = folders
        # Persist the animation list immediately if a project file is open.
        if self._project_path:
            self._write_current_project(self._project_path)
        self._set_status(f"Extracted {len(folders)} animations to '{out}'.")
        self._load_output()

    # ── animation list ────────────────────────────────────────────────────────

    def _load_output(self):
        out = Path(self.v_out.get().strip())
        if not out.exists():
            self._set_status(f"Output folder not found: {out}")
            return
        anims = sorted(
            d for d in out.iterdir()
            if d.is_dir() and (d / "frames.json").exists()
        )
        self._anim_dirs = anims
        self.anim_list.delete(0, tk.END)
        for a in anims:
            self.anim_list.insert(tk.END, a.name)
        self._set_status(f"Loaded {len(anims)} animation(s) from '{out}'.")

    def _on_anim_select(self, _event=None):
        sel = self.anim_list.curselection()
        if not sel:
            return
        anim_dir = self._anim_dirs[sel[0]]
        if anim_dir == self.selected_anim:
            return
        self.selected_anim = anim_dir
        self.selected_frames.clear()
        self._last_clicked = None
        self.lbl_anim.config(text=anim_dir.name)
        self._load_frames(anim_dir)
        self._pv_load(anim_dir)

    def _rename_folder(self):
        if not self.selected_anim:
            messagebox.showwarning("No Animation", "Select an animation first.")
            return
        dlg = _InputDialog(self.root, "Rename folder",
                           "New name:", self.selected_anim.name)
        new_name = dlg.result
        if not new_name or new_name == self.selected_anim.name:
            return
        new_path = self.selected_anim.parent / new_name
        if new_path.exists():
            messagebox.showerror("Rename", f"'{new_name}' already exists.")
            return
        self.selected_anim.rename(new_path)
        self.selected_anim = new_path
        self._load_output()
        # re-select the renamed item
        for i, d in enumerate(self._anim_dirs):
            if d == new_path:
                self.anim_list.selection_clear(0, tk.END)
                self.anim_list.selection_set(i)
                self.anim_list.see(i)
                break
        self.lbl_anim.config(text=new_name)
        self._set_status(f"Renamed to '{new_name}'.")

    def _delete_anim(self):
        if not self.selected_anim:
            messagebox.showwarning("No Animation", "Select an animation first.")
            return
        name = self.selected_anim.name
        if not messagebox.askyesno(
                "Delete Animation",
                f"Permanently delete '{name}' and all its files?\n\nThis cannot be undone.",
                icon="warning"):
            return
        import shutil
        shutil.rmtree(self.selected_anim)
        self.selected_anim = None
        self.selected_frames.clear()
        self._last_clicked = None
        self.lbl_anim.config(text="")
        self.lbl_sel.config(text="")
        for w in self.frame_holder.winfo_children():
            w.destroy()
        self._frame_images.clear()
        self._frame_cells.clear()
        self._load_output()
        self._set_status(f"Deleted '{name}'.")

    # ── frame viewer ──────────────────────────────────────────────────────────

    def _load_frames(self, anim_dir: Path):
        for w in self.frame_holder.winfo_children():
            w.destroy()
        self._frame_images.clear()
        self._frame_cells.clear()
        self._drag_indicator = None
        self._drag_active    = False
        self._drag_src       = None

        pngs = sorted(anim_dir.glob("*.png"))

        # All frames share the same scale so they appear at a uniform size.
        # Scale is derived from the tallest frame clamped to [MIN_SCALE, MAX_SCALE].
        if pngs:
            max_h = max(Image.open(p).height for p in pngs)
            scale = max(MIN_SCALE, min(MAX_SCALE, THUMB_H / max(max_h, 1)))
        else:
            scale = MIN_SCALE

        for i, png in enumerate(pngs):
            self._add_frame_card(i, png, scale)

        # Drag drop indicator — invisible thin bar placed over frame_holder
        self._drag_indicator = tk.Frame(self.frame_holder, bg=ACCENT,
                                        width=3, cursor="sb_h_double_arrow")
        # starts hidden (not placed)

        self._update_sel_label()
        self.canvas.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._pv_reload()

    def _add_frame_card(self, idx: int, png_path: Path, scale: float):
        try:
            photo = _make_thumb(png_path, scale)
        except Exception:
            photo = None

        card = tk.Frame(self.frame_holder, bg=BG_CARD,
                        padx=3, pady=3, cursor="hand2")
        card.grid(row=0, column=idx, sticky=tk.N, padx=3, pady=6)

        if photo:
            img_lbl = tk.Label(card, image=photo, bg=BG_CARD,
                               relief=tk.FLAT, borderwidth=0)
            img_lbl.pack()
            self._frame_images.append(photo)
        else:
            tk.Label(card, text="(err)", bg=BG_CARD, fg=RED,
                     width=6, height=4).pack()

        tk.Label(card, text=str(idx), bg=BG_CARD, fg=FG_DIM,
                 font=("Consolas", 8)).pack()

        self._frame_cells.append(card)

        for w in (card, *card.winfo_children()):
            w.bind("<Button-1>",         lambda e, i=idx: self._drag_press(e, i))
            w.bind("<B1-Motion>",        lambda e, i=idx: self._drag_motion(e, i))
            w.bind("<ButtonRelease-1>",  lambda e, i=idx: self._drag_release(e, i))
            w.bind("<Control-Button-1>", lambda e, i=idx: self._on_click(e, i, ctrl=True))
            w.bind("<Shift-Button-1>",   lambda e, i=idx: self._on_click(e, i, shift=True))
            w.bind("<Button-3>",         lambda e, i=idx: self._frame_right_click(e, i))

    def _on_click(self, _event, idx: int, ctrl=False, shift=False):
        if shift and self._last_clicked is not None:
            lo, hi = sorted((self._last_clicked, idx))
            if not ctrl:
                self.selected_frames.clear()
            self.selected_frames.update(range(lo, hi + 1))
        elif ctrl:
            if idx in self.selected_frames:
                self.selected_frames.discard(idx)
            else:
                self.selected_frames.add(idx)
        else:
            self.selected_frames = {idx}

        self._last_clicked = idx
        self._refresh_cards()
        self._update_sel_label()

    def _refresh_cards(self):
        for i, card in enumerate(self._frame_cells):
            if i in self.selected_frames:
                card.config(bg=BG_SEL)
                for w in card.winfo_children():
                    w.config(bg=BG_SEL)
            else:
                card.config(bg=BG_CARD)
                for w in card.winfo_children():
                    w.config(bg=BG_CARD)

    def _update_sel_label(self):
        n = len(self.selected_frames)
        if n == 0:
            self.lbl_sel.config(text="")
        elif n == 1:
            self.lbl_sel.config(text=f"frame {next(iter(self.selected_frames))} selected")
        else:
            s = sorted(self.selected_frames)
            self.lbl_sel.config(text=f"{n} frames selected: {s}")

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ── merge / split ─────────────────────────────────────────────────────────

    def _merge_frames(self):
        if not self.selected_anim:
            return
        if len(self.selected_frames) < 2:
            messagebox.showwarning("Merge", "Select 2 or more frames to merge.")
            return
        indices = sorted(self.selected_frames)
        try:
            from extract_sprites import cmd_stitch
            cmd_stitch(self.selected_anim, indices)
            self._set_status(f"Merged frames {indices} -> frame {indices[0]}.")
            self.selected_frames.clear()
            self._last_clicked = None
            self._load_frames(self.selected_anim)
        except Exception as exc:
            messagebox.showerror("Merge Error", str(exc))

    def _split_frame(self):
        if not self.selected_anim:
            return
        if len(self.selected_frames) != 1:
            messagebox.showwarning("Split", "Select exactly 1 frame to split.")
            return
        idx = next(iter(self.selected_frames))
        try:
            from extract_sprites import cmd_split, load_metadata
            meta = load_metadata(self.selected_anim)
            frame_meta = next(
                (f for f in meta["frames"] if f["index"] == idx), None)
            if frame_meta is None:
                messagebox.showerror("Split", f"Frame {idx} not found in frames.json.")
                return
            n_blobs = len(frame_meta["blobs"])
            if n_blobs < 2:
                messagebox.showinfo(
                    "Split",
                    f"Frame {idx} has only 1 blob — nothing to split.\n\n"
                    "Use Merge to combine frames first if they were stitched "
                    "from multiple separate blobs.")
                return
            cmd_split(self.selected_anim, idx, split_x=None)
            self._set_status(
                f"Split frame {idx} into {n_blobs} frames.")
            self.selected_frames.clear()
            self._last_clicked = None
            self._load_frames(self.selected_anim)
        except Exception as exc:
            messagebox.showerror("Split Error", str(exc))

    def _delete_selected_frames(self):
        if not self.selected_anim or not self.selected_frames:
            messagebox.showwarning("Delete Frames", "Select one or more frames first.")
            return
        indices = sorted(self.selected_frames)
        n = len(indices)
        if not messagebox.askyesno(
                "Delete Frames",
                f"Permanently delete {n} frame(s) {indices}?\n\nThis cannot be undone.",
                icon="warning"):
            return
        try:
            _cmd_delete_frames(self.selected_anim, set(indices))
            self._set_status(f"Deleted {n} frame(s).")
            self.selected_frames.clear()
            self._last_clicked = None
            self._load_frames(self.selected_anim)
        except Exception as exc:
            messagebox.showerror("Delete Frames", str(exc))

    # ── animation preview ─────────────────────────────────────────────────────

    def _pv_null_refs(self):
        """Clear widget references so stale callbacks exit cleanly."""
        self._pv_canvas      = None
        self._pv_btn_play    = None
        self._pv_lbl_counter = None
        self._pv_lbl_delay   = None

    @staticmethod
    def _safe_destroy(widget):
        try:
            if widget and widget.winfo_exists():
                widget.destroy()
        except Exception:
            pass

    def _build_preview_panel(self, parent: tk.Widget):
        """Build (or rebuild) the preview panel inside *parent*."""
        # Stop playback and null refs BEFORE touching any widgets.
        self._pv_pause()
        self._pv_null_refs()
        for w in list(parent.winfo_children()):
            self._safe_destroy(w)

        # header row
        hdr = tk.Frame(parent, bg=BG_PANEL, pady=4)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="Preview", bg=BG_PANEL, fg=ACCENT,
                 font=("", 10, "bold")).pack(side=tk.LEFT, padx=8)
        is_detached = isinstance(parent, tk.Toplevel)
        if is_detached:
            self._btn(hdr, "Reattach", self._pv_reattach,
                      YELLOW, small=True).pack(side=tk.RIGHT, padx=4)
        else:
            self._btn(hdr, "Detach [+]", self._pv_detach,
                      FG_DIM, small=True).pack(side=tk.RIGHT, padx=4)

        # canvas
        self._pv_canvas = tk.Canvas(parent, bg=BG_PANEL,
                                    highlightthickness=0)
        self._pv_canvas.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        self._pv_canvas.bind("<Configure>", lambda _: self._pv_render())

        # playback controls
        ctrl = tk.Frame(parent, bg=BG_PANEL, pady=4)
        ctrl.pack(fill=tk.X, padx=4)

        self._pv_btn_play = self._btn(ctrl, "▶", self._pv_toggle_play, GREEN)
        self._pv_btn_play.pack(side=tk.LEFT, padx=2)
        self._btn(ctrl, "■", self._pv_stop, RED).pack(side=tk.LEFT, padx=2)

        self._pv_lbl_counter = tk.Label(ctrl, text="— / —",
                                        bg=BG_PANEL, fg=FG_DIM,
                                        font=("Consolas", 9), width=8)
        self._pv_lbl_counter.pack(side=tk.LEFT, padx=6)

        tk.Checkbutton(ctrl, text="Loop", variable=self._pv_loop,
                       bg=BG_PANEL, fg=FG, selectcolor=BG_CARD,
                       activebackground=BG_PANEL, activeforeground=FG,
                       relief=tk.FLAT, borderwidth=0).pack(side=tk.LEFT, padx=4)

        # delay slider — no command= to avoid Tcl command lifecycle issues;
        # label is updated inside _pv_render instead.
        sldr = tk.Frame(parent, bg=BG_PANEL, pady=2)
        sldr.pack(fill=tk.X, padx=6, pady=(0, 6))
        tk.Label(sldr, text="Delay", bg=BG_PANEL, fg=FG_DIM,
                 font=("", 8)).pack(side=tk.LEFT)
        tk.Scale(sldr, variable=self._pv_delay,
                 from_=20, to=2000, resolution=10, orient=tk.HORIZONTAL,
                 bg=BG_PANEL, fg=FG, troughcolor=BG_CARD,
                 highlightthickness=0, showvalue=False,
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        self._pv_lbl_delay = tk.Label(sldr, text="100 ms",
                                      bg=BG_PANEL, fg=FG_DIM,
                                      font=("Consolas", 8), width=7)
        self._pv_lbl_delay.pack(side=tk.LEFT)

        # If frames are already loaded, render the current one.
        if self._pv_frames:
            self._pv_render()

    def _pv_update_delay_label(self):
        ms = self._pv_delay.get()
        if self._pv_lbl_delay and self._pv_lbl_delay.winfo_exists():
            self._pv_lbl_delay.config(text=f"{ms} ms")

    def _pv_measure_max(self):
        """Scan all frames and store the max natural width/height."""
        mw = mh = 1
        for p in self._pv_frames:
            try:
                with Image.open(p) as im:
                    mw = max(mw, im.width)
                    mh = max(mh, im.height)
            except Exception:
                pass
        self._pv_max_w = mw
        self._pv_max_h = mh

    def _pv_load(self, anim_dir: Path):
        self._pv_stop()
        self._pv_frames  = sorted(anim_dir.glob("*.png"))
        self._pv_current = 0
        self._pv_measure_max()
        self._pv_render()

    def _pv_render(self):
        """Render the current frame onto the preview canvas."""
        canvas = self._pv_canvas
        if canvas is None or not canvas.winfo_exists():
            return
        if not self._pv_frames:
            canvas.delete("all")
            canvas.create_text(
                canvas.winfo_width() // 2 or 60,
                canvas.winfo_height() // 2 or 60,
                text="No animation", fill=FG_DIM, font=("", 9))
            return

        idx = self._pv_current
        try:
            img = Image.open(self._pv_frames[idx]).convert("RGBA")
        except Exception:
            return

        cw = canvas.winfo_width()  or 240
        ch = canvas.winfo_height() or 240

        # Single scale derived from the largest frame so every frame renders
        # at the same zoom level.
        max_w = self._pv_max_w or img.width
        max_h = self._pv_max_h or img.height
        scale = min(cw / max_w, ch / max_h)
        scale = max(scale, 1.0)
        scale = min(scale, MAX_SCALE)

        w = max(1, round(img.width  * scale))
        h = max(1, round(img.height * scale))
        img = img.resize((w, h), Image.NEAREST)

        # Flatten transparency onto the canvas background colour.
        canvas_bg = canvas.cget("bg")
        panel_rgb = tuple(
            int(canvas_bg.lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
        bg_img = Image.new("RGBA", (w, h), (*panel_rgb, 255))
        composited = Image.alpha_composite(bg_img, img)
        self._pv_photo = ImageTk.PhotoImage(composited)

        canvas.delete("all")
        # Anchor: lower-left corner offset from panel centre by half the
        # maximum scaled frame dimensions, so all frames share the same
        # stable lower-left origin with the animation bounding-box centred.
        anchor_x = cw // 2 - round(max_w * scale) // 2
        anchor_y = ch // 2 + round(max_h * scale) // 2
        canvas.create_image(anchor_x, anchor_y,
                            image=self._pv_photo, anchor=tk.SW)

        n = len(self._pv_frames)
        if self._pv_lbl_counter and self._pv_lbl_counter.winfo_exists():
            self._pv_lbl_counter.config(text=f"{idx + 1} / {n}")
        self._pv_update_delay_label()

    def _pv_tick(self):
        if not self._pv_playing or not self._pv_frames:
            return
        nxt = self._pv_current + 1
        if nxt >= len(self._pv_frames):
            if self._pv_loop.get():
                nxt = 0
            else:
                self._pv_stop()
                return
        self._pv_current = nxt
        self._pv_render()
        # Schedule on root, not the canvas, so it survives widget rebuilds.
        self._pv_after_id = self.root.after(self._pv_delay.get(), self._pv_tick)

    def _pv_toggle_play(self):
        if self._pv_playing:
            self._pv_pause()
        else:
            self._pv_play()

    def _pv_play(self):
        if not self._pv_frames:
            return
        self._pv_playing = True
        if self._pv_btn_play and self._pv_btn_play.winfo_exists():
            self._pv_btn_play.config(text="⏸")
        self._pv_tick()

    def _pv_pause(self):
        self._pv_playing = False
        if self._pv_btn_play and self._pv_btn_play.winfo_exists():
            self._pv_btn_play.config(text="▶")
        if self._pv_after_id:
            try:
                self.root.after_cancel(self._pv_after_id)
            except Exception:
                pass
            self._pv_after_id = None

    def _pv_stop(self):
        self._pv_pause()
        self._pv_current = 0
        self._pv_render()

    def _pv_detach(self):
        """Open the preview in a separate resizable window."""
        if self._pv_toplevel and self._pv_toplevel.winfo_exists():
            self._pv_toplevel.lift()
            return
        was_playing = self._pv_playing

        # Stop + null refs before touching any widgets.
        self._pv_pause()
        self._pv_null_refs()

        # Show placeholder in the main pane first (no active widgets to conflict).
        for w in list(self._pv_pane.winfo_children()):
            self._safe_destroy(w)
        tk.Label(self._pv_pane, text="Preview detached",
                 bg=BG_PANEL, fg=FG_DIM, font=("", 9)).pack(pady=20)
        self._btn(self._pv_pane, "Reattach [-]", self._pv_reattach,
                  YELLOW, small=True).pack()

        # Build the live preview inside the new Toplevel.
        tl = tk.Toplevel(self.root)
        tl.title("Animation Preview")
        tl.geometry("400x500")
        tl.configure(bg=BG_PANEL)
        tl.protocol("WM_DELETE_WINDOW", self._pv_reattach)
        self._pv_toplevel = tl
        self._build_preview_panel(tl)

        if was_playing:
            self._pv_play()

    def _pv_reattach(self):
        """Close the detached window and rebuild preview in the main pane."""
        was_playing = self._pv_playing
        # Stop + null refs before any destruction.
        self._pv_pause()
        self._pv_null_refs()
        tl = self._pv_toplevel
        self._pv_toplevel = None
        self._safe_destroy(tl)
        self._build_preview_panel(self._pv_pane)
        if was_playing:
            self._pv_play()

    # ── preview reload (sync after any sequence change) ───────────────────────

    def _pv_reload(self):
        """Reload preview frames from disk, preserving position and play state."""
        if not self.selected_anim:
            self._pv_frames = []
            self._pv_render()
            return
        was_playing = self._pv_playing
        if was_playing:
            self._pv_pause()
        self._pv_frames  = sorted(self.selected_anim.glob("*.png"))
        self._pv_current = min(self._pv_current, max(0, len(self._pv_frames) - 1))
        self._pv_measure_max()
        self._pv_render()
        if was_playing and self._pv_frames:
            self._pv_play()

    # ── duplicate frame ───────────────────────────────────────────────────────

    def _duplicate_frame(self):
        if not self.selected_anim:
            return
        if len(self.selected_frames) != 1:
            messagebox.showwarning("Duplicate", "Select exactly 1 frame to duplicate.")
            return
        idx = next(iter(self.selected_frames))
        try:
            _cmd_duplicate_frame(self.selected_anim, idx)
            self._set_status(f"Duplicated frame {idx} to end.")
            self.selected_frames.clear()
            self._last_clicked = None
            self._load_frames(self.selected_anim)
        except Exception as exc:
            messagebox.showerror("Duplicate Error", str(exc))

    # ── drag-to-reorder ───────────────────────────────────────────────────────

    _DRAG_THRESHOLD = 6   # pixels of movement before drag mode activates

    def _drag_press(self, event, idx: int):
        """Button-1 press: immediately select and record drag start."""
        self._on_click(event, idx)
        self._drag_src      = idx
        self._drag_dst      = None
        self._drag_active   = False
        self._drag_start_xy = (event.x_root, event.y_root)

    def _drag_motion(self, event, __idx: int):
        """B1-Motion: activate drag once threshold is exceeded."""
        if self._drag_src is None:
            return
        dx = abs(event.x_root - self._drag_start_xy[0])
        if not self._drag_active and dx < self._DRAG_THRESHOLD:
            return
        self._drag_active = True
        # Determine drop target from mouse x relative to frame_holder
        holder_x = event.x_root - self.frame_holder.winfo_rootx()
        self._drag_dst = self._drag_find_target(holder_x)
        self._drag_show_indicator(self._drag_dst)

    def _drag_release(self, __event, __idx: int):
        """ButtonRelease-1: complete the drag if active."""
        if not self._drag_active or self._drag_src is None:
            self._drag_cancel()
            return
        src, dst = self._drag_src, self._drag_dst
        self._drag_cancel()
        if src is not None and dst is not None and dst != src and dst != src + 1:
            try:
                _cmd_reorder_frames(self.selected_anim, src, dst)
                self._set_status(f"Moved frame {src} to position {dst}.")
                self.selected_frames.clear()
                self._last_clicked = None
                self._load_frames(self.selected_anim)
            except Exception as exc:
                messagebox.showerror("Reorder Error", str(exc))

    def _drag_cancel(self):
        self._drag_active = False
        self._drag_src    = None
        self._drag_dst    = None
        if self._drag_indicator and self._drag_indicator.winfo_exists():
            self._drag_indicator.place_forget()

    def _drag_find_target(self, mouse_x: int) -> int:
        """Return the insertion index (0..n) that the mouse is currently over."""
        for i, card in enumerate(self._frame_cells):
            cx = card.winfo_x() + card.winfo_width() // 2
            if mouse_x < cx:
                return i
        return len(self._frame_cells)

    def _drag_show_indicator(self, target_idx: int):
        ind = self._drag_indicator
        if ind is None or not ind.winfo_exists():
            return
        cells = self._frame_cells
        if not cells:
            return
        h = max(card.winfo_height() for card in cells) + 12
        if target_idx < len(cells):
            x = cells[target_idx].winfo_x() - 3
        else:
            last = cells[-1]
            x = last.winfo_x() + last.winfo_width() + 1
        ind.place(x=x, y=0, width=4, height=h)
        ind.lift()

    # ── context menus ─────────────────────────────────────────────────────────

    def _context_menu(self, items: list) -> tk.Menu:
        """Build a popup menu from a list of (label, command) or None (separator)."""
        m = tk.Menu(self.root, tearoff=False,
                    bg=BG_PANEL, fg=FG,
                    activebackground=BG_SEL, activeforeground=ACCENT,
                    relief=tk.FLAT, borderwidth=1)
        for item in items:
            if item is None:
                m.add_separator()
            else:
                label, cmd, enabled = item[0], item[1], item[2] if len(item) > 2 else True
                m.add_command(label=label, command=cmd,
                              state=tk.NORMAL if enabled else tk.DISABLED)
        return m

    def _anim_right_click(self, event):
        # Select the item under the cursor first
        idx = self.anim_list.nearest(event.y)
        if idx < 0:
            return
        self.anim_list.selection_clear(0, tk.END)
        self.anim_list.selection_set(idx)
        self._on_anim_select()
        menu = self._context_menu([
            ("Rename…", self._rename_folder),
            None,
            ("Delete",  self._delete_anim),
        ])
        menu.post(event.x_root, event.y_root)

    def _frame_right_click(self, event, idx: int):
        # If clicking an unselected frame, replace the selection
        if idx not in self.selected_frames:
            self.selected_frames = {idx}
            self._last_clicked = idx
            self._refresh_cards()
            self._update_sel_label()
        n = len(self.selected_frames)
        menu = self._context_menu([
            ("Split",                self._split_frame,            n == 1),
            (f"Merge {n} frames",    self._merge_frames,           n >= 2),
            ("Duplicate",            self._duplicate_frame,        n == 1),
            None,
            (f"Delete {n} frame(s)", self._delete_selected_frames, True),
        ])
        menu.post(event.x_root, event.y_root)


# ── frame deletion helper ─────────────────────────────────────────────────────

def _cmd_delete_frames(out_dir: Path, indices: set[int]):
    """Remove frames at `indices`, renumber the rest, update frames.json."""
    import json as _json
    meta_path = out_dir / "frames.json"
    meta = _json.loads(meta_path.read_text(encoding="utf-8"))

    # Delete PNG files for removed frames
    for f in meta["frames"]:
        if f["index"] in indices:
            p = out_dir / f["file"]
            if p.exists():
                p.unlink()

    kept = [f for f in meta["frames"] if f["index"] not in indices]

    # Two-pass rename to avoid collisions: first to .tmp names, then to final names
    for new_idx, f in enumerate(kept):
        old_path = out_dir / f["file"]
        tmp_path = out_dir / f"{new_idx:03d}.__tmp__.png"
        if old_path.exists():
            old_path.rename(tmp_path)
        f["_tmp"] = tmp_path.name

    for new_idx, f in enumerate(kept):
        tmp_path = out_dir / f["_tmp"]
        final    = out_dir / f"{new_idx:03d}.png"
        if tmp_path.exists():
            tmp_path.rename(final)
        del f["_tmp"]
        f["index"] = new_idx
        f["file"]  = f"{new_idx:03d}.png"

    meta["frames"] = kept
    meta_path.write_text(_json.dumps(meta, indent=2), encoding="utf-8")


def _cmd_duplicate_frame(out_dir: Path, src_idx: int):
    """Append a copy of frame src_idx at the end of the sequence."""
    import json as _json, shutil as _shutil
    meta_path = out_dir / "frames.json"
    meta = _json.loads(meta_path.read_text(encoding="utf-8"))

    src_frame = next(f for f in meta["frames"] if f["index"] == src_idx)
    new_idx   = max(f["index"] for f in meta["frames"]) + 1

    _shutil.copy2(out_dir / src_frame["file"], out_dir / f"{new_idx:03d}.png")

    meta["frames"].append({
        "index": new_idx,
        "file":  f"{new_idx:03d}.png",
        "blobs": src_frame["blobs"],
    })
    meta_path.write_text(_json.dumps(meta, indent=2), encoding="utf-8")


def _cmd_reorder_frames(out_dir: Path, src_idx: int, dst_idx: int):
    """Move frame at src_idx so it is inserted before dst_idx, renaming files."""
    if src_idx == dst_idx or src_idx + 1 == dst_idx:
        return
    import json as _json
    meta_path = out_dir / "frames.json"
    meta   = _json.loads(meta_path.read_text(encoding="utf-8"))
    frames = sorted(meta["frames"], key=lambda f: f["index"])

    item = frames.pop(src_idx)
    # Adjust insertion point: removing src shifts later positions left by one
    insert_at = dst_idx - 1 if dst_idx > src_idx else dst_idx
    frames.insert(insert_at, item)

    # Two-pass rename to avoid collisions
    for new_idx, f in enumerate(frames):
        old_path = out_dir / f["file"]
        tmp_path = out_dir / f"{new_idx:03d}.__tmp__.png"
        if old_path.exists():
            old_path.rename(tmp_path)
        f["_tmp"] = tmp_path.name

    for new_idx, f in enumerate(frames):
        tmp_path = out_dir / f["_tmp"]
        final    = out_dir / f"{new_idx:03d}.png"
        if tmp_path.exists():
            tmp_path.rename(final)
        del f["_tmp"]
        f["index"] = new_idx
        f["file"]  = f"{new_idx:03d}.png"

    meta["frames"] = frames
    meta_path.write_text(_json.dumps(meta, indent=2), encoding="utf-8")


# ── simple input dialog ───────────────────────────────────────────────────────

class _InputDialog(tk.Toplevel):
    def __init__(self, parent, title, prompt, initial=""):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()
        self.result = None

        tk.Label(self, text=prompt, bg=BG, fg=FG).pack(padx=16, pady=(12, 4))
        self._entry = tk.Entry(self, bg=BG_CARD, fg=FG, insertbackground=FG,
                               relief=tk.FLAT, width=36, font=("Consolas", 10))
        self._entry.pack(padx=16, pady=4)
        self._entry.insert(0, initial)
        self._entry.select_range(0, tk.END)
        self._entry.focus()

        btns = tk.Frame(self, bg=BG)
        btns.pack(pady=8)
        tk.Button(btns, text="OK",     command=self._ok,
                  bg=BG_CARD, fg=GREEN, relief=tk.FLAT,
                  padx=10).pack(side=tk.LEFT, padx=4)
        tk.Button(btns, text="Cancel", command=self.destroy,
                  bg=BG_CARD, fg=FG_DIM, relief=tk.FLAT,
                  padx=10).pack(side=tk.LEFT, padx=4)
        self._entry.bind("<Return>", lambda _: self._ok())
        self._entry.bind("<Escape>", lambda _: self.destroy())

        # centre over parent
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width()  - self.winfo_width())  // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")
        self.wait_window()

    def _ok(self):
        self.result = self._entry.get().strip()
        self.destroy()


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Sprite Sheet Extractor GUI")
    ap.add_argument("file",   nargs="?", default="",
                    help=f"Sprite-sheet GIF or project file ({PROJECT_EXT}).")
    ap.add_argument("--output", "-o", default="",
                    help="Output folder (ignored when loading a project file).")
    args = ap.parse_args()

    root = tk.Tk()
    root.geometry("1200x720")
    app = SpriteGUI(root)

    if args.file:
        p = Path(args.file)
        if p.suffix.lower() == PROJECT_EXT:
            root.after(100, lambda: app._load_project_file(p))
        else:
            app.v_gif.set(str(p))
            if args.output:
                app.v_out.set(args.output)
            root.after(100, app._maybe_autoload)

    root.mainloop()


if __name__ == "__main__":
    main()
