"""
slicer_window.py — Slicer dialog for gooey-sprites.

Workflow
--------
1.  Open against a single source frame.
2.  Build a named-node tree (groups → parts).
3.  Draw lasso or box selections; Extract to save PNGs.
4.  Previous-slice outlines render as ghost overlays; each node has an eye toggle.
5.  Tree + geometry persists in a per-frame  .slicer.json  file.
6.  Export Pack copies extracted PNGs + source frame into a portable folder.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from pathlib import Path

import tkinter as tk
import tkinter.ttk as ttk
from tkinter import filedialog, messagebox, simpledialog

from PIL import Image, ImageDraw, ImageTk

try:
    import numpy as _np
    _HAS_NUMPY = True
except ImportError:
    _np = None          # type: ignore[assignment]
    _HAS_NUMPY = False

import slicer_export
from constants import (
    BG, BG_PANEL, BG_CARD, BG_SEL,
    FG, FG_DIM, ACCENT, RED, GREEN, YELLOW,
)

# ── constants ────────────────────────────────────────────────────────────────

_GRID_MINOR      = "#2a2a3e"
_GRID_MAJOR      = "#313150"
_BADGE_BG_LASSO  = YELLOW
_BADGE_BG_BOX    = ACCENT
_BADGE_FG        = "#1e1e2e"
_LASSO_STROKE    = YELLOW
_BOX_STROKE      = ACCENT
_GHOST_COLOR     = "#b0b8c8"
_GHOST_WIDTH     = 2

_ANGLE_COLOR     = "#f5a623"   # orange rotation arrow
_ANGLE_HANDLE_R  = 7           # drag-handle circle radius (px, screen space)

_THUMB_SIZE      = 56
_HANDLE_R        = 5
_HANDLE_SQ       = 5
_DASH            = (4, 3)
_GHOST_DASH      = (3, 5)

_ZOOM_LEVELS     = (1, 2, 4, 8, 16, 32)
_GRID_THRESHOLD  = 4
_MAJOR_THRESHOLD = 16

_SIDE_MIN = 160
_SIDE_MAX = 520

_EYE_ON  = "●"
_EYE_OFF = "○"

_PACK_NAME_RE = re.compile(r"^[a-z0-9_-]+$")
_PREFS_PATH   = Path.home() / ".gooey-sprites" / "prefs.json"


# ── helpers ──────────────────────────────────────────────────────────────────

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


def _poly_bbox(pts):
    xs = [p[0] for p in pts];  ys = [p[1] for p in pts]
    return int(min(xs)), int(min(ys)), math.ceil(max(xs)), math.ceil(max(ys))


def _ghost_origin(ghost: dict | None) -> list[int] | None:
    """Return [x, y] top-left of the bounding box from a ghost dict, or None."""
    if ghost is None:
        return None
    pts = ghost.get("pts", [])
    if not pts:
        return None
    if ghost.get("type") == "box" and len(pts) == 2:
        x0, y0 = pts[0];  x1, y1 = pts[1]
        return [int(min(x0, x1)), int(min(y0, y1))]
    xs = [p[0] for p in pts];  ys = [p[1] for p in pts]
    return [int(min(xs)), int(min(ys))]


def _new_node(w: int = 128, h: int = 128) -> dict:
    return {"w": w, "h": h, "ghost": None, "visible": True, "angle": None}


def _estimate_angle(
    png_path: Path,
    part_world:   tuple[float, float],
    parent_world: tuple[float, float],
    alpha_threshold: int = 32,
) -> float | None:
    """PCA-based dominant orientation of a part PNG, disambiguated by hierarchy.

    Returns angle in degrees (flopsy convention: 0° = right, 90° = up),
    or None if numpy is unavailable or the image has too few opaque pixels.
    """
    if not _HAS_NUMPY:
        return None
    try:
        arr = _np.array(Image.open(png_path).convert("RGBA"))
        ys, xs = _np.where(arr[:, :, 3] > alpha_threshold)
        if len(xs) < 16:
            return None

        # PCA in image space
        cx_i, cy_i = float(xs.mean()), float(ys.mean())
        coords = _np.stack([xs - cx_i, ys - cy_i], axis=1).astype(_np.float32)
        cov = _np.cov(coords.T)
        if _np.any(_np.isnan(cov)):
            return None
        _, vecs = _np.linalg.eigh(cov)
        axis_img = vecs[:, 1]          # largest eigenvalue = principal axis

        # Convert image-space axis → world-space (negate image Y)
        wax, way = float(axis_img[0]), -float(axis_img[1])

        # Disambiguate: dot with parent → child direction in world space
        dx = part_world[0] - parent_world[0]
        dy = part_world[1] - parent_world[1]
        if wax * dx + way * dy < 0:
            wax, way = -wax, -way

        return float(math.degrees(math.atan2(way, wax))) - 90.0
    except Exception:
        return None


# ── serialisation ─────────────────────────────────────────────────────────────

def _tree_to_json(tree_data: dict, group_visible: dict,
                  group_hierarchy: dict, source_frame: str) -> dict:
    groups = []
    for g, parts in tree_data.items():
        parts_list = []
        for name, nd in parts.items():
            entry: dict = {
                "name":    name,
                "canvas":  [nd["w"], nd["h"]],
                "visible": nd.get("visible", True),
                "slice":   nd.get("ghost"),   # "ghost" in memory, "slice" on disk
            }
            if nd.get("angle") is not None:
                entry["angle"] = nd["angle"]
            parts_list.append(entry)
        entry: dict = {
            "name":    g,
            "visible": group_visible.get(g, True),
            "parts":   parts_list,
        }
        hier = group_hierarchy.get(g)
        if hier:
            entry["hierarchy"] = hier
        groups.append(entry)
    return {"version": 1, "source_frame": source_frame, "groups": groups}


def _load_template_hierarchy(group_name: str) -> dict[str, str]:
    """Return the hierarchy dict for a named group from character-template.json, or {}."""
    tmpl_path = Path(__file__).parent / "character-template.json"
    try:
        tmpl = json.loads(tmpl_path.read_text(encoding="utf-8"))
        g = tmpl.get("groups", {}).get(group_name, {})
        if isinstance(g, dict):
            return g.get("hierarchy", {})
    except Exception:
        pass
    return {}


def _tree_from_json(data: dict) -> tuple[dict, dict, dict]:
    """Return (tree_data, group_visible, group_hierarchy) parsed from a slicer JSON dict."""
    tree_data:      dict[str, dict[str, dict]] = {}
    group_visible:  dict[str, bool] = {}
    group_hierarchy: dict[str, dict[str, str]] = {}
    for g in data.get("groups", []):
        gname = g["name"]
        group_visible[gname] = g.get("visible", True)
        tree_data[gname] = {}
        hier = g.get("hierarchy")
        if isinstance(hier, dict) and hier:
            group_hierarchy[gname] = hier
        else:
            # Fall back to template hierarchy for known group names
            tmpl_hier = _load_template_hierarchy(gname)
            if tmpl_hier:
                group_hierarchy[gname] = tmpl_hier
        for p in g.get("parts", []):
            pname = p["name"]
            canvas = p.get("canvas", [128, 128])
            tree_data[gname][pname] = {
                "w":       canvas[0],
                "h":       canvas[1],
                "ghost":   p.get("slice"),
                "visible": p.get("visible", True),
                "angle":   p.get("angle"),
            }
    return tree_data, group_visible, group_hierarchy


# ── main window ──────────────────────────────────────────────────────────────

class SlicerWindow:

    _SIDE_W = 240

    def __init__(self, parent: tk.Misc, source_png: Path, output_dir: Path,
                 slicer_json: Path | None = None):
        self._src_path   = source_png
        self._output_dir = output_dir

        # default slice dir: <anim_dir>/slices/<frame_stem>/
        anim_dir   = source_png.parent
        frame_stem = source_png.stem
        self._default_slice_dir = anim_dir / "slices" / frame_stem

        self._src_img: Image.Image = Image.open(source_png).convert("RGBA")

        # canvas state
        self._zoom_idx         = 1
        self._pan_x            = 0.0
        self._pan_y            = 0.0
        self._initial_fit_done = False

        # tool state
        self._tool = "lasso"

        # lasso (image-space)
        self._lasso_pts: list[tuple[float, float]] = []
        self._lasso_closed  = False
        self._lasso_mouse: tuple[float, float] | None = None

        # box (image-space)
        self._box_start: tuple[float, float] | None = None
        self._box_end:   tuple[float, float] | None = None
        self._box_dragging = False

        # pan drag
        self._panning     = False
        self._pan_start_x = 0
        self._pan_start_y = 0
        self._pan_orig_x  = 0.0
        self._pan_orig_y  = 0.0

        # grip resize
        self._grip_x0 = 0
        self._grip_w0 = self._SIDE_W

        # tk photo cache
        self._tk_photo: ImageTk.PhotoImage | None = None

        # tree state
        self._tree_data:      dict[str, dict[str, dict]] = {}
        self._group_visible:  dict[str, bool] = {}
        self._group_hierarchy: dict[str, dict[str, str]] = {}
        self._iid_to_node:  dict[str, tuple[str, str | None]] = {}
        self._node_to_iid:  dict[tuple[str, str | None], str] = {}
        self._tree_thumbs:  dict[str, ImageTk.PhotoImage] = {}

        # rotation drag state
        self._rot_handles:       list[dict] = []   # populated each redraw
        self._rot_dragging:      bool = False
        self._rot_drag_node:     tuple[str, str] | None = None
        self._rot_drag_ctr_img:  tuple[float, float] = (0.0, 0.0)

        # hover tooltip
        self._tooltip_win:      tk.Toplevel | None = None
        self._tooltip_after_id: str | None = None
        self._tooltip_last_iid: str = ""
        self._tooltip_photo:    ImageTk.PhotoImage | None = None

        # file / dirty state
        self._slicer_json_path: Path | None = None
        self._dirty = False

        # load slicer.json: explicit path first, then auto-detect by convention
        _candidates = (
            [slicer_json] if slicer_json is not None
            else [
                self._default_slice_dir / "slicer.json",
                self._default_slice_dir / f"{frame_stem}.slicer.json",
            ]
        )
        for _cand in _candidates:
            if _cand and _cand.exists():
                try:
                    data = json.loads(_cand.read_text(encoding="utf-8"))
                    self._tree_data, self._group_visible, self._group_hierarchy = _tree_from_json(data)
                    self._slicer_json_path = _cand
                except Exception:
                    pass
                break

        # ── window ───────────────────────────────────────────────────────────
        self._win = tk.Toplevel(parent)
        self._win.configure(bg=BG)
        self._win.geometry("1280x820")
        self._win.minsize(900, 600)
        self._win.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self._update_title()

        self._win.bind("<Escape>",   lambda _: self._clear_selection())
        self._win.bind("<l>",        lambda _: self._set_tool("lasso"))
        self._win.bind("<L>",        lambda _: self._set_tool("lasso"))
        self._win.bind("<b>",        lambda _: self._set_tool("box"))
        self._win.bind("<B>",        lambda _: self._set_tool("box"))
        self._win.bind("<Control-s>", lambda _: self._cmd_save())
        self._win.bind("<Control-S>", lambda _: self._cmd_save())

        self._load_frame()
        # Backfill angles for parts loaded from old slicer files that predate the field
        self._win.after(150, self._compute_missing_angles)

    # ── title / dirty ─────────────────────────────────────────────────────────

    def _update_title(self):
        name  = self._slicer_json_path.name if self._slicer_json_path else "unsaved"
        dirty = " *" if self._dirty else ""
        self._win.title(f"Slicer — {name}{dirty}")

    def _set_dirty(self):
        self._dirty = True
        self._update_title()

    # ── construction ──────────────────────────────────────────────────────────

    def _build(self):
        # ── header ───────────────────────────────────────────────────────────
        hdr = tk.Frame(self._win, bg=BG_PANEL, padx=8, pady=6)
        hdr.pack(fill=tk.X)

        # file buttons
        for txt, cmd in (
            ("Save",        self._cmd_save),
            ("Save As…",    self._cmd_save_as),
            ("Load…",       self._cmd_load),
            ("Export Pack…",self._cmd_export),
        ):
            tk.Button(hdr, text=txt, command=cmd,
                      bg=BG_CARD, fg=FG, relief=tk.FLAT,
                      padx=6, pady=2, font=("", 8),
                      cursor="hand2").pack(side=tk.LEFT, padx=(0, 2))

        tk.Frame(hdr, bg=FG_DIM, width=1).pack(side=tk.LEFT, fill=tk.Y,
                                                padx=(6, 8))

        tk.Label(hdr, text="Source:", bg=BG_PANEL, fg=FG_DIM,
                 font=("", 8)).pack(side=tk.LEFT)
        self._lbl_source = tk.Label(hdr, text="", bg=BG_PANEL, fg=FG,
                                    font=("Consolas", 9))
        self._lbl_source.pack(side=tk.LEFT, padx=(4, 12))

        self._lbl_zoom = tk.Label(hdr, text="2×", bg=BG_PANEL, fg=ACCENT,
                                  font=("Consolas", 9), width=4)
        self._lbl_zoom.pack(side=tk.LEFT, padx=(0, 12))

        tk.Label(hdr, text="Output:", bg=BG_PANEL, fg=FG_DIM,
                 font=("", 8)).pack(side=tk.LEFT, padx=(0, 2))
        self._v_slice_dir = tk.StringVar(value=str(self._default_slice_dir))
        tk.Entry(hdr, textvariable=self._v_slice_dir, width=36,
                 bg=BG_CARD, fg=FG, insertbackground=FG,
                 font=("Consolas", 8), relief=tk.FLAT).pack(side=tk.LEFT, padx=2)
        tk.Button(hdr, text="Browse…", command=self._browse_slice_dir,
                  bg=BG_CARD, fg=FG_DIM, relief=tk.FLAT, padx=6, pady=2,
                  font=("", 8), cursor="hand2").pack(side=tk.LEFT, padx=2)

        # ── body ─────────────────────────────────────────────────────────────
        body = tk.Frame(self._win, bg=BG)
        body.pack(fill=tk.BOTH, expand=True)

        self._side = tk.Frame(body, bg=BG_PANEL, width=self._SIDE_W)
        self._side.pack(side=tk.LEFT, fill=tk.Y)
        self._side.pack_propagate(False)
        self._build_side(self._side)

        grip = tk.Frame(body, bg=BG_CARD, width=5, cursor="sb_h_double_arrow")
        grip.pack(side=tk.LEFT, fill=tk.Y)
        grip.bind("<ButtonPress-1>", self._grip_press)
        grip.bind("<B1-Motion>",     self._grip_drag)

        canvas_f = tk.Frame(body, bg=BG)
        canvas_f.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._build_canvas(canvas_f)

        self._set_tool("lasso")

    def _build_side(self, parent: tk.Frame):
        PAD = 8

        # tree buttons
        hf = tk.Frame(parent, bg=BG_PANEL)
        hf.pack(fill=tk.X, padx=PAD, pady=(8, 2))
        tk.Label(hf, text="Parts", bg=BG_PANEL, fg=FG_DIM,
                 font=("", 7, "bold")).pack(side=tk.LEFT)

        bf = tk.Frame(parent, bg=BG_PANEL)
        bf.pack(fill=tk.X, padx=PAD, pady=(0, 2))
        for txt, cmd, col in (
            ("+ Group",    self._add_group,         ACCENT),
            ("+ Part",     self._add_part,          FG),
            ("Template…",  self._cmd_load_template, FG_DIM),
            ("▲",          self._move_node_up,      FG_DIM),
            ("▼",          self._move_node_down,    FG_DIM),
            ("✕",          self._delete_node,       RED),
        ):
            tk.Button(bf, text=txt, command=cmd, bg=BG_CARD, fg=col,
                      relief=tk.FLAT, font=("", 7), padx=4, pady=2,
                      cursor="hand2").pack(side=tk.LEFT, padx=(0, 2))

        # treeview style
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Slicer.Treeview",
                        background=BG_CARD, foreground=FG,
                        fieldbackground=BG_CARD, rowheight=64,
                        borderwidth=0, relief="flat", font=("Consolas", 8))
        style.map("Slicer.Treeview",
                  background=[("selected", BG_SEL)],
                  foreground=[("selected", ACCENT)])

        tf = tk.Frame(parent, bg=BG_PANEL)
        tf.pack(fill=tk.BOTH, expand=True, padx=PAD, pady=(0, 4))
        vsb = tk.Scrollbar(tf, orient=tk.VERTICAL, bg=BG_CARD,
                           troughcolor=BG_PANEL, relief=tk.FLAT)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._tv = ttk.Treeview(tf, style="Slicer.Treeview",
                                selectmode="browse", show="tree",
                                columns=("eye",), yscrollcommand=vsb.set)
        self._tv.column("#0",  stretch=True, minwidth=80)
        self._tv.column("eye", stretch=False, width=26, anchor="center")
        self._tv.pack(fill=tk.BOTH, expand=True)
        vsb.config(command=self._tv.yview)
        self._tv.tag_configure("group", foreground=ACCENT,
                               font=("Consolas", 8, "bold"))
        self._tv.tag_configure("part",  foreground=FG)
        self._tv.bind("<<TreeviewSelect>>", self._on_tree_select)
        self._tv.bind("<Button-1>",         self._tv_click)
        self._tv.bind("<Button-3>",         self._tv_context_menu)
        self._tv.bind("<Double-Button-1>",  self._tv_rename_inline)
        self._tv.bind("<Motion>",           self._tv_motion)
        self._tv.bind("<Leave>",            lambda _: self._hide_tooltip())

        # output size
        tk.Frame(parent, bg=FG_DIM, height=1).pack(fill=tk.X, padx=PAD, pady=(2, 4))
        sf = tk.Frame(parent, bg=BG_PANEL)
        sf.pack(fill=tk.X, padx=PAD, pady=(0, 2))
        for lbl, attr, default in (("W", "_v_out_w", "128"), ("H", "_v_out_h", "128")):
            tk.Label(sf, text=lbl, bg=BG_PANEL, fg=FG_DIM,
                     font=("", 7)).pack(side=tk.LEFT)
            v = tk.StringVar(value=default)
            setattr(self, attr, v)
            tk.Entry(sf, textvariable=v, width=5, bg=BG_CARD, fg=FG,
                     insertbackground=FG, font=("Consolas", 9),
                     relief=tk.FLAT).pack(side=tk.LEFT, padx=(2, 6))

        # tool buttons
        tk.Frame(parent, bg=FG_DIM, height=1).pack(fill=tk.X, padx=PAD, pady=4)
        tbf = tk.Frame(parent, bg=BG_PANEL)
        tbf.pack(fill=tk.X, padx=PAD)
        self._btn_lasso = tk.Button(
            tbf, text="✦ LASSO  [L]", command=lambda: self._set_tool("lasso"),
            bg=BG_CARD, fg=YELLOW, relief=tk.FLAT,
            padx=6, pady=4, font=("", 9, "bold"), cursor="hand2", anchor=tk.W)
        self._btn_lasso.pack(fill=tk.X, pady=2)
        self._btn_box = tk.Button(
            tbf, text="☐ BOX  [B]", command=lambda: self._set_tool("box"),
            bg=BG_CARD, fg=FG_DIM, relief=tk.FLAT,
            padx=6, pady=4, font=("", 9), cursor="hand2", anchor=tk.W)
        self._btn_box.pack(fill=tk.X, pady=2)

        # actions
        tk.Frame(parent, bg=FG_DIM, height=1).pack(fill=tk.X, padx=PAD, pady=4)
        tk.Button(parent, text="Extract", command=self._do_extract,
                  bg=BG_CARD, fg=GREEN, activeforeground=GREEN,
                  activebackground=BG_SEL, relief=tk.FLAT,
                  padx=10, pady=4, font=("", 9, "bold"),
                  cursor="hand2").pack(fill=tk.X, padx=PAD, pady=2)
        tk.Button(parent, text="Clear", command=self._clear_selection,
                  bg=BG_CARD, fg=RED, activeforeground=RED,
                  activebackground=BG_SEL, relief=tk.FLAT,
                  padx=10, pady=2, font=("", 8),
                  cursor="hand2").pack(fill=tk.X, padx=PAD, pady=2)

        # extracted log
        tk.Frame(parent, bg=FG_DIM, height=1).pack(fill=tk.X, padx=PAD, pady=4)
        tk.Label(parent, text="Extracted", bg=BG_PANEL, fg=FG_DIM,
                 font=("", 7, "bold")).pack(anchor=tk.W, padx=PAD)
        ef = tk.Frame(parent, bg=BG_PANEL)
        ef.pack(fill=tk.X, padx=PAD, pady=2)
        evsb = tk.Scrollbar(ef, orient=tk.VERTICAL, bg=BG_CARD,
                            troughcolor=BG_PANEL, relief=tk.FLAT)
        evsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._extracted_lb = tk.Listbox(
            ef, yscrollcommand=evsb.set, height=4,
            bg=BG_CARD, fg=GREEN, selectbackground=BG_SEL,
            selectforeground=ACCENT, relief=tk.FLAT,
            font=("Consolas", 7), activestyle="none")
        self._extracted_lb.pack(fill=tk.BOTH, expand=True)
        evsb.config(command=self._extracted_lb.yview)

        # preview
        tk.Frame(parent, bg=FG_DIM, height=1).pack(fill=tk.X, padx=PAD, pady=4)
        tk.Label(parent, text="Preview", bg=BG_PANEL, fg=FG_DIM,
                 font=("", 7, "bold")).pack(anchor=tk.W, padx=PAD)
        self._pv_canvas = tk.Canvas(parent, bg=BG_CARD,
                                    width=self._SIDE_W - 20,
                                    height=self._SIDE_W - 20,
                                    highlightthickness=0)
        self._pv_canvas.pack(padx=PAD, pady=4)
        self._pv_photo: ImageTk.PhotoImage | None = None

        self._refresh_tree()

    def _build_canvas(self, parent: tk.Frame):
        self._hbar = tk.Scrollbar(parent, orient=tk.HORIZONTAL, bg=BG_CARD,
                                  troughcolor=BG_PANEL, relief=tk.FLAT,
                                  command=self._hscroll)
        self._hbar.pack(side=tk.BOTTOM, fill=tk.X)
        self._vbar = tk.Scrollbar(parent, orient=tk.VERTICAL, bg=BG_CARD,
                                  troughcolor=BG_PANEL, relief=tk.FLAT,
                                  command=self._vscroll)
        self._vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._cv = tk.Canvas(parent, bg=BG, highlightthickness=0,
                             cursor="crosshair")
        self._cv.pack(fill=tk.BOTH, expand=True)

        self._cv.bind("<ButtonPress-1>",      self._cv_press)
        self._cv.bind("<B1-Motion>",          self._cv_drag)
        self._cv.bind("<ButtonRelease-1>",    self._cv_release)
        self._cv.bind("<Double-Button-1>",    self._cv_double)
        self._cv.bind("<Motion>",             self._cv_motion)
        self._cv.bind("<Configure>",          lambda _: self._redraw())
        self._cv.bind("<Control-MouseWheel>", self._cv_zoom_wheel)
        self._cv.bind("<Shift-MouseWheel>",   self._cv_scroll)
        self._cv.bind("<Alt-MouseWheel>",     self._cv_hscroll)
        self._cv.bind("<MouseWheel>",         self._cv_scroll)
        self._cv.bind("<ButtonPress-2>",      self._pan_start)
        self._cv.bind("<B2-Motion>",          self._pan_drag)
        self._cv.bind("<ButtonRelease-2>",    self._pan_end)
        self._cv.bind("<Shift-ButtonPress-1>",   self._pan_start)
        self._cv.bind("<Shift-B1-Motion>",       self._pan_drag)
        self._cv.bind("<Shift-ButtonRelease-1>", self._pan_end)

    # ── resize grip ───────────────────────────────────────────────────────────

    def _grip_press(self, event):
        self._grip_x0 = event.x_root
        self._grip_w0 = self._side.winfo_width()

    def _grip_drag(self, event):
        delta = event.x_root - self._grip_x0
        new_w = max(_SIDE_MIN, min(_SIDE_MAX, self._grip_w0 + delta))
        self._side.config(width=new_w)

    # ── file operations ───────────────────────────────────────────────────────

    def _cmd_save(self):
        if self._slicer_json_path:
            self._save(self._slicer_json_path)
        else:
            self._cmd_save_as()

    def _cmd_save_as(self):
        prefs = _load_prefs()
        initial = str(self._slicer_json_path.parent
                      if self._slicer_json_path
                      else self._default_slice_dir)
        path = filedialog.asksaveasfilename(
            title="Save slicer data",
            initialdir=initial,
            initialfile=(self._slicer_json_path.name
                         if self._slicer_json_path
                         else f"{self._src_path.stem}.slicer.json"),
            defaultextension=".slicer.json",
            filetypes=[("Slicer JSON", "*.slicer.json"), ("JSON", "*.json"),
                       ("All files", "*.*")],
            parent=self._win)
        if not path:
            return
        dest = Path(path)
        self._save(dest)
        prefs["last_slicer_json_dir"] = str(dest.parent)
        _save_prefs(prefs)

    def _save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = _tree_to_json(self._tree_data, self._group_visible,
                             self._group_hierarchy, self._src_path.name)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self._slicer_json_path = path
        self._dirty = False
        self._update_title()

    def _cmd_load(self):
        if self._dirty and not self._confirm_discard():
            return
        prefs = _load_prefs()
        initial = prefs.get("last_slicer_json_dir",
                            str(self._default_slice_dir))
        path = filedialog.askopenfilename(
            title="Load slicer data",
            initialdir=initial,
            filetypes=[("Slicer JSON", "*.slicer.json"), ("JSON", "*.json"),
                       ("All files", "*.*")],
            parent=self._win)
        if not path:
            return
        src = Path(path)
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
            self._tree_data, self._group_visible, self._group_hierarchy = _tree_from_json(data)
        except Exception as exc:
            messagebox.showerror("Load Failed",
                                 f"Could not read slicer file:\n{exc}",
                                 parent=self._win)
            return
        self._slicer_json_path = src
        self._dirty = False
        self._update_title()
        prefs["last_slicer_json_dir"] = str(src.parent)
        _save_prefs(prefs)
        self._refresh_tree()
        self._redraw()
        self._compute_missing_angles()

    def _confirm_discard(self) -> bool:
        """Ask Save / Discard / Cancel when dirty.  Returns True if OK to proceed."""
        ans = messagebox.askyesnocancel(
            "Unsaved Changes",
            "Save changes before continuing?",
            parent=self._win)
        if ans is None:      # Cancel
            return False
        if ans:              # Yes → save first
            self._cmd_save()
            if self._dirty:  # save was cancelled (e.g. Save As dialog cancelled)
                return False
        return True          # No → discard

    def _on_close(self):
        if not self._confirm_discard():
            return
        self._win.destroy()

    # ── export ────────────────────────────────────────────────────────────────

    def _cmd_export(self):
        if not self._tree_data:
            messagebox.showinfo("Export Pack",
                                "No parts defined yet.", parent=self._win)
            return

        prefs = _load_prefs()

        # pack name
        default_name = re.sub(r"[^a-z0-9_-]", "_",
                               self._src_path.stem.lower())
        dlg = simpledialog.askstring(
            "Export Pack",
            "Pack name [a-z0-9_-]:",
            initialvalue=prefs.get("last_pack_name", default_name),
            parent=self._win)
        if not dlg:
            return
        pack_name = dlg.strip()
        if not _PACK_NAME_RE.match(pack_name):
            messagebox.showerror("Invalid Name",
                                 "Pack name must match [a-z0-9_-]+.",
                                 parent=self._win)
            return

        # target folder
        chosen = filedialog.askdirectory(
            title="Choose export folder",
            initialdir=prefs.get("last_export_dir", str(Path.home())),
            parent=self._win,
            mustexist=True)
        if not chosen:
            return

        target_dir = Path(chosen)
        slice_dir  = Path(self._v_slice_dir.get().strip())

        result = slicer_export.export_pack(
            self._tree_data, self._group_hierarchy,
            self._src_path, slice_dir, pack_name, target_dir)

        prefs["last_pack_name"]  = pack_name
        prefs["last_export_dir"] = str(target_dir)
        _save_prefs(prefs)

        out_path = target_dir / pack_name
        if result["copied"] == 0:
            messagebox.showinfo(
                "Export Pack",
                f"No extracted parts found — nothing written to:\n{out_path}",
                parent=self._win)
        else:
            skipped_note = (f"\n{result['skipped']} part(s) skipped (not yet extracted)."
                            if result["skipped"] else "")
            messagebox.showinfo(
                "Export Pack",
                f"Pack '{pack_name}' exported.\n"
                f"{result['copied']} part(s) written to:\n{out_path}"
                f"{skipped_note}",
                parent=self._win)

    # ── tree management ───────────────────────────────────────────────────────

    def _refresh_tree(self):
        for iid in list(self._tv.get_children()):
            self._tv.delete(iid)
        self._iid_to_node.clear()
        self._node_to_iid.clear()
        new_thumbs: dict[str, ImageTk.PhotoImage] = {}

        slice_dir = Path(self._v_slice_dir.get().strip())

        for group, parts in self._tree_data.items():
            g_eye = _EYE_ON if self._group_visible.get(group, True) else _EYE_OFF
            g_iid = self._tv.insert("", tk.END, text=f" {group}",
                                    values=(g_eye,), tags=("group",), open=True)
            self._iid_to_node[g_iid] = (group, None)
            self._node_to_iid[(group, None)] = g_iid

            for part_name, nd in parts.items():
                img_path = slice_dir / group / f"{part_name}.png"
                thumb    = self._make_thumb(img_path) if img_path.exists() else None
                p_eye    = _EYE_ON if nd.get("visible", True) else _EYE_OFF
                w, h     = nd["w"], nd["h"]
                p_iid    = self._tv.insert(g_iid, tk.END,
                                           text=f" {part_name}  [{w}×{h}]",
                                           image=thumb or "",
                                           values=(p_eye,), tags=("part",))
                if thumb:
                    new_thumbs[p_iid] = thumb
                self._iid_to_node[p_iid] = (group, part_name)
                self._node_to_iid[(group, part_name)] = p_iid

        self._tree_thumbs = new_thumbs

    def _make_thumb(self, img_path: Path) -> ImageTk.PhotoImage | None:
        try:
            img = Image.open(img_path).convert("RGBA")
            img.thumbnail((_THUMB_SIZE, _THUMB_SIZE), Image.NEAREST)
            bg = Image.new("RGBA", img.size, (40, 40, 60, 255))
            bg.paste(img, mask=img.split()[3])
            return ImageTk.PhotoImage(bg)
        except Exception:
            return None

    def _on_tree_select(self, _event=None):
        sel = self._tv.selection()
        if not sel:
            return
        node = self._iid_to_node.get(sel[0])
        if node is None or node[1] is None:
            return
        nd = self._tree_data.get(node[0], {}).get(node[1])
        if nd:
            self._v_out_w.set(str(nd["w"]))
            self._v_out_h.set(str(nd["h"]))

    def _tv_click(self, event):
        if self._tv.identify_column(event.x) != "#1":
            return
        iid = self._tv.identify_row(event.y)
        if not iid:
            return
        node = self._iid_to_node.get(iid)
        if node is None:
            return
        group, part = node
        if part is None:
            self._toggle_group_vis(group)
        else:
            self._toggle_part_vis(group, part)
        return "break"

    def _toggle_group_vis(self, group: str):
        self._group_visible[group] = not self._group_visible.get(group, True)
        self._set_dirty()
        self._refresh_tree()
        self._redraw()

    def _toggle_part_vis(self, group: str, part: str):
        nd = self._tree_data.get(group, {}).get(part)
        if nd:
            nd["visible"] = not nd.get("visible", True)
            self._set_dirty()
            self._refresh_tree()
            self._redraw()

    def _tv_context_menu(self, event):
        iid = self._tv.identify_row(event.y)
        if iid:
            self._tv.selection_set(iid)
        menu = tk.Menu(self._win, tearoff=0, bg=BG_CARD, fg=FG,
                       activebackground=BG_SEL, activeforeground=ACCENT,
                       relief=tk.FLAT, bd=1)
        menu.add_command(label="Add Group",  command=self._add_group)
        menu.add_command(label="Add Part",   command=self._add_part)
        menu.add_separator()
        menu.add_command(label="Rename",     command=self._rename_node)
        menu.add_command(label="Delete",     command=self._delete_node)
        menu.tk_popup(event.x_root, event.y_root)

    def _tv_rename_inline(self, event):
        iid = self._tv.identify_row(event.y)
        if iid:
            self._tv.selection_set(iid)
            self._rename_node()

    # ── node reordering ───────────────────────────────────────────────────────

    def _move_node_up(self):
        self._shift_node(-1)

    def _move_node_down(self):
        self._shift_node(+1)

    def _shift_node(self, direction: int):
        """Move selected group or part up (−1) or down (+1) in order."""
        sel = self._tv.selection()
        if not sel:
            return
        node = self._iid_to_node.get(sel[0])
        if node is None:
            return
        group, part = node

        if part is None:
            # ── move group ────────────────────────────────────────────────────
            keys = list(self._tree_data.keys())
            idx  = keys.index(group)
            new_idx = idx + direction
            if new_idx < 0 or new_idx >= len(keys):
                return
            keys[idx], keys[new_idx] = keys[new_idx], keys[idx]
            self._tree_data = {k: self._tree_data[k] for k in keys}
        else:
            # ── move part within its group ────────────────────────────────────
            parts   = self._tree_data[group]
            keys    = list(parts.keys())
            idx     = keys.index(part)
            new_idx = idx + direction
            if new_idx < 0 or new_idx >= len(keys):
                return
            keys[idx], keys[new_idx] = keys[new_idx], keys[idx]
            self._tree_data[group] = {k: parts[k] for k in keys}

        self._set_dirty()
        self._refresh_tree()
        iid = self._node_to_iid.get(node)
        if iid:
            self._tv.selection_set(iid)
            self._tv.see(iid)

    def _cmd_load_template(self):
        prefs   = _load_prefs()
        initial = prefs.get("last_template_dir", str(Path.home()))
        path_str = filedialog.askopenfilename(
            title="Load Template",
            initialdir=initial,
            filetypes=[("JSON templates", "*.json"), ("All files", "*.*")],
            parent=self._win,
        )
        if not path_str:
            return
        path = Path(path_str)
        prefs["last_template_dir"] = str(path.parent)
        _save_prefs(prefs)

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("Load Template",
                                 f"Could not read template:\n{exc}",
                                 parent=self._win)
            return

        groups = data.get("groups", {})
        if not isinstance(groups, dict):
            messagebox.showerror("Load Template",
                                 "Invalid template: 'groups' must be an object.",
                                 parent=self._win)
            return

        added = 0
        for group_name, group_val in groups.items():
            # v1: group_val is a flat list of part name strings
            # v2: group_val is {"parts": [...], "hierarchy": {...}}
            if isinstance(group_val, list):
                part_names = group_val
                hierarchy:  dict[str, str] = {}
            elif isinstance(group_val, dict):
                part_names = group_val.get("parts", [])
                hierarchy  = group_val.get("hierarchy", {})
            else:
                continue

            if group_name not in self._tree_data:
                self._tree_data[group_name] = {}
                self._group_visible[group_name] = True

            if hierarchy:
                existing = self._group_hierarchy.get(group_name, {})
                existing.update(hierarchy)
                self._group_hierarchy[group_name] = existing

            for part_name in part_names:
                if not isinstance(part_name, str) or not part_name:
                    continue
                if part_name not in self._tree_data[group_name]:
                    self._tree_data[group_name][part_name] = _new_node()
                    added += 1

        if added:
            self._set_dirty()
            self._refresh_tree()

    def _add_group(self):
        name = simpledialog.askstring(
            "Add Group", "Group name (a-z 0-9 _ -):", parent=self._win)
        if not name:
            return
        name = name.strip()
        if not _PACK_NAME_RE.match(name):
            messagebox.showerror("Invalid Name", "Use only: a-z 0-9 _ -",
                                 parent=self._win)
            return
        if name in self._tree_data:
            messagebox.showinfo("Exists", f"Group '{name}' already exists.",
                                parent=self._win)
            return
        self._tree_data[name] = {}
        self._group_visible[name] = True
        self._set_dirty()
        self._refresh_tree()
        g_iid = self._node_to_iid.get((name, None))
        if g_iid:
            self._tv.selection_set(g_iid)
            self._tv.see(g_iid)

    def _add_part(self):
        group = self._selected_group()
        if group is None:
            messagebox.showwarning("Select a Group",
                                   "Select or create a group first.",
                                   parent=self._win)
            return
        name = simpledialog.askstring(
            "Add Part", f"Part name in '{group}' (a-z 0-9 _ -):",
            parent=self._win)
        if not name:
            return
        name = name.strip()
        if not _PACK_NAME_RE.match(name):
            messagebox.showerror("Invalid Name", "Use only: a-z 0-9 _ -",
                                 parent=self._win)
            return
        if name in self._tree_data.get(group, {}):
            messagebox.showinfo("Exists",
                                f"'{name}' already exists in '{group}'.",
                                parent=self._win)
            return
        try:
            w = int(self._v_out_w.get());  h = int(self._v_out_h.get())
            if w < 1 or h < 1:
                raise ValueError
        except ValueError:
            w, h = 128, 128
        self._tree_data.setdefault(group, {})[name] = _new_node(w, h)
        self._set_dirty()
        self._refresh_tree()
        p_iid = self._node_to_iid.get((group, name))
        if p_iid:
            self._tv.selection_set(p_iid)
            self._tv.see(p_iid)

    def _rename_node(self):
        sel = self._tv.selection()
        if not sel:
            return
        node = self._iid_to_node.get(sel[0])
        if node is None:
            return
        group, part = node
        old = part if part is not None else group
        new = simpledialog.askstring("Rename", f"New name for '{old}':",
                                     initialvalue=old, parent=self._win)
        if not new or new == old:
            return
        new = new.strip()
        if not _PACK_NAME_RE.match(new):
            messagebox.showerror("Invalid Name", "Use only: a-z 0-9 _ -",
                                 parent=self._win)
            return
        if part is None:
            if new in self._tree_data:
                messagebox.showinfo("Exists", f"Group '{new}' already exists.",
                                    parent=self._win)
                return
            self._tree_data[new] = self._tree_data.pop(group)
            self._group_visible[new] = self._group_visible.pop(group, True)
            key = (new, None)
        else:
            if new in self._tree_data.get(group, {}):
                messagebox.showinfo("Exists", f"Part '{new}' already exists.",
                                    parent=self._win)
                return
            self._tree_data[group][new] = self._tree_data[group].pop(part)
            key = (group, new)
        self._set_dirty()
        self._refresh_tree()
        iid = self._node_to_iid.get(key)
        if iid:
            self._tv.selection_set(iid)
            self._tv.see(iid)

    def _delete_node(self):
        sel = self._tv.selection()
        if not sel:
            return
        node = self._iid_to_node.get(sel[0])
        if node is None:
            return
        group, part = node
        if part is None:
            if not messagebox.askyesno("Delete Group",
                                       f"Delete group '{group}' and all parts?",
                                       parent=self._win):
                return
            self._tree_data.pop(group, None)
            self._group_visible.pop(group, None)
        else:
            self._tree_data.get(group, {}).pop(part, None)
        self._set_dirty()
        self._refresh_tree()
        self._redraw()

    def _selected_group(self) -> str | None:
        sel = self._tv.selection()
        if not sel:
            return None
        node = self._iid_to_node.get(sel[0])
        return node[0] if node else None

    # ── hover tooltip ─────────────────────────────────────────────────────────

    def _tv_motion(self, event):
        iid = self._tv.identify_row(event.y)
        if iid == self._tooltip_last_iid:
            return
        self._hide_tooltip()
        self._tooltip_last_iid = iid
        if not iid:
            return
        node = self._iid_to_node.get(iid)
        if node is None or node[1] is None:
            return  # group node — no preview
        group, part = node
        img_path = Path(self._v_slice_dir.get().strip()) / group / f"{part}.png"
        if not img_path.exists():
            return
        x_root, y_root = event.x_root, event.y_root
        self._tooltip_after_id = self._win.after(
            400, lambda: self._show_tooltip(x_root, y_root, img_path))

    def _show_tooltip(self, x_root: int, y_root: int, img_path: Path):
        self._tooltip_win = None  # cleared before rebuild
        try:
            img = Image.open(img_path).convert("RGBA")
        except Exception:
            return

        # pick largest integer zoom that fits within 256 × 256
        iw, ih = img.size
        max_dim = 256
        scale = max(1, min(max_dim // max(iw, 1), max_dim // max(ih, 1)))
        sw = max(1, iw * scale);  sh = max(1, ih * scale)

        # checkerboard background to show transparency
        bg = Image.new("RGBA", (sw, sh))
        cell = 8
        dark, light = (40, 40, 60, 255), (55, 55, 78, 255)
        draw = ImageDraw.Draw(bg)
        for ty in range(0, sh, cell):
            for tx in range(0, sw, cell):
                col = light if (tx // cell + ty // cell) % 2 == 0 else dark
                draw.rectangle((tx, ty, tx + cell - 1, ty + cell - 1), fill=col)
        bg.paste(img.resize((sw, sh), Image.NEAREST), mask=img.resize((sw, sh), Image.NEAREST).split()[3])

        photo = ImageTk.PhotoImage(bg)

        win = tk.Toplevel(self._win)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BG_CARD)

        tk.Label(win, image=photo, bg=BG_CARD, bd=0).pack(padx=4, pady=(4, 2))
        tk.Label(win, text=f"{img_path.parent.name}/{img_path.name}  {iw}×{ih}",
                 bg=BG_CARD, fg=FG_DIM, font=("Consolas", 7)).pack(padx=4, pady=(0, 4))

        win.update_idletasks()
        # position: right of cursor, nudge up so it doesn't clip screen bottom
        wx = x_root + 18
        wy = y_root - win.winfo_height() // 2
        win.geometry(f"+{wx}+{wy}")

        self._tooltip_win   = win
        self._tooltip_photo = photo   # keep ref to prevent GC

    def _hide_tooltip(self):
        if self._tooltip_after_id is not None:
            self._win.after_cancel(self._tooltip_after_id)
            self._tooltip_after_id = None
        if self._tooltip_win is not None:
            try:
                self._tooltip_win.destroy()
            except Exception:
                pass
            self._tooltip_win = None
        self._tooltip_last_iid = ""

    # ── frame ─────────────────────────────────────────────────────────────────

    def _load_frame(self):
        self._src_img = Image.open(self._src_path).convert("RGBA")
        self._lbl_source.config(text=self._src_path.name)
        self._initial_fit_done = False
        self._clear_selection()

    # ── tool ──────────────────────────────────────────────────────────────────

    def _set_tool(self, tool: str):
        self._clear_selection()
        self._tool = tool
        if tool == "lasso":
            self._cv.config(cursor="crosshair")
            self._btn_lasso.config(fg=YELLOW, font=("", 9, "bold"),
                                   relief=tk.GROOVE, bd=2,
                                   highlightbackground=YELLOW,
                                   highlightthickness=2)
            self._btn_box.config(fg=FG_DIM, font=("", 9),
                                 relief=tk.FLAT, bd=0, highlightthickness=0)
        else:
            self._cv.config(cursor="arrow")
            self._btn_box.config(fg=ACCENT, font=("", 9, "bold"),
                                 relief=tk.GROOVE, bd=2,
                                 highlightbackground=ACCENT,
                                 highlightthickness=2)
            self._btn_lasso.config(fg=FG_DIM, font=("", 9),
                                   relief=tk.FLAT, bd=0, highlightthickness=0)
        self._redraw()

    def _clear_selection(self):
        self._lasso_pts.clear()
        self._lasso_closed = False
        self._lasso_mouse  = None
        self._box_start    = None
        self._box_end      = None
        self._box_dragging = False
        self._update_preview(None)
        self._redraw()

    # ── coordinates ───────────────────────────────────────────────────────────

    def _zoom(self) -> int:
        return _ZOOM_LEVELS[self._zoom_idx]

    def _img_to_screen(self, ix: float, iy: float) -> tuple[float, float]:
        z = self._zoom()
        return ix * z + self._pan_x, iy * z + self._pan_y

    def _screen_to_img(self, sx: float, sy: float) -> tuple[float, float]:
        z = self._zoom()
        return (sx - self._pan_x) / z, (sy - self._pan_y) / z

    # ── fit / scroll ──────────────────────────────────────────────────────────

    def _fit_image_to_canvas(self):
        if not hasattr(self, "_cv"):
            return
        cw = self._cv.winfo_width();  ch = self._cv.winfo_height()
        if cw <= 1 or ch <= 1:
            return
        iw, ih = self._src_img.size
        best_idx = 0
        for i, z in enumerate(_ZOOM_LEVELS):
            if iw * z <= cw and ih * z <= ch:
                best_idx = i
            else:
                break
        self._zoom_idx = best_idx
        z = _ZOOM_LEVELS[best_idx]
        self._lbl_zoom.config(text=f"{z}×")
        self._pan_x = (cw - iw * z) / 2.0
        self._pan_y = (ch - ih * z) / 2.0
        self._initial_fit_done = True

    def _update_scrollbars(self):
        z       = self._zoom()
        iw, ih  = self._src_img.size
        cw      = self._cv.winfo_width()  or 1
        ch      = self._cv.winfo_height() or 1
        total_w = max(iw * z, 1);  total_h = max(ih * z, 1)
        self._hbar.set(-self._pan_x / total_w, (cw - self._pan_x) / total_w)
        self._vbar.set(-self._pan_y / total_h, (ch - self._pan_y) / total_h)

    def _hscroll(self, *args):
        total_w = self._src_img.size[0] * self._zoom()
        if args[0] == "moveto":
            self._pan_x = -float(args[1]) * total_w
        elif args[0] == "scroll":
            self._pan_x -= int(args[1]) * self._zoom() * 8
        self._redraw()

    def _vscroll(self, *args):
        total_h = self._src_img.size[1] * self._zoom()
        if args[0] == "moveto":
            self._pan_y = -float(args[1]) * total_h
        elif args[0] == "scroll":
            self._pan_y -= int(args[1]) * self._zoom() * 8
        self._redraw()

    # ── zoom / pan ────────────────────────────────────────────────────────────

    def _cv_zoom_wheel(self, event):
        ix, iy = self._screen_to_img(event.x, event.y)
        if event.delta > 0:
            self._zoom_idx = min(len(_ZOOM_LEVELS) - 1, self._zoom_idx + 1)
        else:
            self._zoom_idx = max(0, self._zoom_idx - 1)
        z = self._zoom()
        self._pan_x = event.x - ix * z
        self._pan_y = event.y - iy * z
        self._lbl_zoom.config(text=f"{z}×")
        self._redraw()

    def _cv_scroll(self, event):
        self._pan_y -= event.delta * 0.5
        self._redraw()

    def _cv_hscroll(self, event):
        self._pan_x -= event.delta * 0.5
        self._redraw()

    def _pan_start(self, event):
        self._panning     = True
        self._pan_start_x = event.x;  self._pan_start_y = event.y
        self._pan_orig_x  = self._pan_x;  self._pan_orig_y = self._pan_y

    def _pan_drag(self, event):
        if not self._panning:
            return
        self._pan_x = self._pan_orig_x + (event.x - self._pan_start_x)
        self._pan_y = self._pan_orig_y + (event.y - self._pan_start_y)
        self._redraw()

    def _pan_end(self, _event):
        self._panning = False

    # ── canvas events ─────────────────────────────────────────────────────────

    def _cv_press(self, event):
        if self._panning:
            return
        # Check rotation handles before normal tool dispatch
        for h in self._rot_handles:
            tx, ty = h["tip_screen"]
            if math.hypot(event.x - tx, event.y - ty) <= _ANGLE_HANDLE_R + 4:
                self._rot_dragging    = True
                self._rot_drag_node   = (h["group"], h["part"])
                self._rot_drag_ctr_img = h["center_img"]
                return
        ix, iy = self._screen_to_img(event.x, event.y)
        if self._tool == "box":
            self._box_start = (ix, iy);  self._box_end = (ix, iy)
            self._box_dragging = True
        elif self._tool == "lasso" and not self._lasso_closed:
            if self._lasso_pts:
                fx, fy = self._img_to_screen(*self._lasso_pts[0])
                if math.hypot(event.x - fx, event.y - fy) < 8:
                    self._lasso_closed = True
                    self._lasso_mouse  = None
                    self._update_preview(self._build_preview_image())
                    self._redraw()
                    return
            self._lasso_pts.append((ix, iy))
        self._redraw()

    def _cv_drag(self, event):
        if self._panning:
            return
        if self._rot_dragging and self._rot_drag_node:
            group, part = self._rot_drag_node
            nd = self._tree_data.get(group, {}).get(part)
            if nd is not None:
                cx_s, cy_s = self._img_to_screen(*self._rot_drag_ctr_img)
                dx = event.x - cx_s
                dy = event.y - cy_s
                if abs(dx) > 1 or abs(dy) > 1:
                    # Screen dy is down; world dy is up — negate
                    nd["angle"] = math.degrees(math.atan2(-dy, dx))
                self._redraw()
            return
        if self._tool == "box" and self._box_dragging:
            self._box_end = self._screen_to_img(event.x, event.y)
            self._update_preview(self._build_preview_image())
            self._redraw()

    def _cv_release(self, _event):
        if self._rot_dragging:
            self._rot_dragging  = False
            if self._rot_drag_node:
                self._set_dirty()
            self._rot_drag_node = None
            return
        if self._tool == "box":
            self._box_dragging = False

    def _cv_double(self, _event):
        if self._tool == "lasso" and not self._lasso_closed and len(self._lasso_pts) >= 2:
            self._lasso_closed = True
            self._lasso_mouse  = None
            self._update_preview(self._build_preview_image())
            self._redraw()

    def _cv_motion(self, event):
        if self._tool == "lasso" and not self._lasso_closed and self._lasso_pts:
            self._lasso_mouse = self._screen_to_img(event.x, event.y)
            self._redraw()

    # ── drawing ───────────────────────────────────────────────────────────────

    def _redraw(self):
        if not hasattr(self, "_cv"):
            return
        cv = self._cv
        if not cv.winfo_exists():
            return
        if not self._initial_fit_done:
            self._fit_image_to_canvas()

        cv.delete("all")
        z      = self._zoom()
        iw, ih = self._src_img.size
        cw     = cv.winfo_width()  or 800
        ch     = cv.winfo_height() or 600

        # 1. pixel grid
        if z >= _GRID_THRESHOLD:
            x0 = int(self._pan_x) % z;  y0 = int(self._pan_y) % z
            for gx in range(x0, cw + z, z):
                px = round((gx - self._pan_x) / z)
                c  = _GRID_MAJOR if z >= _MAJOR_THRESHOLD and px % 8 == 0 else _GRID_MINOR
                cv.create_line(gx, 0, gx, ch, fill=c, width=1)
            for gy in range(y0, ch + z, z):
                py = round((gy - self._pan_y) / z)
                c  = _GRID_MAJOR if z >= _MAJOR_THRESHOLD and py % 8 == 0 else _GRID_MINOR
                cv.create_line(0, gy, cw, gy, fill=c, width=1)

        # 2. source image
        scaled = self._src_img.resize((max(1, iw * z), max(1, ih * z)),
                                      Image.NEAREST)
        self._tk_photo = ImageTk.PhotoImage(scaled)
        cv.create_image(int(self._pan_x), int(self._pan_y),
                        image=self._tk_photo, anchor=tk.NW)

        # 3. ghost overlays
        self._draw_ghosts(cv)

        # 4. active selection
        if self._tool == "lasso":
            self._draw_lasso(cv)
        else:
            self._draw_box(cv)

        # 5. mode badge
        self._draw_badge(cv)

        # 6. scrollbars
        self._update_scrollbars()

    def _draw_ghosts(self, cv: tk.Canvas):
        self._rot_handles = []
        frame = self._src_path.name
        for group, parts in self._tree_data.items():
            if not self._group_visible.get(group, True):
                continue
            for part_name, nd in parts.items():
                if not nd.get("visible", True):
                    continue
                ghost = nd.get("ghost")
                if ghost is None or ghost.get("frame") != frame:
                    continue
                pts = ghost["pts"]
                if ghost["type"] == "lasso" and len(pts) >= 3:
                    pts_s = [self._img_to_screen(x, y) for x, y in pts]
                    cv.create_polygon([c for p in pts_s for c in p],
                                      outline=_GHOST_COLOR, fill="",
                                      width=_GHOST_WIDTH, dash=_GHOST_DASH)
                elif ghost["type"] == "box" and len(pts) == 2:
                    x0s, y0s = self._img_to_screen(*pts[0])
                    x1s, y1s = self._img_to_screen(*pts[1])
                    cv.create_rectangle(min(x0s, x1s), min(y0s, y1s),
                                        max(x0s, x1s), max(y0s, y1s),
                                        outline=_GHOST_COLOR, fill="",
                                        width=1, dash=_GHOST_DASH)

                # ── rotation arrow ────────────────────────────────────────
                angle = nd.get("angle")
                if angle is not None:
                    # Bounding-box centre in image space
                    if ghost["type"] == "box":
                        cx_i = (pts[0][0] + pts[1][0]) / 2
                        cy_i = (pts[0][1] + pts[1][1]) / 2
                        half  = math.hypot(abs(pts[1][0] - pts[0][0]),
                                           abs(pts[1][1] - pts[0][1])) / 2
                    else:
                        xs_ = [p[0] for p in pts];  ys_ = [p[1] for p in pts]
                        cx_i = (min(xs_) + max(xs_)) / 2
                        cy_i = (min(ys_) + max(ys_)) / 2
                        half  = math.hypot(max(xs_) - min(xs_),
                                           max(ys_) - min(ys_)) / 2

                    # Arrow length proportional to bbox diagonal (min 12 img-px)
                    L = max(12.0, half * 0.75)
                    a_rad = math.radians(angle)
                    # Image-space direction: world Y-up → image Y-down, so negate sin
                    tip_i  = (cx_i + L * math.cos(a_rad),
                              cy_i - L * math.sin(a_rad))
                    tail_i = (cx_i - L * 0.25 * math.cos(a_rad),
                              cy_i + L * 0.25 * math.sin(a_rad))

                    cx_s,  cy_s  = self._img_to_screen(cx_i,    cy_i)
                    tx_s,  ty_s  = self._img_to_screen(*tip_i)
                    tlx_s, tly_s = self._img_to_screen(*tail_i)

                    is_active = self._rot_drag_node == (group, part_name)
                    col   = "#ffffff" if is_active else _ANGLE_COLOR
                    r     = _ANGLE_HANDLE_R + (2 if is_active else 0)

                    cv.create_line(tlx_s, tly_s, tx_s, ty_s,
                                   fill=col, width=2)
                    # Arrowhead: small filled triangle at tip
                    head = 9
                    a_s  = math.atan2(ty_s - cy_s, tx_s - cx_s)  # screen angle
                    hx1 = tx_s + head * math.cos(a_s + math.radians(145))
                    hy1 = ty_s + head * math.sin(a_s + math.radians(145))
                    hx2 = tx_s + head * math.cos(a_s - math.radians(145))
                    hy2 = ty_s + head * math.sin(a_s - math.radians(145))
                    cv.create_polygon(tx_s, ty_s, hx1, hy1, hx2, hy2,
                                      fill=col, outline="")
                    # Drag handle: ring at tip
                    cv.create_oval(tx_s - r, ty_s - r, tx_s + r, ty_s + r,
                                   outline=col, fill="", width=2)

                    self._rot_handles.append({
                        "group": group, "part": part_name,
                        "center_img": (cx_i, cy_i),
                        "tip_screen": (tx_s, ty_s),
                    })

    def _draw_lasso(self, cv: tk.Canvas):
        if not self._lasso_pts:
            return
        pts_s = [self._img_to_screen(ix, iy) for ix, iy in self._lasso_pts]
        if self._lasso_closed and len(pts_s) >= 3:
            cv.create_polygon([c for p in pts_s for c in p],
                              fill=YELLOW, stipple="gray25", outline="", width=0)
        if len(pts_s) >= 2:
            for i in range(len(pts_s) - 1):
                cv.create_line(*pts_s[i], *pts_s[i + 1],
                               fill=_LASSO_STROKE, width=1, dash=_DASH)
            if self._lasso_closed:
                cv.create_line(*pts_s[-1], *pts_s[0],
                               fill=_LASSO_STROKE, width=1, dash=_DASH)
        if not self._lasso_closed and self._lasso_mouse:
            mx, my = self._img_to_screen(*self._lasso_mouse)
            cv.create_line(*pts_s[-1], mx, my,
                           fill=_LASSO_STROKE, width=1, dash=(2, 4))
        for i, (sx, sy) in enumerate(pts_s):
            r = _HANDLE_R
            cv.create_oval(sx - r, sy - r, sx + r, sy + r,
                           outline=(YELLOW if i == 0 else _LASSO_STROKE),
                           fill=_BADGE_FG, width=1)

    def _draw_box(self, cv: tk.Canvas):
        if self._box_start is None or self._box_end is None:
            return
        x0s, y0s = self._img_to_screen(*self._box_start)
        x1s, y1s = self._img_to_screen(*self._box_end)
        x0, x1 = min(x0s, x1s), max(x0s, x1s)
        y0, y1 = min(y0s, y1s), max(y0s, y1s)
        cv.create_rectangle(x0, y0, x1, y1, fill=ACCENT, stipple="gray25",
                            outline=_BOX_STROKE, width=1, dash=_DASH)
        s = _HANDLE_SQ
        for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            cv.create_rectangle(cx - s, cy - s, cx + s, cy + s,
                                fill=_BADGE_FG, outline=_BOX_STROKE, width=1)

    def _draw_badge(self, cv: tk.Canvas):
        if self._tool == "lasso":
            text, bg = "✦ LASSO  [L]", _BADGE_BG_LASSO
        else:
            text, bg = "☐ BOX  [B]",   _BADGE_BG_BOX
        font = ("", 11, "bold")
        tmp  = cv.create_text(0, 0, text=text, font=font, anchor=tk.NW)
        bb   = cv.bbox(tmp);  cv.delete(tmp)
        if not bb:
            return
        tw, th   = bb[2] - bb[0], bb[3] - bb[1]
        px, py   = 10, 5
        bx0, by0 = 10, 10
        cv.create_rectangle(bx0, by0, bx0 + tw + px * 2, by0 + th + py * 2,
                            fill=bg, outline="", width=0)
        cv.create_text(bx0 + px, by0 + py, text=text,
                       fill=_BADGE_FG, font=font, anchor=tk.NW)

    # ── preview ───────────────────────────────────────────────────────────────

    def _build_preview_image(self) -> Image.Image | None:
        try:
            out_w = int(self._v_out_w.get());  out_h = int(self._v_out_h.get())
        except ValueError:
            return None
        crop = self._compute_crop()
        return crop

    def _update_preview(self, img: Image.Image | None):
        pv = self._pv_canvas
        pv.delete("all")
        if img is None:
            return
        cw = pv.winfo_width()  or (self._SIDE_W - 20)
        ch = pv.winfo_height() or (self._SIDE_W - 20)
        scale = min(cw / max(img.width, 1), ch / max(img.height, 1), 4.0)
        sw = max(1, round(img.width * scale));  sh = max(1, round(img.height * scale))
        self._pv_photo = ImageTk.PhotoImage(img.resize((sw, sh), Image.NEAREST))
        pv.create_image(cw // 2, ch // 2, image=self._pv_photo, anchor=tk.CENTER)

    # ── extraction ────────────────────────────────────────────────────────────

    def _compute_crop(self) -> Image.Image | None:
        iw, ih = self._src_img.size
        if self._tool == "lasso":
            if not self._lasso_closed or len(self._lasso_pts) < 3:
                return None
            pts = self._lasso_pts
            x0, y0, x1, y1 = _poly_bbox(pts)
            x0 = max(0, x0);  y0 = max(0, y0)
            x1 = min(iw, x1); y1 = min(ih, y1)
            if x1 <= x0 or y1 <= y0:
                return None
            mask = Image.new("L", self._src_img.size, 0)
            ImageDraw.Draw(mask).polygon([c for p in pts for c in p], fill=255)
            result = self._src_img.copy()
            result.putalpha(mask)
            return result.crop((x0, y0, x1, y1))
        else:
            if self._box_start is None or self._box_end is None:
                return None
            bx0 = min(self._box_start[0], self._box_end[0])
            by0 = min(self._box_start[1], self._box_end[1])
            bx1 = max(self._box_start[0], self._box_end[0])
            by1 = max(self._box_start[1], self._box_end[1])
            bx0 = max(0, int(bx0));        by0 = max(0, int(by0))
            bx1 = min(iw, math.ceil(bx1)); by1 = min(ih, math.ceil(by1))
            if bx1 <= bx0 or by1 <= by0:
                return None
            return self._src_img.crop((bx0, by0, bx1, by1))

    @staticmethod
    def _fit_to_canvas(crop: Image.Image, out_w: int, out_h: int) -> Image.Image:
        cw, ch = crop.size
        if cw > out_w or ch > out_h:
            scale = min(out_w / cw, out_h / ch)
            cw = max(1, round(cw * scale));  ch = max(1, round(ch * scale))
            crop = crop.resize((cw, ch), Image.LANCZOS)
        canvas = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 0))
        canvas.paste(crop, ((out_w - cw) // 2, (out_h - ch) // 2))
        return canvas

    def _do_extract(self):
        try:
            out_w = int(self._v_out_w.get());  out_h = int(self._v_out_h.get())
            if out_w < 1 or out_h < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid Size", "W and H must be positive integers.",
                                 parent=self._win)
            return

        crop = self._compute_crop()
        if crop is None:
            messagebox.showwarning("No Selection", "Draw a selection first.",
                                   parent=self._win)
            return

        sel = self._tv.selection()
        if not sel:
            messagebox.showwarning("No Part Selected",
                                   "Select a part node in the tree first.",
                                   parent=self._win)
            return
        node_ref = self._iid_to_node.get(sel[0])
        if node_ref is None or node_ref[1] is None:
            messagebox.showwarning("Select a Part",
                                   "Select a leaf part node, not a group.",
                                   parent=self._win)
            return
        group, name = node_ref
        iid = sel[0]

        slice_dir = Path(self._v_slice_dir.get().strip())
        out_path  = slice_dir / group / f"{name}.png"

        if out_path.exists():
            choice = messagebox.askyesnocancel(
                "File Exists",
                f"'{out_path.name}' already exists in {group}/.\n\nOverwrite?",
                parent=self._win)
            if choice is None:
                return
            if not choice:
                out_path = slice_dir / group / f"{name}_{uuid.uuid4().hex[:6]}.png"

        out_path.parent.mkdir(parents=True, exist_ok=True)
        crop.save(out_path)

        # update node — use actual cropped dimensions, not the W/H UI fields
        nd = self._tree_data[group][name]
        nd["w"] = crop.width;  nd["h"] = crop.height;  nd["visible"] = True
        if self._tool == "lasso" and self._lasso_pts:
            nd["ghost"] = {"type": "lasso", "frame": self._src_path.name,
                           "pts": [list(p) for p in self._lasso_pts]}
        elif self._tool == "box" and self._box_start and self._box_end:
            nd["ghost"] = {"type": "box", "frame": self._src_path.name,
                           "pts": [list(self._box_start), list(self._box_end)]}
        self._group_visible.setdefault(group, True)

        # Estimate bone orientation via PCA (requires numpy)
        if _HAS_NUMPY:
            iw, ih = self._src_img.size
            origin = _ghost_origin(nd["ghost"])
            if origin is not None:
                part_world = (
                    origin[0] + nd["w"] / 2 - iw / 2,
                    -(origin[1] + nd["h"] / 2 - ih / 2),
                )
                parent_name = self._group_hierarchy.get(group, {}).get(name, "root")
                parent_world: tuple[float, float] = (0.0, 0.0)
                p_nd = self._tree_data.get(group, {}).get(parent_name)
                if p_nd:
                    p_orig = _ghost_origin(p_nd.get("ghost"))
                    if p_orig is not None:
                        parent_world = (
                            p_orig[0] + p_nd["w"] / 2 - iw / 2,
                            -(p_orig[1] + p_nd["h"] / 2 - ih / 2),
                        )
                nd["angle"] = _estimate_angle(out_path, part_world, parent_world)

        self._set_dirty()

        # update thumbnail
        thumb = self._make_thumb(out_path)
        if thumb:
            self._tree_thumbs[iid] = thumb
            self._tv.item(iid, image=thumb)

        self._extracted_lb.insert(0, f"{group}/{out_path.name}")
        self._extracted_lb.see(0)
        self._advance_part_selection(group, iid)
        self._clear_selection()

    def _compute_missing_angles(self) -> None:
        """Backfill angle estimates for parts that have a ghost but no stored angle.

        Runs automatically after load so old slicer files gain rotation arrows
        without requiring re-extraction.
        """
        if not _HAS_NUMPY:
            return
        iw, ih = self._src_img.size
        slice_dir = Path(self._v_slice_dir.get().strip())
        computed = 0

        for group, parts in self._tree_data.items():
            for part_name, nd in parts.items():
                if nd.get("angle") is not None:
                    continue
                if nd.get("ghost") is None:
                    continue
                png_path = slice_dir / group / f"{part_name}.png"
                if not png_path.exists():
                    continue
                origin = _ghost_origin(nd["ghost"])
                if origin is None:
                    continue

                part_world: tuple[float, float] = (
                    origin[0] + nd["w"] / 2 - iw / 2,
                    -(origin[1] + nd["h"] / 2 - ih / 2),
                )
                parent_name = self._group_hierarchy.get(group, {}).get(part_name, "root")
                parent_world: tuple[float, float] = (0.0, 0.0)
                p_nd = self._tree_data.get(group, {}).get(parent_name)
                if p_nd:
                    p_orig = _ghost_origin(p_nd.get("ghost"))
                    if p_orig is not None:
                        parent_world = (
                            p_orig[0] + p_nd["w"] / 2 - iw / 2,
                            -(p_orig[1] + p_nd["h"] / 2 - ih / 2),
                        )

                angle = _estimate_angle(png_path, part_world, parent_world)
                if angle is not None:
                    nd["angle"] = angle
                    computed += 1

        if computed:
            self._set_dirty()
            self._refresh_tree()
            self._redraw()

    def _advance_part_selection(self, group: str, current_iid: str):
        slice_dir = Path(self._v_slice_dir.get().strip())
        g_iid = self._node_to_iid.get((group, None))
        if g_iid is None:
            return
        found = False
        for child_iid in self._tv.get_children(g_iid):
            if child_iid == current_iid:
                found = True;  continue
            if not found:
                continue
            node = self._iid_to_node.get(child_iid)
            if node and node[1]:
                if not (slice_dir / group / f"{node[1]}.png").exists():
                    self._tv.selection_set(child_iid)
                    self._tv.see(child_iid)
                    self._on_tree_select()
                    return

    # ── output dir ────────────────────────────────────────────────────────────

    def _browse_slice_dir(self):
        chosen = filedialog.askdirectory(
            title="Choose slice output folder",
            initialdir=self._v_slice_dir.get(),
            parent=self._win, mustexist=False)
        if chosen:
            self._v_slice_dir.set(chosen)
            self._refresh_tree()
