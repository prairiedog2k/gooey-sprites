"""SpriteGUI — main application window."""

import json
import shutil
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path

from PIL import Image, ImageTk

from constants import (
    BG, BG_PANEL, BG_CARD, BG_SEL,
    FG, FG_DIM, ACCENT, RED, GREEN, YELLOW,
    THUMB_H, MIN_SCALE, MAX_SCALE,
    PROJECT_EXT,
)
from project import _read_project, _write_project, _resolve_project_paths
from image_helpers import _make_thumb
from frame_ops import (
    _cmd_delete_frames, _cmd_duplicate_frame, _cmd_reorder_frames,
)
from dialogs import _InputDialog
from compose_window import ComposeWindow


# ── undo helpers ──────────────────────────────────────────────────────────────

def _snapshot_anim_dir(anim_dir: Path) -> dict[str, bytes]:
    """Return a {filename: bytes} snapshot of every file in anim_dir."""
    return {f.name: f.read_bytes() for f in anim_dir.iterdir() if f.is_file()}


def _restore_anim_dir(anim_dir: Path, snapshot: dict[str, bytes]) -> None:
    """Recreate anim_dir from a snapshot, replacing whatever is there."""
    if anim_dir.exists():
        shutil.rmtree(anim_dir)
    anim_dir.mkdir(parents=True)
    for name, data in snapshot.items():
        (anim_dir / name).write_bytes(data)


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
        self.v_gap          = tk.IntVar(value=4)
        self.v_tol          = tk.IntVar(value=20)
        self.v_minpx        = tk.IntVar(value=100)
        self.v_filter_false = tk.BooleanVar(value=False)
        self.v_auto_split   = tk.BooleanVar(value=True)

        self._anim_dirs: list[Path] = []
        self._flagged_anims: set[str] = set()
        self.selected_anim: Path | None = None
        self.selected_frames: set[int] = set()
        self._frame_images: list[ImageTk.PhotoImage] = []
        self._frame_cells:  list[tk.Frame] = []
        self._last_clicked: int | None = None
        self._frame_zoom: float = 1.0
        self._frame_fit_scale: float = 1.0
        self._frame_zoom_lbl: tk.Label | None = None
        self._project_path: Path | None = None
        self._sheet_copy_path: Path | None = None
        self._managed_anims: list[str] = []
        self._dirty: bool = False
        self._undo_stack: list[tuple[str, object]] = []
        self._MAX_UNDO = 50

        # preview state
        self._pv_frames:   list[Path] = []
        self._pv_current:  int        = 0
        self._pv_playing:  bool       = False
        self._pv_after_id             = None
        self._pv_delay    = tk.IntVar(value=100)
        self._pv_loop     = tk.BooleanVar(value=True)
        self._pv_photo                = None
        self._pv_toplevel: tk.Toplevel | None = None
        self._pv_canvas:   tk.Canvas  | None = None
        self._pv_lbl_counter: tk.Label | None = None
        self._pv_btn_play:    tk.Button | None = None
        self._pv_lbl_delay:   tk.Label | None = None
        self._pv_max_w:   int = 1
        self._pv_max_h:   int = 1

        # drag-to-reorder state
        self._drag_src:      int | None = None
        self._drag_dst:      int | None = None
        self._drag_active:   bool       = False
        self._drag_start_xy: tuple      = (0, 0)
        self._drag_indicator: tk.Frame | None = None

        # palette panel state
        self._pal_colors: list = []
        self._pal_n_colors      = tk.IntVar(value=16)
        self._pal_selected_idx: int | None = None
        self._pal_canvas:       tk.Canvas | None = None
        self._pal_count_lbl:    tk.Label  | None = None
        self._pal_n_lbl:        tk.Label  | None = None
        self._pal_hover_label:  tk.Label  | None = None
        self._pal_col_canvas:   tk.Canvas | None = None
        self._pal_col_hdr_lbl:  tk.Label  | None = None
        self._pal_col_img_refs: list = []

        # pane focus state
        self._focused_pane: str = "animations"  # animations|frames|preview|palette
        self._anim_hdr_lbl:   tk.Label | None = None
        self._frames_hdr_lbl: tk.Label | None = None
        self._pv_hdr_lbl:     tk.Label | None = None
        self._pal_hdr_lbl:    tk.Label | None = None
        self._anim_menu:      tk.Menu  | None = None
        self._frames_menu:    tk.Menu  | None = None
        self._preview_menu:   tk.Menu  | None = None

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── dirty tracking ────────────────────────────────────────────────────────

    def _mark_dirty(self):
        self._dirty = True
        self._update_title()

    # ── undo ──────────────────────────────────────────────────────────────────

    def _push_undo(self, description: str, undo_fn) -> None:
        self._undo_stack.append((description, undo_fn))
        if len(self._undo_stack) > self._MAX_UNDO:
            self._undo_stack.pop(0)
        self._update_undo_menu()

    def _clear_undo(self) -> None:
        self._undo_stack.clear()
        self._update_undo_menu()

    def _clear_ui(self) -> None:
        """Clear all views — animation list, frame viewer, and preview."""
        # Stop preview playback and blank the canvas
        self._pv_pause()
        self._pv_frames = []
        if self._pv_canvas and self._pv_canvas.winfo_exists():
            self._pv_canvas.delete("all")

        # Clear animation list, selection, and frame viewer
        self.anim_list.delete(0, tk.END)
        self._anim_dirs.clear()
        self.selected_anim = None
        self.selected_frames.clear()
        self._last_clicked = None
        self.lbl_anim.config(text="")
        self.lbl_sel.config(text="")
        for w in self.frame_holder.winfo_children():
            w.destroy()
        self._frame_images.clear()
        self._frame_cells.clear()

        # Clear palette
        self._pal_colors = []
        self._pal_redraw()

    # ── pane focus ────────────────────────────────────────────────────────────

    _PANE_ORDER = ["animations", "frames", "preview", "palette"]

    def _set_pane_focus(self, pane: str) -> None:
        self._focused_pane = pane
        lbl_map = {
            "animations": self._anim_hdr_lbl,
            "frames":     self._frames_hdr_lbl,
            "preview":    self._pv_hdr_lbl,
            "palette":    self._pal_hdr_lbl,
        }
        for name, lbl in lbl_map.items():
            if lbl and lbl.winfo_exists():
                lbl.config(fg=GREEN if name == pane else ACCENT)
        self._update_pane_menus()

    def _update_pane_menus(self) -> None:
        def _set_menu(menu: tk.Menu | None, state: str) -> None:
            if not menu:
                return
            last = menu.index("end")
            if last is None:
                return
            for i in range(last + 1):
                try:
                    menu.entryconfig(i, state=state)
                except tk.TclError:
                    pass  # separators

        _set_menu(self._anim_menu,    tk.NORMAL if self._focused_pane == "animations" else tk.DISABLED)
        _set_menu(self._frames_menu,  tk.NORMAL if self._focused_pane == "frames"     else tk.DISABLED)
        _set_menu(self._preview_menu, tk.NORMAL if self._focused_pane == "preview"    else tk.DISABLED)

    def _focus_next_pane(self) -> None:
        order = self._PANE_ORDER
        idx = order.index(self._focused_pane) if self._focused_pane in order else 0
        self._set_pane_focus(order[(idx + 1) % len(order)])

    def _focus_prev_pane(self) -> None:
        order = self._PANE_ORDER
        idx = order.index(self._focused_pane) if self._focused_pane in order else 0
        self._set_pane_focus(order[(idx - 1) % len(order)])

    def _open_frame_edit_from_menu(self) -> None:
        if not self.selected_anim or not self.selected_frames:
            return
        self._open_frame_edit(min(self.selected_frames))

    def _pv_faster(self) -> None:
        self._pv_delay.set(max(20, self._pv_delay.get() - 20))

    def _pv_slower(self) -> None:
        self._pv_delay.set(min(2000, self._pv_delay.get() + 20))

    def _do_undo(self) -> None:
        if not self._undo_stack:
            self._set_status("Nothing to undo.")
            return
        description, undo_fn = self._undo_stack.pop()
        try:
            undo_fn()
        except Exception as exc:
            messagebox.showerror("Undo Failed", str(exc))
        self._mark_dirty()
        self._update_undo_menu()
        self._set_status(f"Undo: {description}")

    def _update_undo_menu(self) -> None:
        if not hasattr(self, "_edit_menu"):
            return
        if self._undo_stack:
            desc = self._undo_stack[-1][0]
            self._edit_menu.entryconfig(0, label=f"Undo {desc}",
                                        state=tk.NORMAL)
        else:
            self._edit_menu.entryconfig(0, label="Undo", state=tk.DISABLED)

    def _select_list_item(self, idx: int) -> None:
        """Select the animation at *idx* in the listbox and load its content."""
        if not self._anim_dirs:
            return
        idx = max(0, min(idx, len(self._anim_dirs) - 1))
        self.anim_list.selection_clear(0, tk.END)
        self.anim_list.selection_set(idx)
        self.anim_list.see(idx)
        d = self._anim_dirs[idx]
        self.selected_anim = d
        self.selected_frames.clear()
        self._last_clicked = None
        self.lbl_anim.config(text=d.name)
        self._load_frames(d)
        self._pv_load(d)

    def _select_anim_by_path(self, path: Path) -> None:
        """Select and display the animation at *path* if it still exists."""
        self._load_output()
        for i, d in enumerate(self._anim_dirs):
            if d == path:
                self._select_list_item(i)
                return

    # ── close ─────────────────────────────────────────────────────────────────

    def _on_close(self):
        """Exit cleanly: cancel callbacks, stop the event loop, destroy widgets."""
        if self._dirty:
            answer = messagebox.askyesnocancel(
                "Unsaved Changes",
                "You have unsaved changes. Save the project before closing?")
            if answer is None:   # Cancel
                return
            if answer:           # Yes — save first
                self._save_project()
                if self._dirty:  # save was cancelled (no path chosen)
                    return
        self._pv_playing = False
        if self._pv_after_id:
            try:
                self.root.after_cancel(self._pv_after_id)
            except Exception:
                pass
            self._pv_after_id = None
        self._pv_null_refs()
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self._build_menu()
        self._build_toolbar()
        self._build_body()
        self._build_statusbar()

    def _build_menu(self):
        def _menu(**kw):
            return tk.Menu(None, tearoff=False,
                           bg=BG_PANEL, fg=FG,
                           activebackground=BG_SEL, activeforeground=ACCENT,
                           relief=tk.FLAT, **kw)

        menubar = tk.Menu(self.root, bg=BG_PANEL, fg=FG,
                          activebackground=BG_SEL, activeforeground=ACCENT,
                          relief=tk.FLAT, borderwidth=0)
        self.root.config(menu=menubar)

        # ── File ──────────────────────────────────────────────────────────────
        file_menu = _menu()
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="New Project",       command=self._new_project,
                              accelerator="Ctrl+N")
        file_menu.add_command(label="Open Project…",     command=self._open_project,
                              accelerator="Ctrl+O")
        self._recent_menu = _menu()
        file_menu.add_cascade(label="Open Recent", menu=self._recent_menu)
        self._rebuild_recent_menu()
        file_menu.add_separator()
        file_menu.add_command(label="Save Project",      command=self._save_project,
                              accelerator="Ctrl+S")
        file_menu.add_command(label="Save Project As…",  command=self._save_project_as,
                              accelerator="Ctrl+Shift+S")
        file_menu.add_separator()
        file_menu.add_command(label="View Sprite Sheet…", command=self._open_sheet_viewer)
        file_menu.add_separator()
        file_menu.add_command(label="Exit",              command=self._on_close)

        # ── Edit ──────────────────────────────────────────────────────────────
        edit_menu = _menu()
        menubar.add_cascade(label="Edit", menu=edit_menu)
        edit_menu.add_command(label="Undo", command=self._do_undo,
                              accelerator="Ctrl+Z", state=tk.DISABLED)
        self._edit_menu = edit_menu

        # ── Animations ────────────────────────────────────────────────────────
        anim_menu = _menu()
        menubar.add_cascade(label="Animations", menu=anim_menu)
        anim_menu.add_command(label="Rename…",      command=self._rename_folder)
        anim_menu.add_command(label="Duplicate",    command=self._duplicate_anim)
        anim_menu.add_command(label="Remove",       command=self._delete_anim)
        anim_menu.add_separator()
        anim_menu.add_command(label="Compose From", command=lambda: self._open_compose(
                                                        initial_anim=self.selected_anim))
        anim_menu.add_command(label="Compose New",  command=self._open_compose)
        self._anim_menu = anim_menu

        # ── Frames ────────────────────────────────────────────────────────────
        frames_menu = _menu()
        menubar.add_cascade(label="Frames", menu=frames_menu)
        frames_menu.add_command(label="Edit",      command=self._open_frame_edit_from_menu)
        frames_menu.add_command(label="Split",     command=self._split_frame)
        frames_menu.add_command(label="Merge",     command=self._merge_frames)
        frames_menu.add_command(label="Duplicate", command=self._duplicate_frame)
        frames_menu.add_command(label="Delete",    command=self._delete_selected_frames)
        self._frames_menu = frames_menu

        # ── Preview ───────────────────────────────────────────────────────────
        preview_menu = _menu()
        menubar.add_cascade(label="Preview", menu=preview_menu)
        preview_menu.add_command(label="Start",  command=self._pv_play)
        preview_menu.add_command(label="Stop",   command=self._pv_pause)
        preview_menu.add_checkbutton(label="Loop", variable=self._pv_loop)
        preview_menu.add_separator()
        preview_menu.add_command(label="Faster", command=self._pv_faster)
        preview_menu.add_command(label="Slower", command=self._pv_slower)
        self._preview_menu = preview_menu

        # Initial menu state — Animations pane starts focused
        self._update_pane_menus()

        # ── Global key bindings ───────────────────────────────────────────────
        self.root.bind_all("<Control-n>", lambda _: self._new_project())
        self.root.bind_all("<Control-o>", lambda _: self._open_project())
        self.root.bind_all("<Control-s>", lambda _: self._save_project())
        self.root.bind_all("<Control-S>", lambda _: self._save_project_as())
        self.root.bind_all("<Control-z>", lambda _: self._do_undo())
        self.root.bind_all("<F2>",        lambda _: self._rename_folder())
        self.root.bind_all("<Control-Key-1>", lambda _: self._set_pane_focus("animations"))
        self.root.bind_all("<Control-Key-2>", lambda _: self._set_pane_focus("frames"))
        self.root.bind_all("<Control-Key-3>", lambda _: self._set_pane_focus("preview"))
        self.root.bind_all("<Control-Key-4>", lambda _: self._set_pane_focus("palette"))
        self.root.bind_all("<Left>",         lambda _: self._frame_arrow_key(-1, shift=False))
        self.root.bind_all("<Right>",        lambda _: self._frame_arrow_key(+1, shift=False))
        self.root.bind_all("<Shift-Left>",   lambda _: self._frame_arrow_key(-1, shift=True))
        self.root.bind_all("<Shift-Right>",  lambda _: self._frame_arrow_key(+1, shift=True))

    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg=BG_PANEL, pady=6, padx=8)
        bar.pack(fill=tk.X)

        # row 1 – file paths
        r1 = tk.Frame(bar, bg=BG_PANEL)
        r1.pack(fill=tk.X, pady=2)
        self._label(r1, "Sprite Sheet:", width=16).pack(side=tk.LEFT)
        tk.Entry(r1, textvariable=self.v_gif, bg=BG_CARD, fg=FG,
                 insertbackground=FG, relief=tk.FLAT, width=55,
                 font=("Consolas", 9)).pack(side=tk.LEFT, padx=4)
        self._btn(r1, "Browse…",    self._browse_gif).pack(side=tk.LEFT)
        self._btn(r1, "View Sheet", self._open_sheet_viewer).pack(side=tk.LEFT, padx=(4, 0))

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
        self._btn(r3, "Load Output Folder",   self._user_load_output, ACCENT).pack(side=tk.LEFT, padx=4)
        self._label(r3, "  Gap:").pack(side=tk.LEFT)
        tk.Spinbox(r3, from_=0, to=20, textvariable=self.v_gap, width=4,
                   bg=BG_CARD, fg=FG, buttonbackground=BG_CARD,
                   relief=tk.FLAT).pack(side=tk.LEFT)
        self._label(r3, "  Tol:").pack(side=tk.LEFT)
        tk.Spinbox(r3, from_=0, to=100, textvariable=self.v_tol, width=4,
                   bg=BG_CARD, fg=FG, buttonbackground=BG_CARD,
                   relief=tk.FLAT).pack(side=tk.LEFT)
        self._label(r3, "  Min px:").pack(side=tk.LEFT)
        tk.Spinbox(r3, from_=0, to=5000, textvariable=self.v_minpx, width=5,
                   bg=BG_CARD, fg=FG, buttonbackground=BG_CARD,
                   relief=tk.FLAT).pack(side=tk.LEFT)
        tk.Checkbutton(r3, text="  Filter false positives",
                       variable=self.v_filter_false,
                       bg=BG_PANEL, fg=FG, selectcolor=BG_CARD,
                       activebackground=BG_PANEL, activeforeground=FG,
                       relief=tk.FLAT, borderwidth=0,
                       font=("", 9)).pack(side=tk.LEFT, padx=(8, 0))
        tk.Checkbutton(r3, text="  Auto Split",
                       variable=self.v_auto_split,
                       bg=BG_PANEL, fg=FG, selectcolor=BG_CARD,
                       activebackground=BG_PANEL, activeforeground=FG,
                       relief=tk.FLAT, borderwidth=0,
                       font=("", 9)).pack(side=tk.LEFT, padx=(8, 0))

    _PAL_PANEL_W     = 160   # fixed pixel width of the palette strip
    _PAL_SWATCH_COLS = 4     # swatches per row
    _PAL_SWATCH_SIZE = 36    # target swatch size (px); actual = canvas_w / cols

    def _build_body(self):
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill=tk.BOTH, expand=True)

        # Palette panel – fixed width, anchored to the right
        pal_frame = tk.Frame(body, bg=BG_PANEL, width=self._PAL_PANEL_W)
        pal_frame.pack(side=tk.RIGHT, fill=tk.Y)
        pal_frame.pack_propagate(False)
        self._build_palette_panel(pal_frame)

        pane = tk.PanedWindow(body, orient=tk.HORIZONTAL,
                              bg=BG, sashwidth=5, sashrelief=tk.FLAT,
                              sashpad=2)
        pane.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)

        # ── left: animation list ──────────────────────────────────────────────
        left = tk.Frame(pane, bg=BG_PANEL, width=220)
        pane.add(left, minsize=160)

        self._anim_hdr_lbl = tk.Label(left, text="Animations", bg=BG_PANEL, fg=GREEN,
                                      font=("", 10, "bold"), pady=6)
        self._anim_hdr_lbl.pack(fill=tk.X, padx=8)

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
        self.anim_list.bind("<Button-1>",        lambda _: self._set_pane_focus("animations"), add="+")

        # buttons under the list
        btn_row = tk.Frame(left, bg=BG_PANEL)
        btn_row.pack(pady=4)
        self._btn(btn_row, "Rename…",   self._rename_folder,  YELLOW, small=True).pack(side=tk.LEFT, padx=2)
        self._btn(btn_row, "Duplicate", self._duplicate_anim, ACCENT, small=True).pack(side=tk.LEFT, padx=2)
        self._btn(btn_row, "Delete",    self._delete_anim,    RED,    small=True).pack(side=tk.LEFT, padx=2)

        compose_row = tk.Frame(left, bg=BG_PANEL)
        compose_row.pack(pady=(0, 6))
        self._btn(compose_row, "Compose…", self._open_compose,
                  GREEN, small=True).pack()

        # ── centre: frame viewer ─────────────────────────────────────────────
        right = tk.Frame(pane, bg=BG)
        pane.add(right)

        # ── right: animation preview ──────────────────────────────────────────
        self._pv_pane = tk.Frame(pane, bg=BG_PANEL, width=260)
        pane.add(self._pv_pane, minsize=180)

        hdr = tk.Frame(right, bg=BG_PANEL, pady=4)
        hdr.pack(fill=tk.X)
        self._frames_hdr_lbl = tk.Label(hdr, text="Frames", bg=BG_PANEL, fg=ACCENT,
                                        font=("", 10, "bold"))
        self._frames_hdr_lbl.pack(side=tk.LEFT, padx=8)
        self.lbl_anim = tk.Label(hdr, text="", bg=BG_PANEL,
                                 fg=FG_DIM, font=("Consolas", 9))
        self.lbl_anim.pack(side=tk.LEFT)
        self.lbl_sel = tk.Label(hdr, text="", bg=BG_PANEL,
                                fg=FG_DIM, font=("", 9))
        self.lbl_sel.pack(side=tk.RIGHT, padx=8)

        cf = tk.Frame(right, bg=BG)
        cf.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(cf, bg=BG_PANEL, highlightthickness=0)
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
        self.canvas.bind("<Button-1>", lambda _: self._set_pane_focus("frames"), add="+")

        self.frame_holder = tk.Frame(self.canvas, bg=BG_PANEL)
        self._canvas_win = self.canvas.create_window(
            (0, 0), window=self.frame_holder, anchor=tk.NW)
        self.frame_holder.bind(
            "<Configure>",
            lambda e: self.canvas.configure(
                scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_frame_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        # ── bottom frame toolbar ──────────────────────────────────────────────
        bot = tk.Frame(right, bg=BG_PANEL, pady=6, padx=8)
        bot.pack(fill=tk.X)
        self._btn(bot, "Merge Selected",    self._merge_frames,           ACCENT ).pack(side=tk.LEFT, padx=4)
        self._btn(bot, "Split Selected",    self._split_frame,            RED   ).pack(side=tk.LEFT, padx=4)
        self._btn(bot, "Duplicate",         self._duplicate_frame,        YELLOW).pack(side=tk.LEFT, padx=4)
        self._btn(bot, "Delete Frame(s)",   self._delete_selected_frames, RED   ).pack(side=tk.LEFT, padx=4)
        tk.Label(bot,
                 text="Click to select  |  Ctrl+click to add/remove  |  Shift+click to range-select",
                 bg=BG_PANEL, fg=FG_DIM, font=("", 8)).pack(side=tk.LEFT, padx=12)

        # Zoom controls (right-aligned)
        self._frame_zoom_lbl = tk.Label(bot, text="100%", width=5,
                                        bg=BG_PANEL, fg=FG, font=("Consolas", 8))
        self._frame_zoom_lbl.pack(side=tk.RIGHT, padx=(0, 4))
        self._btn(bot, "+",   self._frame_zoom_in,  small=True).pack(side=tk.RIGHT, padx=1)
        self._btn(bot, "Fit", self._frame_zoom_fit, small=True).pack(side=tk.RIGHT, padx=1)
        self._btn(bot, "−",   self._frame_zoom_out, small=True).pack(side=tk.RIGHT, padx=1)
        tk.Label(bot, text="Zoom:", bg=BG_PANEL, fg=FG_DIM,
                 font=("", 8)).pack(side=tk.RIGHT, padx=(8, 2))

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
            kw["width"]  = width
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

    # ── project file commands ─────────────────────────────────────────────────

    def _update_title(self):
        marker = " *" if self._dirty else ""
        if self._project_path:
            self.root.title(
                f"Sprite Sheet Extractor — {self._project_path.name}{marker}")
        else:
            self.root.title(f"Sprite Sheet Extractor{marker}")

    def _new_project(self):
        if self._dirty:
            answer = messagebox.askyesnocancel(
                "Unsaved Changes",
                "You have unsaved changes. Save the project before creating a new one?")
            if answer is None:
                return
            if answer:
                self._save_project()
                if self._dirty:
                    return
        path = filedialog.asksaveasfilename(
            title="New Project — Choose Location",
            defaultextension=PROJECT_EXT,
            filetypes=[("Sprite Sheet Project", f"*{PROJECT_EXT}"),
                       ("All files", "*.*")])
        if not path:
            return
        p = Path(path)

        # Create a project folder named after the project stem,
        # then place the .ssproj file and output dir inside it.
        proj_folder = p.parent / p.stem
        proj_folder.mkdir(parents=True, exist_ok=True)
        proj_file = proj_folder / p.name

        self._project_path = proj_file
        self._sheet_copy_path = None
        self.v_gif.set("")
        self.v_out.set(str(proj_folder))
        self.v_gap.set(4)
        self.v_tol.set(20)
        self.v_minpx.set(100)
        self._managed_anims.clear()
        self._flagged_anims.clear()
        self._clear_ui()
        self._dirty = False
        self._clear_undo()
        self._write_current_project(proj_file)
        self._update_title()
        self._set_status(f"New project '{p.stem}'.")

    # ── recent projects ───────────────────────────────────────────────────────

    _RECENT_FILE = Path.home() / ".sprite_sheet_recent.json"
    _RECENT_MAX  = 4

    def _load_recent_projects(self) -> list[str]:
        try:
            import json as _json
            return _json.loads(self._RECENT_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _push_recent_project(self, path: Path) -> None:
        import json as _json
        p = str(path.resolve())
        recents = [r for r in self._load_recent_projects() if r != p]
        recents.insert(0, p)
        recents = recents[:self._RECENT_MAX]
        try:
            self._RECENT_FILE.write_text(_json.dumps(recents, indent=2), encoding="utf-8")
        except Exception:
            pass
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self) -> None:
        if not hasattr(self, "_recent_menu"):
            return
        self._recent_menu.delete(0, tk.END)
        recents = self._load_recent_projects()
        if not recents:
            self._recent_menu.add_command(label="(none)", state=tk.DISABLED)
        else:
            for p in recents:
                self._recent_menu.add_command(
                    label=p,
                    command=lambda p_=p: self._load_project_file(Path(p_)))

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
        gif_abs, out_abs, sheet_abs = _resolve_project_paths(data, path.parent)
        self._clear_ui()
        self._project_path = path
        self._sheet_copy_path = Path(sheet_abs) if sheet_abs else None
        self.v_gif.set(gif_abs)
        self.v_out.set(out_abs or "./sprites")
        self.v_gap.set(int(data.get("gap", 4)))
        self.v_tol.set(int(data.get("tol", 20)))
        self.v_minpx.set(int(data.get("min_pixels", 100)))
        self._managed_anims = list(data.get("animations", []))
        self._flagged_anims = set(data.get("flagged_animations", []))
        self._dirty = False
        self._clear_undo()
        self._update_title()
        self._set_status(f"Opened '{path.name}'.")
        self._push_recent_project(path)
        self._load_output()
        if self._anim_dirs:
            self._select_list_item(0)

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

    def _ensure_sheet_copy(self, src: Path) -> str:
        """Copy *src* into the project directory; return relative path of the copy.

        If *src* is already inside the project directory no copy is made.
        Returns "" when no project is open or the source file doesn't exist.
        """
        if not self._project_path or not src.exists():
            return ""
        proj_dir = self._project_path.parent
        try:
            rel = src.relative_to(proj_dir)
            self._sheet_copy_path = src
            return str(rel)
        except ValueError:
            pass
        dest = proj_dir / f"sheet{src.suffix}"
        # Only re-copy when the destination is missing or the source has changed
        if not dest.exists() or dest.stat().st_size != src.stat().st_size:
            shutil.copy2(str(src), str(dest))
        self._sheet_copy_path = dest
        return f"sheet{src.suffix}"

    def _write_current_project(self, path: Path):
        try:
            gif_path  = self.v_gif.get().strip()
            sheet_rel = ""
            if gif_path:
                try:
                    sheet_rel = self._ensure_sheet_copy(Path(gif_path))
                except Exception:
                    pass
            _write_project(path,
                           gif=gif_path,
                           sheet=sheet_rel,
                           output=self.v_out.get().strip(),
                           gap=self.v_gap.get(),
                           tol=self.v_tol.get(),
                           min_pixels=self.v_minpx.get(),
                           animations=self._managed_anims,
                           flagged_animations=list(self._flagged_anims))
            self._dirty = False
            self._update_title()
            self._set_status(f"Project saved to '{path}'.")
        except Exception as exc:
            messagebox.showerror("Save Project", f"Could not save project:\n{exc}")

    # ── file dialogs ──────────────────────────────────────────────────────────

    def _browse_gif(self):
        p = filedialog.askopenfilename(
            filetypes=[
                ("Sprite sheets", "*.gif *.png *.jpg *.jpeg"),
                ("GIF files",  "*.gif"),
                ("PNG files",  "*.png"),
                ("JPEG files", "*.jpg *.jpeg"),
                ("All files",  "*.*"),
            ])
        if p:
            self.v_gif.set(p)

    def _browse_out(self):
        p = filedialog.askdirectory()
        if p:
            self.v_out.set(p)

    def _open_sheet_viewer(self):
        # Prefer the project-local copy; fall back to the raw v_gif path
        path: Path | None = None
        if self._sheet_copy_path and self._sheet_copy_path.exists():
            path = self._sheet_copy_path
        else:
            raw = self.v_gif.get().strip()
            if raw:
                path = Path(raw)
        if not path or not path.exists():
            messagebox.showwarning("Sprite Sheet",
                                   "No sprite sheet is loaded.\n"
                                   "Browse to a sprite sheet file first.")
            return
        from sheet_viewer import SheetViewerWindow
        SheetViewerWindow(self.root, path)

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
            messagebox.showwarning("No Sprite Sheet", "Please select a sprite sheet image first.")
            return
        out_root = Path(self.v_out.get().strip() or "./sprites")

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
                results = sheet.extract_all(max_intra_gap=gap,
                                            min_pixels=self.v_minpx.get())
                folders: list[str] = []
                scores:  list[int] = []
                auto_split = self.v_auto_split.get()
                for n, (_, sprites, frames, score) in enumerate(results, 1):
                    folder  = f"unknown-{n:03d}"
                    out_dir = out_root / folder
                    if auto_split:
                        from extract_sprites import apply_auto_split
                        sprites, frames = apply_auto_split(
                            sprites, frames, sheet.arr, sheet.bg, sheet.tol)
                    save_animation(out_dir, sprites, frames, gif, sheet.bg, sheet.tol)
                    folders.append(folder)
                    scores.append(score)
                    self.root.after(0, lambda c=len(folders), f=folder:
                                    self._set_status(f"Saved {c}: {f}"))
                self.root.after(0, lambda fl=folders, sc=scores:
                                self._finish_extract(str(out_root), fl, sc))
            except Exception as exc:
                self.root.after(0, lambda e=exc: (
                    self._set_status(f"Error: {e}"),
                    messagebox.showerror("Extraction Error", str(e))
                ))

        threading.Thread(target=run, daemon=True).start()

    def _finish_extract(self, out: str, folders: list[str],
                        scores: list[int] | None = None):
        from extract_sprites import flag_false_positives
        flags = flag_false_positives(scores) if scores else [False] * len(folders)

        out_root = Path(out)
        if self.v_filter_false.get():
            # Remove flagged animations from disk and from the list
            kept_folders: list[str] = []
            for folder, is_fp in zip(folders, flags):
                if is_fp:
                    import shutil as _shutil
                    fp_dir = out_root / folder
                    if fp_dir.is_dir():
                        _shutil.rmtree(fp_dir)
                else:
                    kept_folders.append(folder)
            self._managed_anims = kept_folders
            self._flagged_anims = set()
            n_removed = len(folders) - len(kept_folders)
            status_extra = f" ({n_removed} false positives removed)" if n_removed else ""
        else:
            self._managed_anims = folders
            self._flagged_anims = {
                folder for folder, is_fp in zip(folders, flags) if is_fp
            }
            status_extra = (
                f" ({len(self._flagged_anims)} flagged as possible false positives)"
                if self._flagged_anims else ""
            )

        self._clear_undo()
        if self._project_path:
            self._write_current_project(self._project_path)
        else:
            self._mark_dirty()
        self._set_status(f"Extracted {len(folders)} animations to '{out}'{status_extra}.")
        self._load_output()

    # ── animation list ────────────────────────────────────────────────────────

    def _load_output(self):
        """Refresh the animation list from the output folder.

        This is called from many internal places (rename, merge, split, etc.).
        Auto-split is NOT applied here; use ``_user_load_output`` for that.
        """
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
            if a.name in self._flagged_anims:
                i = self.anim_list.size() - 1
                self.anim_list.itemconfig(i, fg=YELLOW)
        self._set_status(f"Loaded {len(anims)} animation(s) from '{out}'.")
        self._refresh_palette()

    def _user_load_output(self):
        """Called by the 'Load Output Folder' button.  Applies auto-split if enabled."""
        if not self.v_auto_split.get():
            self._load_output()
            return
        out = Path(self.v_out.get().strip())
        if not out.exists():
            self._set_status(f"Output folder not found: {out}")
            return
        anims = sorted(
            d for d in out.iterdir()
            if d.is_dir() and (d / "frames.json").exists()
        )
        self._set_status(f"Loaded {len(anims)} animation(s) — auto-splitting…")
        self._anim_dirs = anims
        self.anim_list.delete(0, tk.END)
        for a in anims:
            self.anim_list.insert(tk.END, a.name)
            if a.name in self._flagged_anims:
                i = self.anim_list.size() - 1
                self.anim_list.itemconfig(i, fg=YELLOW)
        self._refresh_palette()

        def _run_splits():
            changed = sum(self._auto_split_dir(d) for d in anims)
            self.root.after(0, lambda n=changed: (
                self._load_output(),
                self._mark_dirty() if n else None,
                self._set_status(
                    f"Loaded {len(anims)} animation(s)"
                    + (f"  ({n} frame(s) split)" if n else "") + ".")
            ))
        threading.Thread(target=_run_splits, daemon=True).start()

    def _auto_split_dir(self, anim_dir: Path) -> int:
        """Split every multi-blob frame in *anim_dir* into single-blob frames.

        Returns the number of frames that were split.
        """
        try:
            import json as _json
            from extract_sprites import cmd_split
            meta_path = anim_dir / "frames.json"
            if not meta_path.exists():
                return 0
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
            # Work highest-index first so earlier indices stay valid after each split
            multi = [
                i for i, f in enumerate(meta["frames"])
                if len(f.get("blobs", [])) > 1
                and not f["blobs"][0].get("png_local")  # skip already-png-local blobs
            ]
            for idx in reversed(multi):
                cmd_split(anim_dir, idx, split_x=None)
            return len(multi)
        except Exception:
            return 0

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
        self._frame_zoom = 1.0   # reset to fit whenever the animation changes
        self.lbl_anim.config(text=anim_dir.name)
        self._load_frames(anim_dir)
        self._pv_load(anim_dir)
        self._pv_play()

    def _open_compose(self, initial_anim=None):
        out = self.v_out.get().strip()
        if not out or not Path(out).is_dir():
            messagebox.showwarning(
                "No Output Folder",
                "Load an output folder with extracted animations first.")
            return

        def _after_compose_save(anim_name: str):
            if anim_name not in self._managed_anims:
                self._managed_anims.append(anim_name)
            self._load_output()
            if self._project_path:
                self._write_current_project(self._project_path)
            else:
                self._mark_dirty()

        ComposeWindow(
            parent       = self.root,
            output_dir   = Path(out),
            on_save      = _after_compose_save,
            initial_anim = initial_anim,
            on_close     = self._load_output)

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
        old_path = self.selected_anim

        # Capture current frame file names for undo before any changes
        old_meta_path = old_path / "frames.json"
        old_frame_files = []
        if old_meta_path.exists():
            meta = json.loads(old_meta_path.read_text(encoding="utf-8"))
            old_frame_files = [(f["index"], f["file"]) for f in meta["frames"]]

        old_name = old_path.name
        old_path.rename(new_path)
        self.selected_anim = new_path

        # Rename frame files to [new_name]-NNN.png and update frames.json
        new_meta_path = new_path / "frames.json"
        if new_meta_path.exists():
            meta = json.loads(new_meta_path.read_text(encoding="utf-8"))
            for f in meta["frames"]:
                old_file = new_path / f["file"]
                new_file_name = f"{new_name}-{f['index']:03d}.png"
                if old_file.exists() and old_file.name != new_file_name:
                    old_file.rename(new_path / new_file_name)
                f["file"] = new_file_name
            new_meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        # Keep managed/flagged animation name lists in sync
        self._managed_anims = [new_name if n == old_name else n
                                for n in self._managed_anims]
        if old_name in self._flagged_anims:
            self._flagged_anims.discard(old_name)
            self._flagged_anims.add(new_name)

        def _undo_rename(op=old_path, np_=new_path,
                         old_files=old_frame_files, o_name=old_name, n_name=new_name):
            # Restore frame file names in frames.json, then rename folder back
            undo_meta = np_ / "frames.json"
            if undo_meta.exists() and old_files:
                meta = json.loads(undo_meta.read_text(encoding="utf-8"))
                old_by_idx = dict(old_files)
                for f in meta["frames"]:
                    old_fname = old_by_idx.get(f["index"])
                    if old_fname and f["file"] != old_fname:
                        cur = np_ / f["file"]
                        if cur.exists():
                            cur.rename(np_ / old_fname)
                        f["file"] = old_fname
                undo_meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            np_.rename(op)
            # Restore managed/flagged lists
            self._managed_anims = [o_name if n == n_name else n
                                    for n in self._managed_anims]
            if n_name in self._flagged_anims:
                self._flagged_anims.discard(n_name)
                self._flagged_anims.add(o_name)
            self._select_anim_by_path(op)

        self._push_undo(f"Rename '{old_name}' → '{new_name}'", _undo_rename)
        self._mark_dirty()
        self._load_output()
        for i, d in enumerate(self._anim_dirs):
            if d == new_path:
                self.anim_list.selection_clear(0, tk.END)
                self.anim_list.selection_set(i)
                self.anim_list.see(i)
                break
        self.lbl_anim.config(text=new_name)
        self._set_status(f"Renamed to '{new_name}'.")

    def _duplicate_anim(self):
        if not self.selected_anim:
            messagebox.showwarning("No Animation", "Select an animation first.")
            return
        name = self.selected_anim.name
        dlg  = _InputDialog(self.root, "Duplicate Animation",
                            "New animation name:", f"{name}-copy")
        new_name = dlg.result
        if not new_name or new_name == name:
            return
        new_path = self.selected_anim.parent / new_name
        if new_path.exists():
            messagebox.showerror("Duplicate", f"'{new_name}' already exists.")
            return
        import shutil
        shutil.copytree(self.selected_anim, new_path)
        def _undo_dup_anim(p=new_path):
            shutil.rmtree(p)
            self._load_output()
        self._push_undo(f"Duplicate '{name}' → '{new_name}'", _undo_dup_anim)
        self._mark_dirty()
        self._load_output()
        for i, d in enumerate(self._anim_dirs):
            if d == new_path:
                self._select_list_item(i)
                break
        self._set_status(f"Duplicated '{name}' as '{new_name}'.")

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
        snap = _snapshot_anim_dir(self.selected_anim)
        path = self.selected_anim
        prev_idx = self._anim_dirs.index(path) if path in self._anim_dirs else 0
        was_managed = name in self._managed_anims
        was_flagged = name in self._flagged_anims
        shutil.rmtree(self.selected_anim)
        self._managed_anims = [n for n in self._managed_anims if n != name]
        self._flagged_anims.discard(name)
        self.selected_anim = None
        self.selected_frames.clear()
        self._last_clicked = None
        self.lbl_anim.config(text="")
        self.lbl_sel.config(text="")
        for w in self.frame_holder.winfo_children():
            w.destroy()
        self._frame_images.clear()
        self._frame_cells.clear()
        def _undo_delete_anim(p=path, s=snap, n=name, wm=was_managed, wf=was_flagged):
            _restore_anim_dir(p, s)
            if wm and n not in self._managed_anims:
                self._managed_anims.append(n)
            if wf:
                self._flagged_anims.add(n)
            self._select_anim_by_path(p)
        self._push_undo(f"Delete '{name}'", _undo_delete_anim)
        self._mark_dirty()
        self._load_output()
        if self._anim_dirs:
            self._select_list_item(max(0, prev_idx - 1))
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

        # Per-card vertical overhead: card.pady (3 top+3 bot=6) + index label (~14px)
        _CARD_H_OVERHEAD = 20
        # Per-card horizontal overhead: card.padx (3+3=6) + grid padx (3+3=6)
        _CARD_W_OVERHEAD = 12

        ch = self.canvas.winfo_height()
        cw = self.canvas.winfo_width()

        if pngs:
            # Open each image once to get both dimensions
            img_sizes = []
            for p in pngs:
                try:
                    im = Image.open(p)
                    img_sizes.append(im.size)   # (width, height)
                except Exception:
                    img_sizes.append((1, 1))
            max_h = max(s[1] for s in img_sizes)

            if ch > 20 and cw > 20 and max_h > 0:
                # Height fit: leave 8 px margin per side (16 px total) above card overhead
                scale_h = (ch - _CARD_H_OVERHEAD - 16) / max_h

                # Width fit: all frames visible at once, no horizontal scrolling needed
                total_src_w = sum(s[0] for s in img_sizes)
                avail_w = cw - len(pngs) * _CARD_W_OVERHEAD - 8
                scale_w = avail_w / total_src_w if total_src_w > 0 else scale_h

                self._frame_fit_scale = max(0.1, min(float(MAX_SCALE),
                                                     scale_h, scale_w))
            else:
                # Canvas not yet laid out — fall back to THUMB_H, refit once ready
                self._frame_fit_scale = max(float(MIN_SCALE),
                                            min(float(MAX_SCALE), THUMB_H / max(max_h, 1)))
                self.root.after(120,
                    lambda d=anim_dir: self._load_frames(d)
                    if self.selected_anim == d else None)

            scale = self._frame_fit_scale * self._frame_zoom

            # bottom_pad: space below all cards so the tallest card sits at
            #   screen_bottom = ch/2 + max_card_h/2  (i.e. tallest card centred).
            # All cards share the same bottom_pad → they are bottom-aligned.
            max_card_h = round(max_h * scale) + _CARD_H_OVERHEAD
            bottom_pad = max(4, (ch - max_card_h) // 2)
        else:
            scale      = self._frame_fit_scale * self._frame_zoom
            bottom_pad = 20

        # Keep the embedded window exactly as tall as the canvas so the grid
        # row has room to honour bottom_pad without overflow.
        if ch > 20:
            self.canvas.itemconfig(self._canvas_win, height=ch)
            self.frame_holder.rowconfigure(0, minsize=ch)

        if self._frame_zoom_lbl:
            self._frame_zoom_lbl.config(text=f"{round(scale * 100)}%")

        for i, png in enumerate(pngs):
            self._add_frame_card(i, png, scale, bottom_pad)

        self._drag_indicator = tk.Frame(self.frame_holder, bg=ACCENT,
                                        width=3, cursor="sb_h_double_arrow")

        self._update_sel_label()
        self.canvas.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._pv_reload()

    def _add_frame_card(self, idx: int, png_path: Path, scale: float,
                        bottom_pad: int = 6):
        try:
            photo = _make_thumb(png_path, scale)
        except Exception:
            photo = None

        card = tk.Frame(self.frame_holder, bg=BG_CARD,
                        padx=3, pady=3, cursor="hand2")
        # sticky=S + shared bottom_pad → all cards bottom-align at the same Y,
        # which positions the tallest card vertically centred in the view.
        card.grid(row=0, column=idx, sticky=tk.S, padx=3, pady=(0, bottom_pad))

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

    def _frame_arrow_key(self, direction: int, shift: bool) -> None:
        """Left/Right arrow navigation in the frames pane."""
        if self._focused_pane != "frames" or not self._frame_cells:
            return
        n = len(self._frame_cells)
        if self._last_clicked is not None:
            cursor = self._last_clicked
        else:
            cursor = 0 if direction > 0 else n - 1
        new_idx = cursor + direction
        if new_idx < 0 or new_idx >= n:
            return
        if shift:
            self.selected_frames.add(new_idx)
        else:
            self.selected_frames = {new_idx}
        self._last_clicked = new_idx
        self._refresh_cards()
        self._update_sel_label()
        self._scroll_card_into_view(new_idx)

    def _scroll_card_into_view(self, idx: int) -> None:
        """Scroll the frames canvas so the card at *idx* is visible."""
        if idx < 0 or idx >= len(self._frame_cells):
            return
        card = self._frame_cells[idx]
        sr = self.canvas.bbox("all")
        if not sr:
            return
        total_w = sr[2] - sr[0]
        if total_w <= 0:
            return
        card_x = card.winfo_x()
        card_w = card.winfo_width()
        canvas_w = self.canvas.winfo_width()
        view_left  = self.canvas.xview()[0] * total_w
        view_right = view_left + canvas_w
        if card_x < view_left:
            self.canvas.xview_moveto(card_x / total_w)
        elif card_x + card_w > view_right:
            self.canvas.xview_moveto((card_x + card_w - canvas_w) / total_w)

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_frame_canvas_configure(self, event):
        """On window resize, refit the current animation if one is loaded."""
        h = event.height
        if h > 1 and self.selected_anim:
            # Debounce: cancel any pending refit and schedule a fresh one
            if hasattr(self, "_frame_refit_id"):
                self.root.after_cancel(self._frame_refit_id)
            anim = self.selected_anim
            self._frame_refit_id = self.root.after(
                150, lambda d=anim: self._load_frames(d)
                if self.selected_anim == d else None)

    # ── frame zoom ────────────────────────────────────────────────────────────

    _FRAME_ZOOM_STEP = 1.25

    def _frame_zoom_in(self):
        self._frame_zoom = min(self._frame_zoom * self._FRAME_ZOOM_STEP, 16.0)
        if self.selected_anim:
            self._load_frames(self.selected_anim)

    def _frame_zoom_out(self):
        self._frame_zoom = max(self._frame_zoom / self._FRAME_ZOOM_STEP, 0.05)
        if self.selected_anim:
            self._load_frames(self.selected_anim)

    def _frame_zoom_fit(self):
        self._frame_zoom = 1.0
        if self.selected_anim:
            self._load_frames(self.selected_anim)

    # ── merge / split / delete frames ─────────────────────────────────────────

    def _merge_frames(self):
        if not self.selected_anim:
            return
        if len(self.selected_frames) < 2:
            messagebox.showwarning("Merge", "Select 2 or more frames to merge.")
            return
        indices = sorted(self.selected_frames)
        try:
            from extract_sprites import cmd_stitch
            snap = _snapshot_anim_dir(self.selected_anim)
            path = self.selected_anim
            cmd_stitch(self.selected_anim, indices)
            def _undo_merge(p=path, s=snap):
                _restore_anim_dir(p, s)
                self._select_anim_by_path(p)
            self._push_undo(f"Merge frames {indices} in '{path.name}'", _undo_merge)
            self._mark_dirty()
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
            import json as _json
            from extract_sprites import cmd_split, load_metadata
            meta = load_metadata(self.selected_anim)
            frame_meta = next(
                (f for f in meta["frames"] if f["index"] == idx), None)
            if frame_meta is None:
                messagebox.showerror("Split", f"Frame {idx} not found in frames.json.")
                return
            blobs = frame_meta.get("blobs", [])
            n_blobs = len(blobs)
            if n_blobs < 2:
                messagebox.showinfo(
                    "Split",
                    f"Frame {idx} has only 1 blob — nothing to split.\n\n"
                    "Use Merge to combine frames first if they were stitched "
                    "from multiple separate blobs.")
                return
            snap = _snapshot_anim_dir(self.selected_anim)
            path = self.selected_anim

            # If blobs were detected from the modified PNG (after transparency
            # replacement) rather than from the original sprite sheet, we crop
            # directly from the current PNG instead of re-rendering from the sheet.
            if blobs and blobs[0].get("png_local"):
                self._split_frame_from_png(self.selected_anim, frame_meta, meta)
            else:
                cmd_split(self.selected_anim, idx, split_x=None)

            def _undo_split(p=path, s=snap):
                _restore_anim_dir(p, s)
                self._select_anim_by_path(p)
            self._push_undo(f"Split frame {idx} in '{path.name}'", _undo_split)
            self._mark_dirty()
            self._set_status(f"Split frame {idx} into {n_blobs} frames.")
            self.selected_frames.clear()
            self._last_clicked = None
            self._load_frames(self.selected_anim)
        except Exception as exc:
            messagebox.showerror("Split Error", str(exc))

    def _split_frame_from_png(self, anim_dir: Path, frame_meta: dict, meta: dict):
        """Split a frame using PNG-local blob coords (set after transparency replacement).

        Crops each blob region from the current PNG and inserts them as new frames,
        replacing the original frame entry.
        """
        import json as _json

        blobs    = frame_meta["blobs"]   # list of {x0,y0,x1,y1, png_local:True}
        src_file = anim_dir / frame_meta["file"]
        src_img  = Image.open(src_file).convert("RGBA")
        src_idx  = frame_meta["index"]

        # Build list of all other frames (not the one being split)
        other_frames = [f for f in meta["frames"] if f["index"] != src_idx]

        # Find a gap in the index space large enough to fit N new frames.
        # Simplest: assign new indices starting from max_existing + 1.
        max_idx = max((f["index"] for f in meta["frames"]), default=0)

        new_frames_meta = []
        for i, blob in enumerate(blobs):
            x0, y0, x1, y1 = blob["x0"], blob["y0"], blob["x1"], blob["y1"]
            region = src_img.crop((x0, y0, x1, y1))
            new_idx  = max_idx + 1 + i
            new_file = f"{new_idx:03d}.png"
            region.save(anim_dir / new_file)
            new_frames_meta.append({
                "index": new_idx,
                "file":  new_file,
                "blobs": [{"x0": x0, "y0": y0, "x1": x1, "y1": y1,
                            "png_local": True}],
            })

        # Remove the source file and update frames.json
        src_file.unlink(missing_ok=True)
        meta["frames"] = other_frames + new_frames_meta
        # Re-number sequentially so indices stay compact
        meta["frames"].sort(key=lambda f: f["index"])
        for i, f in enumerate(meta["frames"]):
            old_file = anim_dir / f["file"]
            new_file = anim_dir / f"{i:03d}.png"
            if old_file != new_file:
                old_file.rename(new_file)
            f["index"] = i
            f["file"]  = f"{i:03d}.png"
        (anim_dir / "frames.json").write_text(
            _json.dumps(meta, indent=2), encoding="utf-8")

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
            snap = _snapshot_anim_dir(self.selected_anim)
            path = self.selected_anim
            _cmd_delete_frames(self.selected_anim, set(indices))
            def _undo_del_frames(p=path, s=snap):
                _restore_anim_dir(p, s)
                self._select_anim_by_path(p)
            self._push_undo(f"Delete frame(s) {indices} in '{path.name}'", _undo_del_frames)
            self._mark_dirty()
            self._set_status(f"Deleted {n} frame(s).")
            self.selected_frames.clear()
            self._last_clicked = None
            self._load_frames(self.selected_anim)
        except Exception as exc:
            messagebox.showerror("Delete Frames", str(exc))

    def _duplicate_frame(self):
        if not self.selected_anim:
            return
        if len(self.selected_frames) != 1:
            messagebox.showwarning("Duplicate", "Select exactly 1 frame to duplicate.")
            return
        idx = next(iter(self.selected_frames))
        try:
            snap = _snapshot_anim_dir(self.selected_anim)
            path = self.selected_anim
            _cmd_duplicate_frame(self.selected_anim, idx)
            def _undo_dup_frame(p=path, s=snap):
                _restore_anim_dir(p, s)
                self._select_anim_by_path(p)
            self._push_undo(f"Duplicate frame {idx} in '{path.name}'", _undo_dup_frame)
            self._mark_dirty()
            self._set_status(f"Duplicated frame {idx} to end.")
            self.selected_frames.clear()
            self._last_clicked = None
            self._load_frames(self.selected_anim)
        except Exception as exc:
            messagebox.showerror("Duplicate Error", str(exc))

    # ── animation preview ─────────────────────────────────────────────────────

    def _pv_null_refs(self):
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

    # ── project palette panel ─────────────────────────────────────────────────

    _PAL_MAX = 512   # max distinct colors collected

    _PAL_COL_H    = 170  # px: height of expanded collapsible frame list
    _PAL_THUMB_W  = 44   # px: thumbnail width inside frame list

    def _build_palette_panel(self, parent: tk.Widget):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)   # vpane row expands

        # ── row 0: header + dial (fixed height) ───────────────────────────────
        top_ctrl = tk.Frame(parent, bg=BG_PANEL)
        top_ctrl.grid(row=0, column=0, sticky=tk.EW)
        top_ctrl.columnconfigure(0, weight=1)

        hdr = tk.Frame(top_ctrl, bg=BG_PANEL, pady=4)
        hdr.grid(row=0, column=0, sticky=tk.EW)
        self._pal_hdr_lbl = tk.Label(hdr, text="Palette", bg=BG_PANEL, fg=ACCENT,
                                     font=("", 10, "bold"))
        self._pal_hdr_lbl.pack(side=tk.LEFT, padx=8)
        self._pal_count_lbl = tk.Label(hdr, text="", bg=BG_PANEL,
                                       fg=FG_DIM, font=("", 8))
        self._pal_count_lbl.pack(side=tk.LEFT)

        dial_row = tk.Frame(top_ctrl, bg=BG_PANEL)
        dial_row.grid(row=1, column=0, sticky=tk.EW, padx=6, pady=(0, 2))
        tk.Label(dial_row, text="Colors", bg=BG_PANEL, fg=FG_DIM,
                 font=("", 8)).pack(side=tk.LEFT)
        self._pal_n_lbl = tk.Label(dial_row, text="16", bg=BG_PANEL,
                                   fg=FG, font=("Consolas", 8), width=4,
                                   anchor=tk.E)
        self._pal_n_lbl.pack(side=tk.RIGHT)
        tk.Scale(dial_row, variable=self._pal_n_colors,
                 from_=1, to=256,
                 orient=tk.HORIZONTAL, showvalue=False,
                 command=lambda _: self._pal_on_scale(),
                 bg=BG_PANEL, fg=FG, troughcolor=BG_CARD,
                 highlightthickness=0, relief=tk.FLAT,
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        self._pal_hover_label = tk.Label(top_ctrl, text="", bg=BG_PANEL,
                                         fg=FG_DIM, font=("Consolas", 7),
                                         anchor=tk.W)
        self._pal_hover_label.grid(row=2, column=0, sticky=tk.EW,
                                   padx=4, pady=(0, 2))

        # ── row 1: vertical pane — swatches (top) | frames (bottom) ───────────
        vpane = tk.PanedWindow(parent, orient=tk.VERTICAL,
                               bg=BG, sashwidth=5, sashrelief=tk.FLAT,
                               sashpad=1)
        vpane.grid(row=1, column=0, sticky=tk.NSEW)

        # ── top pane: swatch grid ─────────────────────────────────────────────
        sw_frame = tk.Frame(vpane, bg=BG_PANEL)
        sw_frame.rowconfigure(0, weight=1)
        sw_frame.columnconfigure(0, weight=1)
        vpane.add(sw_frame, minsize=60)

        vsb = tk.Scrollbar(sw_frame, orient=tk.VERTICAL,
                           bg=BG_CARD, troughcolor=BG_PANEL, relief=tk.FLAT)
        vsb.grid(row=0, column=1, sticky=tk.NS)
        self._pal_canvas = tk.Canvas(sw_frame, bg=BG_PANEL,
                                     highlightthickness=0,
                                     yscrollcommand=vsb.set)
        self._pal_canvas.grid(row=0, column=0, sticky=tk.NSEW)
        vsb.config(command=self._pal_canvas.yview)

        self._pal_canvas.bind("<Configure>",  lambda _: self._pal_redraw())
        self._pal_canvas.bind("<MouseWheel>",
                              lambda e: self._pal_canvas.yview_scroll(
                                  int(-1 * (e.delta / 120)), "units"))
        self._pal_canvas.bind("<Motion>",    self._pal_on_hover)
        self._pal_canvas.bind("<Leave>",     lambda _: self._pal_hover_update(""))
        self._pal_canvas.bind("<Button-1>",  self._pal_click)
        self._pal_canvas.bind("<Button-3>",  self._pal_right_click)
        self._pal_canvas.bind("<Button-1>",  lambda _: self._set_pane_focus("palette"), add="+")

        # ── bottom pane: frames with selected color ───────────────────────────
        col_frame = tk.Frame(vpane, bg=BG_PANEL)
        col_frame.rowconfigure(1, weight=1)
        col_frame.columnconfigure(0, weight=1)
        vpane.add(col_frame, minsize=60)

        col_hdr = tk.Frame(col_frame, bg=BG_PANEL, pady=2)
        col_hdr.grid(row=0, column=0, columnspan=2, sticky=tk.EW)
        self._pal_col_hdr_lbl = tk.Label(col_hdr, text="Frames",
                                          bg=BG_PANEL, fg=ACCENT,
                                          font=("", 9, "bold"), anchor=tk.W)
        self._pal_col_hdr_lbl.pack(side=tk.LEFT, padx=6)

        col_vsb = tk.Scrollbar(col_frame, orient=tk.VERTICAL,
                               bg=BG_CARD, troughcolor=BG_PANEL, relief=tk.FLAT)
        col_vsb.grid(row=1, column=1, sticky=tk.NS)
        self._pal_col_canvas = tk.Canvas(col_frame, bg=BG_CARD,
                                         highlightthickness=0,
                                         yscrollcommand=col_vsb.set)
        self._pal_col_canvas.grid(row=1, column=0, sticky=tk.NSEW)
        col_vsb.config(command=self._pal_col_canvas.yview)
        self._pal_col_canvas.bind("<MouseWheel>",
                                  lambda e: self._pal_col_canvas.yview_scroll(
                                      int(-1 * (e.delta / 120)), "units"))
        self._pal_col_canvas.bind("<Button-1>", self._pal_col_frame_click)

        # Show placeholder text in the frames pane
        self.root.after(50, self._pal_col_show_placeholder)

    def _refresh_palette(self):
        """Recompute palette from all frames (runs in background thread)."""
        anim_dirs = list(self._anim_dirs)

        def _compute():
            try:
                import numpy as np
                from collections import Counter
                counts: Counter = Counter()
                for anim_dir in anim_dirs:
                    for png in sorted(anim_dir.glob("*.png")):
                        try:
                            img = Image.open(png).convert("RGBA")
                            arr = np.array(img)
                            mask = arr[:, :, 3] > 128
                            if not mask.any():
                                continue
                            rgb = arr[mask, :3].astype(np.uint32)
                            packed = (rgb[:, 0] << 16) | (rgb[:, 1] << 8) | rgb[:, 2]
                            vals, cnts = np.unique(packed, return_counts=True)
                            for v, c in zip(vals.tolist(), cnts.tolist()):
                                counts[v] += c
                        except Exception:
                            pass
                top = counts.most_common(self._PAL_MAX)
                colors = [((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF, c)
                          for v, c in top]
                self.root.after(0, lambda cl=colors: self._pal_set(cl))
            except Exception:
                pass

        threading.Thread(target=_compute, daemon=True).start()

    def _pal_set(self, colors: list):
        self._pal_colors = colors
        self._pal_selected_idx = None
        if self._pal_col_hdr_lbl and self._pal_col_hdr_lbl.winfo_exists():
            self._pal_col_hdr_lbl.config(text="Frames", fg=ACCENT)
        self._pal_col_show_placeholder()
        self._pal_redraw()

    def _pal_on_scale(self):
        n = self._pal_n_colors.get()
        if self._pal_n_lbl and self._pal_n_lbl.winfo_exists():
            self._pal_n_lbl.config(text=str(n))
        self._pal_redraw()

    def _pal_hover_update(self, text: str):
        if self._pal_hover_label and self._pal_hover_label.winfo_exists():
            self._pal_hover_label.config(text=text)

    def _pal_event_to_idx(self, event) -> int | None:
        """Return the palette color index under the mouse, or None."""
        canvas = self._pal_canvas
        if not self._pal_colors or canvas is None:
            return None
        cw = canvas.winfo_width()
        if cw == 0:
            return None
        n_show = min(self._pal_n_colors.get(), len(self._pal_colors))
        cols = max(1, self._PAL_SWATCH_COLS)
        sz   = cw // cols
        if sz == 0:
            return None
        cy  = int(canvas.canvasy(event.y))
        col = event.x // sz
        row = cy // sz
        idx = row * cols + col
        if 0 <= col < cols and 0 <= idx < n_show:
            return idx
        return None

    def _pal_on_hover(self, event):
        idx = self._pal_event_to_idx(event)
        if idx is not None:
            r, g, b, count = self._pal_colors[idx]
            self._pal_hover_update(f"#{r:02x}{g:02x}{b:02x}  {count:,}")
        else:
            self._pal_hover_update("")

    def _pal_click(self, event):
        idx = self._pal_event_to_idx(event)
        if idx is None:
            return
        self._pal_selected_idx = idx
        self._pal_redraw()
        self._pal_load_color_frames()

    def _pal_right_click(self, event):
        idx = self._pal_event_to_idx(event)
        if idx is None:
            return
        self._pal_selected_idx = idx
        self._pal_redraw()
        self._pal_load_color_frames()
        r, g, b, _ = self._pal_colors[idx]
        hex_col = f"#{r:02x}{g:02x}{b:02x}"
        menu = tk.Menu(self.root, tearoff=False,
                       bg=BG_PANEL, fg=FG,
                       activebackground=BG_SEL, activeforeground=ACCENT,
                       relief=tk.FLAT, borderwidth=1)
        menu.add_command(
            label=f"Replace {hex_col}…",
            command=lambda: self._pal_replace_color(r, g, b))
        menu.add_command(
            label=f"Replace {hex_col} with Transparency",
            command=lambda: self._pal_replace_transparent(r, g, b))
        menu.post(event.x_root, event.y_root)

    def _pal_replace_color(self, old_r: int, old_g: int, old_b: int):
        from tkinter.colorchooser import askcolor
        init = f"#{old_r:02x}{old_g:02x}{old_b:02x}"
        result = askcolor(color=init,
                          title=f"Replace {init} with…",
                          parent=self.root)
        if result is None or result[0] is None:
            return
        new_r, new_g, new_b = (int(x) for x in result[0])
        if (new_r, new_g, new_b) == (old_r, old_g, old_b):
            return

        anim_dirs = list(self._anim_dirs)
        self._set_status(f"Replacing {init}…")

        def do_replace():
            import numpy as np
            # Collect which dirs are actually affected (for snapshots / undo)
            affected: list[tuple[Path, dict]] = []
            for anim_dir in anim_dirs:
                pngs = sorted(anim_dir.glob("*.png"))
                dir_snap = None
                for png in pngs:
                    try:
                        img = Image.open(png).convert("RGBA")
                        arr = np.array(img)
                        mask = ((arr[:, :, 3] > 128) &
                                (arr[:, :, 0] == old_r) &
                                (arr[:, :, 1] == old_g) &
                                (arr[:, :, 2] == old_b))
                        if not mask.any():
                            continue
                        if dir_snap is None:
                            dir_snap = _snapshot_anim_dir(anim_dir)
                        arr[mask, 0] = new_r
                        arr[mask, 1] = new_g
                        arr[mask, 2] = new_b
                        Image.fromarray(arr).save(png)
                    except Exception:
                        pass
                if dir_snap is not None:
                    affected.append((anim_dir, dir_snap))

            def on_done():
                if not affected:
                    self._set_status(f"No pixels matched {init}.")
                    return
                # Push undo for all affected dirs together
                def _undo(af=affected):
                    for p, s in af:
                        _restore_anim_dir(p, s)
                    self._load_frames(self.selected_anim) \
                        if self.selected_anim else None
                    self._refresh_palette()
                new_hex = f"#{new_r:02x}{new_g:02x}{new_b:02x}"
                self._push_undo(f"Replace {init} → {new_hex}", _undo)
                self._mark_dirty()
                self._pal_selected_idx = None
                self._pal_load_color_frames()
                if self.selected_anim:
                    self._load_frames(self.selected_anim)
                self._refresh_palette()
                self._set_status(
                    f"Replaced {init} → {new_hex} in {len(affected)} animation(s).")
            self.root.after(0, on_done)

        threading.Thread(target=do_replace, daemon=True).start()

    def _pal_replace_transparent(self, old_r: int, old_g: int, old_b: int):
        """Replace every pixel of `old_r,g,b` with transparency across all frames.

        After erasing, re-detect blobs in each modified PNG and update frames.json
        so the Split button can immediately separate the newly disconnected regions.
        Blobs detected this way are stored with ``"png_local": true`` so the split
        logic knows to crop from the current PNG rather than re-render from the
        original sprite sheet.
        """
        import json as _json
        import numpy as _np

        init     = f"#{old_r:02x}{old_g:02x}{old_b:02x}"
        anim_dirs = list(self._anim_dirs)
        self._set_status(f"Replacing {init} with transparency…")

        def _detect_blobs_in_png(png_path: Path) -> list[dict]:
            """Column-scan the PNG alpha channel; return PNG-local blob rects."""
            arr   = _np.array(Image.open(png_path).convert("RGBA"))
            alpha = arr[:, :, 3]
            col_has_fg = (alpha > 0).any(axis=0)
            blobs, in_b, sx = [], False, 0
            for cx, fg in enumerate(col_has_fg):
                if fg and not in_b:
                    sx, in_b = cx, True
                elif not fg and in_b:
                    rows = _np.where(alpha[:, sx:cx].any(axis=1))[0]
                    if rows.size:
                        blobs.append({"x0": sx, "y0": int(rows[0]),
                                      "x1": cx, "y1": int(rows[-1] + 1),
                                      "png_local": True})
                    in_b = False
            if in_b:
                rows = _np.where(alpha[:, sx:].any(axis=1))[0]
                if rows.size:
                    blobs.append({"x0": sx, "y0": int(rows[0]),
                                  "x1": int(alpha.shape[1]), "y1": int(rows[-1] + 1),
                                  "png_local": True})
            return blobs

        def do_replace():
            affected: list[tuple[Path, dict]] = []

            for anim_dir in anim_dirs:
                meta_path = anim_dir / "frames.json"
                if not meta_path.exists():
                    continue
                try:
                    meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    continue

                pngs = sorted(anim_dir.glob("*.png"))
                dir_snap = None
                meta_changed = False

                for png in pngs:
                    try:
                        img = Image.open(png).convert("RGBA")
                        arr = _np.array(img)
                        mask = ((arr[:, :, 3] > 128) &
                                (arr[:, :, 0] == old_r) &
                                (arr[:, :, 1] == old_g) &
                                (arr[:, :, 2] == old_b))
                        if not mask.any():
                            continue
                        if dir_snap is None:
                            dir_snap = _snapshot_anim_dir(anim_dir)
                        arr[mask, 3] = 0       # erase alpha
                        arr[mask, 0] = 0
                        arr[mask, 1] = 0
                        arr[mask, 2] = 0
                        Image.fromarray(arr).save(png)

                        # Re-detect blobs in the now-modified PNG
                        new_blobs = _detect_blobs_in_png(png)
                        # Update frames.json entry for this PNG
                        stem = png.stem
                        for fm in meta["frames"]:
                            if Path(fm["file"]).stem == stem:
                                fm["blobs"] = new_blobs
                                meta_changed = True
                                break
                    except Exception:
                        pass

                if dir_snap is not None:
                    if meta_changed:
                        meta_path.write_text(
                            _json.dumps(meta, indent=2), encoding="utf-8")
                    affected.append((anim_dir, dir_snap))

            def on_done():
                if not affected:
                    self._set_status(f"No pixels matched {init}.")
                    return
                def _undo(af=affected):
                    for p, s in af:
                        _restore_anim_dir(p, s)
                    if self.selected_anim:
                        self._load_frames(self.selected_anim)
                    self._refresh_palette()
                self._push_undo(f"Replace {init} → transparent", _undo)
                self._mark_dirty()
                self._pal_selected_idx = None
                self._pal_load_color_frames()
                if self.selected_anim:
                    self._load_frames(self.selected_anim)
                self._refresh_palette()
                self._set_status(
                    f"Replaced {init} with transparency in {len(affected)} animation(s)."
                    "  Frames with multiple regions can now be Split.")
            self.root.after(0, on_done)

        threading.Thread(target=do_replace, daemon=True).start()

    # ── color frames section ──────────────────────────────────────────────────

    def _pal_col_show_placeholder(self):
        canvas = self._pal_col_canvas
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        self._pal_col_img_refs.clear()
        cw = canvas.winfo_width() or (self._PAL_PANEL_W - 16)
        canvas.create_text(cw // 2, 20,
                           text="Click a swatch\nto see frames",
                           fill=FG_DIM, font=("", 8), justify=tk.CENTER)
        canvas.configure(scrollregion=(0, 0, cw, 40))

    def _pal_load_color_frames(self):
        """Find all frames containing the selected color (background thread)."""
        if self._pal_selected_idx is None or not self._pal_colors:
            return
        r, g, b, _ = self._pal_colors[self._pal_selected_idx]
        hex_col = f"#{r:02x}{g:02x}{b:02x}"

        if self._pal_col_hdr_lbl and self._pal_col_hdr_lbl.winfo_exists():
            self._pal_col_hdr_lbl.config(text=f"Frames  {hex_col}", fg=ACCENT)

        if self._pal_col_canvas and self._pal_col_canvas.winfo_exists():
            self._pal_col_canvas.delete("all")
            cw = self._pal_col_canvas.winfo_width() or (self._PAL_PANEL_W - 16)
            self._pal_col_canvas.create_text(
                cw // 2, 20, text="Searching…", fill=FG_DIM, font=("", 8))

        anim_dirs = list(self._anim_dirs)

        def find():
            import numpy as np
            matches: list[tuple[str, Path]] = []  # (anim_name, png_path)
            for anim_dir in anim_dirs:
                for png in sorted(anim_dir.glob("*.png")):
                    try:
                        img = Image.open(png).convert("RGBA")
                        arr = np.array(img)
                        hit = ((arr[:, :, 3] > 128) &
                               (arr[:, :, 0] == r) &
                               (arr[:, :, 1] == g) &
                               (arr[:, :, 2] == b))
                        if hit.any():
                            matches.append((anim_dir.name, png))
                    except Exception:
                        pass
            self.root.after(
                0, lambda m=matches: self._pal_show_color_frames(m, r, g, b))

        threading.Thread(target=find, daemon=True).start()

    def _pal_show_color_frames(self, matches: list, r: int, g: int, b: int):
        canvas = self._pal_col_canvas
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        self._pal_col_img_refs.clear()

        hex_col = f"#{r:02x}{g:02x}{b:02x}"
        n = len(matches)
        if self._pal_col_hdr_lbl and self._pal_col_hdr_lbl.winfo_exists():
            self._pal_col_hdr_lbl.config(
                text=f"Frames  {hex_col}  ({n})", fg=ACCENT)

        if not matches:
            cw = canvas.winfo_width() or self._PAL_PANEL_W
            canvas.create_text(cw // 2, 20,
                               text="No frames found", fill=FG_DIM, font=("", 8))
            canvas.configure(scrollregion=(0, 0, cw, 40))
            return

        cw      = canvas.winfo_width() or (self._PAL_PANEL_W - 16)
        tw      = self._PAL_THUMB_W
        row_h   = tw + 4          # square thumb + gap
        text_x  = tw + 6
        text_w  = max(cw - tw - 8, 20)

        # Panel BG colour for alpha compositing
        panel_rgb = tuple(
            int(BG_CARD.lstrip("#")[i:i+2], 16) for i in (0, 2, 4))

        for i, (anim_name, png_path) in enumerate(matches):
            y0 = i * row_h
            try:
                img = Image.open(png_path).convert("RGBA")
                scale = tw / max(img.width, img.height)
                nw = max(1, round(img.width  * scale))
                nh = max(1, round(img.height * scale))
                img = img.resize((nw, nh), Image.NEAREST)
                bg  = Image.new("RGBA", (nw, nh), (*panel_rgb, 255))
                composite = Image.alpha_composite(bg, img)
                photo = ImageTk.PhotoImage(composite)
                self._pal_col_img_refs.append(photo)
                canvas.create_image(2, y0 + 2, image=photo, anchor=tk.NW,
                                    tags=(f"frame_{i}",))
            except Exception:
                self._pal_col_img_refs.append(None)

            frame_num = png_path.stem
            # Truncate anim name to fit
            short_name = (anim_name[:10] + "…") if len(anim_name) > 11 else anim_name
            canvas.create_text(text_x, y0 + 4,
                               text=short_name,
                               fill=FG, font=("Consolas", 7),
                               anchor=tk.NW, width=text_w,
                               tags=(f"frame_{i}",))
            canvas.create_text(text_x, y0 + 14,
                               text=frame_num,
                               fill=FG_DIM, font=("Consolas", 7),
                               anchor=tk.NW,
                               tags=(f"frame_{i}",))

        # Store match list for click handler
        self._pal_col_matches = matches
        canvas.configure(scrollregion=(0, 0, cw, n * row_h))

    def _pal_col_frame_click(self, event):
        """Navigate to the animation whose frame was clicked in the list."""
        canvas = self._pal_col_canvas
        if canvas is None or not hasattr(self, "_pal_col_matches"):
            return
        cw = canvas.winfo_width() or self._PAL_PANEL_W
        row_h = self._PAL_THUMB_W + 4
        cy  = int(canvas.canvasy(event.y))
        idx = cy // row_h
        if 0 <= idx < len(self._pal_col_matches):
            anim_name, png_path = self._pal_col_matches[idx]
            anim_dir = png_path.parent
            self._select_anim_by_path(anim_dir)

    def _pal_redraw(self):
        canvas = self._pal_canvas
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete("all")

        n_show = min(self._pal_n_colors.get(), len(self._pal_colors))

        if self._pal_n_lbl and self._pal_n_lbl.winfo_exists():
            self._pal_n_lbl.config(text=str(self._pal_n_colors.get()))
        if self._pal_count_lbl and self._pal_count_lbl.winfo_exists():
            total = len(self._pal_colors)
            self._pal_count_lbl.config(
                text=f"{n_show}/{total}" if total else "")

        if not n_show:
            cw = canvas.winfo_width() or self._PAL_PANEL_W
            canvas.create_text(cw // 2, 24,
                               text="No data", fill=FG_DIM, font=("", 9))
            canvas.configure(scrollregion=(0, 0, cw, 48))
            return

        cw   = canvas.winfo_width() or self._PAL_PANEL_W
        cols = max(1, self._PAL_SWATCH_COLS)
        sz   = cw // cols    # square side length
        sel  = self._pal_selected_idx

        for i in range(n_show):
            r, g, b, _ = self._pal_colors[i]
            row = i // cols
            col = i % cols
            x0  = col * sz
            y0  = row * sz
            canvas.create_rectangle(x0, y0, x0 + sz, y0 + sz,
                                    fill=f"#{r:02x}{g:02x}{b:02x}",
                                    outline="")
            if i == sel:
                # White selection ring (2 px inset)
                canvas.create_rectangle(x0 + 2, y0 + 2,
                                        x0 + sz - 2, y0 + sz - 2,
                                        fill="", outline="#ffffff", width=2)

        rows = (n_show + cols - 1) // cols
        canvas.configure(scrollregion=(0, 0, cw, rows * sz))

    def _build_preview_panel(self, parent: tk.Widget):
        """Build (or rebuild) the preview panel inside *parent*."""
        self._pv_pause()
        self._pv_null_refs()
        for w in list(parent.winfo_children()):
            self._safe_destroy(w)

        hdr = tk.Frame(parent, bg=BG_PANEL, pady=4)
        hdr.pack(fill=tk.X)
        self._pv_hdr_lbl = tk.Label(hdr, text="Preview", bg=BG_PANEL, fg=ACCENT,
                                    font=("", 10, "bold"))
        self._pv_hdr_lbl.pack(side=tk.LEFT, padx=8)
        is_detached = isinstance(parent, tk.Toplevel)
        if is_detached:
            self._btn(hdr, "Reattach", self._pv_reattach,
                      YELLOW, small=True).pack(side=tk.RIGHT, padx=4)
        else:
            self._btn(hdr, "Detach [+]", self._pv_detach,
                      FG_DIM, small=True).pack(side=tk.RIGHT, padx=4)

        self._pv_canvas = tk.Canvas(parent, bg=BG_PANEL, highlightthickness=0)
        self._pv_canvas.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        self._pv_canvas.bind("<Configure>", lambda _: self._pv_render())
        self._pv_canvas.bind("<Button-1>",  lambda _: self._set_pane_focus("preview"), add="+")

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

        # Restore focus highlight if preview pane was focused
        if self._pv_hdr_lbl and self._focused_pane == "preview":
            self._pv_hdr_lbl.config(fg=GREEN)

        if self._pv_frames:
            self._pv_render()

    def _pv_update_delay_label(self):
        ms = self._pv_delay.get()
        if self._pv_lbl_delay and self._pv_lbl_delay.winfo_exists():
            self._pv_lbl_delay.config(text=f"{ms} ms")

    def _pv_measure_max(self):
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

        max_w = self._pv_max_w or img.width
        max_h = self._pv_max_h or img.height
        scale = min(cw / max_w, ch / max_h)
        scale = max(scale, 1.0)
        scale = min(scale, MAX_SCALE)

        w = max(1, round(img.width  * scale))
        h = max(1, round(img.height * scale))
        img = img.resize((w, h), Image.NEAREST)

        canvas_bg = canvas.cget("bg")
        panel_rgb = tuple(
            int(canvas_bg.lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
        bg_img    = Image.new("RGBA", (w, h), (*panel_rgb, 255))
        composited = Image.alpha_composite(bg_img, img)
        self._pv_photo = ImageTk.PhotoImage(composited)

        canvas.delete("all")
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
        if self._pv_toplevel and self._pv_toplevel.winfo_exists():
            self._pv_toplevel.lift()
            return
        was_playing = self._pv_playing

        self._pv_pause()
        self._pv_null_refs()

        for w in list(self._pv_pane.winfo_children()):
            self._safe_destroy(w)
        tk.Label(self._pv_pane, text="Preview detached",
                 bg=BG_PANEL, fg=FG_DIM, font=("", 9)).pack(pady=20)
        self._btn(self._pv_pane, "Reattach [-]", self._pv_reattach,
                  YELLOW, small=True).pack()

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
        was_playing = self._pv_playing
        self._pv_pause()
        self._pv_null_refs()
        tl = self._pv_toplevel
        self._pv_toplevel = None
        self._safe_destroy(tl)
        self._build_preview_panel(self._pv_pane)
        if was_playing:
            self._pv_play()

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

    # ── drag-to-reorder ───────────────────────────────────────────────────────

    _DRAG_THRESHOLD = 6

    def _drag_press(self, event, idx: int):
        self._on_click(event, idx)
        self._drag_src      = idx
        self._drag_dst      = None
        self._drag_active   = False
        self._drag_start_xy = (event.x_root, event.y_root)

    def _drag_motion(self, event, __idx: int):
        if self._drag_src is None:
            return
        dx = abs(event.x_root - self._drag_start_xy[0])
        if not self._drag_active and dx < self._DRAG_THRESHOLD:
            return
        self._drag_active = True
        holder_x = event.x_root - self.frame_holder.winfo_rootx()
        self._drag_dst = self._drag_find_target(holder_x)
        self._drag_show_indicator(self._drag_dst)

    def _drag_release(self, __event, __idx: int):
        if not self._drag_active or self._drag_src is None:
            self._drag_cancel()
            return
        src, dst = self._drag_src, self._drag_dst
        self._drag_cancel()
        if src is not None and dst is not None and dst != src and dst != src + 1:
            try:
                snap = _snapshot_anim_dir(self.selected_anim)
                path = self.selected_anim
                _cmd_reorder_frames(self.selected_anim, src, dst)
                def _undo_reorder(p=path, s=snap):
                    _restore_anim_dir(p, s)
                    self._select_anim_by_path(p)
                self._push_undo(f"Reorder frames in '{path.name}'", _undo_reorder)
                self._mark_dirty()
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
        for i, card in enumerate(self._frame_cells):
            cx = card.winfo_x() + card.winfo_width() // 2
            if mouse_x < cx:
                return i
        return len(self._frame_cells)

    def _drag_show_indicator(self, target_idx: int):
        ind   = self._drag_indicator
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
            x    = last.winfo_x() + last.winfo_width() + 1
        ind.place(x=x, y=0, width=4, height=h)
        ind.lift()

    # ── context menus ─────────────────────────────────────────────────────────

    def _context_menu(self, items: list) -> tk.Menu:
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
        idx = self.anim_list.nearest(event.y)
        if idx < 0:
            return
        self.anim_list.selection_clear(0, tk.END)
        self.anim_list.selection_set(idx)
        self._on_anim_select()
        is_flagged = (self.selected_anim is not None and
                      self.selected_anim.name in self._flagged_anims)
        items = [
            ("Rename…",         self._rename_folder),
            ("Duplicate",       self._duplicate_anim),
            ("Open in Compose", lambda: self._open_compose(self.selected_anim)),
            None,
        ]
        if is_flagged:
            items += [
                ("Mark as Valid",       self._mark_valid_anim),
                ("Delete (false pos.)", self._delete_anim_no_confirm),
            ]
        else:
            items.append(("Delete", self._delete_anim))
        menu = self._context_menu(items)
        menu.post(event.x_root, event.y_root)

    def _mark_valid_anim(self):
        if not self.selected_anim:
            return
        name = self.selected_anim.name
        self._flagged_anims.discard(name)
        # restore normal colour in listbox
        for i, d in enumerate(self._anim_dirs):
            if d.name == name:
                self.anim_list.itemconfig(i, fg=FG)
                break
        self._mark_dirty()
        self._set_status(f"'{name}' marked as valid.")

    def _delete_anim_no_confirm(self):
        """Delete a (flagged) animation without asking for confirmation."""
        if not self.selected_anim:
            return
        name = self.selected_anim.name
        snap = _snapshot_anim_dir(self.selected_anim)
        path = self.selected_anim
        prev_idx = self._anim_dirs.index(path) if path in self._anim_dirs else 0
        was_managed = name in self._managed_anims
        shutil.rmtree(self.selected_anim)
        self._managed_anims = [n for n in self._managed_anims if n != name]
        self._flagged_anims.discard(name)
        self.selected_anim = None
        self.selected_frames.clear()
        self._last_clicked = None
        self.lbl_anim.config(text="")
        self.lbl_sel.config(text="")
        for w in self.frame_holder.winfo_children():
            w.destroy()
        self._frame_images.clear()
        self._frame_cells.clear()
        def _undo_del(p=path, s=snap, n=name, wm=was_managed):
            _restore_anim_dir(p, s)
            if wm and n not in self._managed_anims:
                self._managed_anims.append(n)
            self._flagged_anims.add(n)
            self._select_anim_by_path(p)
        self._push_undo(f"Delete '{name}'", _undo_del)
        self._mark_dirty()
        self._load_output()
        if self._anim_dirs:
            self._select_list_item(max(0, prev_idx - 1))
        self._set_status(f"Deleted '{name}'.")

    def _frame_right_click(self, event, idx: int):
        if idx not in self.selected_frames:
            self.selected_frames = {idx}
            self._last_clicked = idx
            self._refresh_cards()
            self._update_sel_label()
        n = len(self.selected_frames)
        menu = self._context_menu([
            ("Edit Frame…",           lambda i=idx: self._open_frame_edit(i), n == 1),
            None,
            ("Split",                self._split_frame,            n == 1),
            (f"Merge {n} frames",    self._merge_frames,           n >= 2),
            ("Duplicate",            self._duplicate_frame,        n == 1),
            None,
            (f"Delete {n} frame(s)", self._delete_selected_frames, True),
        ])
        menu.post(event.x_root, event.y_root)

    def _open_frame_edit(self, frame_idx: int):
        if not self.selected_anim:
            return
        pngs = sorted(self.selected_anim.glob("*.png"))
        if frame_idx >= len(pngs):
            return
        frame_path = pngs[frame_idx]
        anim_dir   = self.selected_anim

        meta_path  = anim_dir / "frames.json"

        def _make_save_fn(fp):
            """Return an on_save closure bound to a specific frame path."""
            def on_save(result_img: Image.Image, replace: bool, hitboxes: list):
                snap = _snapshot_anim_dir(anim_dir)
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if replace:
                    result_img.save(fp)
                    tgt = next(
                        (f for f in meta["frames"] if f["file"] == fp.name), None)
                    if tgt is not None:
                        tgt["hitboxes"] = [dict(h) for h in hitboxes]
                    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                    op = f"Edit (replace) frame {fp.name}"
                else:
                    new_i = max(f["index"] for f in meta["frames"]) + 1
                    sample = meta["frames"][0]["file"] if meta["frames"] else ""
                    anim_name = anim_dir.name
                    new_file = (f"{anim_name}-{new_i:03d}.png"
                                if sample.startswith(anim_name + "-")
                                else f"{new_i:03d}.png")
                    result_img.save(anim_dir / new_file)
                    src = next(
                        (f for f in meta["frames"] if f["file"] == fp.name), None)
                    meta["frames"].append({
                        "index":    new_i,
                        "file":     new_file,
                        "blobs":    src["blobs"] if src else [],
                        "hitboxes": [dict(h) for h in hitboxes],
                    })
                    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                    op = f"Edit (new) from {fp.name}"

                def _undo(p=anim_dir, s=snap):
                    _restore_anim_dir(p, s)
                    self._select_anim_by_path(p)

                self._push_undo(op, _undo)
                self._mark_dirty()
                self._load_frames(anim_dir)
            return on_save

        def get_frame_data(new_idx: int):
            fp = pngs[new_idx]
            fm = {}
            if meta_path.exists():
                _fmeta = json.loads(meta_path.read_text(encoding="utf-8"))
                fm = next(
                    (f for f in _fmeta["frames"] if f["file"] == fp.name), {})
            return fp, fm, _make_save_fn(fp)

        frame_meta = {}
        if meta_path.exists():
            _fmeta = json.loads(meta_path.read_text(encoding="utf-8"))
            frame_meta = next(
                (f for f in _fmeta["frames"] if f["file"] == frame_path.name), {})

        # Build palette from the project's top 16 colors (if available)
        proj_palette = [(f"#{r:02x}{g:02x}{b:02x}",
                         f"#{r:02x}{g:02x}{b:02x}")
                        for r, g, b, _ in self._pal_colors[:16]] or None

        from frame_edit_window import FrameEditWindow
        FrameEditWindow(self.root, frame_path, _make_save_fn(frame_path),
                        frame_meta=frame_meta, palette=proj_palette,
                        frame_list=pngs, frame_index=frame_idx,
                        get_frame_data=get_frame_data)
