"""
DirectorPanel -- tree-based animation timing, branching, and interpolation editor.

Embedded in the ComposeWindow as a tab alongside the Compose timeline.
Operates on a saved animation folder (reads/writes frames.json).
"""

import json
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk

from constants import (
    BG, BG_PANEL, BG_CARD, BG_SEL,
    FG, FG_DIM, ACCENT, RED, GREEN, YELLOW,
    MAX_SCALE,
)
from image_helpers import _make_thumb, _thumb_scale
from dialogs import _InputDialog
import pack_export


# -- constants ----------------------------------------------------------------

CARD_W  = 72       # card width at zoom 1
CARD_H  = 96       # card height at zoom 1
GAP_X   = 40       # horizontal gap between cards
GAP_Y   = 50       # vertical gap between rows
COL_W   = CARD_W + GAP_X
ROW_H   = CARD_H + GAP_Y
THUMB_H = 72       # thumbnail target height at zoom 1
KF_BORDER = 3
ARROW_W  = 2
RULER_H  = 36      # height of embedded ruler at bottom of canvas (px)

COL_KF      = GREEN
COL_ARROW   = ACCENT
COL_BRANCH  = YELLOW
COL_ANCHOR  = YELLOW


def _uid() -> str:
    return uuid.uuid4().hex[:8]


# -- data model ---------------------------------------------------------------

@dataclass
class DFrame:
    """One frame node in the director tree."""
    uid: str
    png: Path
    keyframe: bool = False
    hold: int = 1
    abs_tick: int = 0               # absolute tick position (X layout)
    row: int = 0                    # branch row (Y layout)
    _photo: object = field(default=None, repr=False)


@dataclass
class DBranch:
    """A branch (or the stem) of the animation tree."""
    name: str
    frames: list[DFrame] = field(default_factory=list)
    branches: dict[str, "DBranch"] = field(default_factory=dict)
    parent: "DBranch | None" = field(default=None, repr=False)
    spawn_uid: str | None = None   # UID of the keyframe that spawned this branch
    reversible: bool = False       # reverse playback exits to parent at spawn frame
    row: int = 0                   # canvas row, assigned by _layout_tree

    def height(self) -> int:
        """Number of rows this subtree occupies."""
        if not self.branches:
            return 1
        return 1 + sum(b.height() for b in self.branches.values())

    def last_frame(self) -> "DFrame | None":
        return self.frames[-1] if self.frames else None


# -- tree I/O -----------------------------------------------------------------

def _build_png_index(directory: Path) -> dict[str, Path]:
    """Return a map of numeric suffix → Path for every PNG directly in *directory*.

    Key is the zero-stripped decimal run at the end of the stem, e.g.:
      bounce-exit-frames-008.png  →  "8"
      008.png                     →  "8"
    """
    idx: dict[str, Path] = {}
    if not directory.is_dir():
        return idx
    for p in sorted(directory.glob("*.png")):
        m = re.search(r"(\d+)$", p.stem)
        if m:
            key = str(int(m.group(1)))
            idx.setdefault(key, p)
    return idx


def _resolve_png(stored: str, branch_dir: Path, root_dir: Path,
                 branch_index: dict[str, Path],
                 root_index: dict[str, Path]) -> Path:
    """Return the best Path for a stored frame filename.

    Resolution order (stops at first hit):
    1. Exact path under branch_dir  (branch's own subdirectory)
    2. Exact path under root_dir    (animation root)
    3. Numeric-suffix fallback in branch_index
    4. Numeric-suffix fallback in root_index
    5. branch_dir / stored as a missing sentinel
    """
    in_branch = branch_dir / stored
    if in_branch.exists():
        return in_branch
    in_root = root_dir / stored
    if in_root.exists():
        return in_root
    m = re.search(r"(\d+)$", Path(stored).stem)
    if m:
        key = str(int(m.group(1)))
        resolved = branch_index.get(key) or root_index.get(key)
        if resolved is not None:
            return resolved
    return in_branch   # missing sentinel — branch_dir preferred for error display


def load_tree(anim_dir: Path) -> tuple[DBranch, dict, dict]:
    """Load animation tree from frames.json.

    Returns (root_branch, director_config, raw_json_data).
    Frame paths that no longer exist are resolved by numeric-suffix fallback
    (branch subdir first, then root) so renamed folders load correctly.
    """
    fj = anim_dir / "frames.json"
    if not fj.exists():
        return DBranch(name=anim_dir.name), {}, {}
    raw = json.loads(fj.read_text(encoding="utf-8"))
    dcfg = raw.get("director", {})

    root_index = _build_png_index(anim_dir)

    def _parse(data: dict, name: str, parent=None,
               branch_dir: Path = anim_dir) -> DBranch:
        branch_index = (_build_png_index(branch_dir)
                        if branch_dir != anim_dir else root_index)
        b = DBranch(name=name, parent=parent)
        for f in data.get("frames", []):
            b.frames.append(DFrame(
                uid=f.get("uid", _uid()),
                png=_resolve_png(f["file"], branch_dir, anim_dir,
                                 branch_index, root_index),
                keyframe=f.get("keyframe", False),
                hold=f.get("hold", 1),
            ))
        for bname, bdata in data.get("branches", {}).items():
            child = _parse(bdata, bname, parent=b,
                           branch_dir=anim_dir / bname)
            child.spawn_uid  = bdata.get("spawn_uid")
            child.reversible = bdata.get("reversible", False)
            b.branches[bname] = child
        return b

    return _parse(raw, anim_dir.name), dcfg, raw


def save_tree(anim_dir: Path, root: DBranch, dcfg: dict, raw: dict):
    """Write the director tree back to frames.json, preserving blobs."""
    # uid -> existing frame entry (for preserving blobs)
    existing: dict[str, dict] = {}

    def _collect(data: dict):
        for f in data.get("frames", []):
            uid = f.get("uid")
            if uid:
                existing[uid] = f
        for bd in data.get("branches", {}).values():
            _collect(bd)
    _collect(raw)

    def _ser(branch: DBranch) -> dict:
        frames = []
        for i, f in enumerate(branch.frames):
            entry: dict = {
                "uid": f.uid,
                "index": i,
                "file": f.png.relative_to(anim_dir).as_posix(),
            }
            old = existing.get(f.uid, {})
            if "blobs" in old:
                entry["blobs"] = old["blobs"]
            if f.keyframe:
                entry["keyframe"] = True
                entry["hold"] = f.hold
            frames.append(entry)
        result: dict = {"frames": frames}
        if branch.parent is not None:
            if branch.spawn_uid:
                result["spawn_uid"] = branch.spawn_uid
            if branch.reversible:
                result["reversible"] = True
        if branch.branches:
            result["branches"] = {
                bname: _ser(child)
                for bname, child in branch.branches.items()
            }
        return result

    out = _ser(root)
    for key in ("gif", "bg", "tol"):
        if key in raw:
            out[key] = raw[key]
    out["director"] = dcfg
    (anim_dir / "frames.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8")


# -- layout -------------------------------------------------------------------

def _layout_tree(root: DBranch):
    """Assign abs_tick / row to every DFrame in the tree.

    abs_tick is the cumulative tick offset from time-zero, so frames are
    positioned proportionally on the horizontal (time) axis.  Child branches
    start at the tick immediately after the parent branch ends.
    """

    def _assign(branch: DBranch, start_row: int, start_tick: int):
        branch.row = start_row
        cum = start_tick
        for f in branch.frames:
            f.row      = start_row
            f.abs_tick = cum
            cum += f.hold if f.keyframe else 1
        child_row = start_row + 1
        for child in branch.branches.values():
            # child starts at the tick immediately after its spawn keyframe
            if child.spawn_uid:
                spawn_f = next((f for f in branch.frames
                                if f.uid == child.spawn_uid), None)
                if spawn_f is not None:
                    c_start = spawn_f.abs_tick + (spawn_f.hold if spawn_f.keyframe else 1)
                else:
                    c_start = cum
            else:
                c_start = cum
            _assign(child, child_row, c_start)
            child_row += child.height()

    _assign(root, 0, 0)


# -- path helpers -------------------------------------------------------------

def _branch_chain(branch: DBranch) -> list[DBranch]:
    """Return [root, ..., branch]."""
    chain: list[DBranch] = []
    b: DBranch | None = branch
    while b is not None:
        chain.append(b)
        b = b.parent
    chain.reverse()
    return chain


def _find_branch(root: DBranch, frame: DFrame) -> DBranch | None:
    """Find the branch that directly contains *frame*."""
    if frame in root.frames:
        return root
    for child in root.branches.values():
        hit = _find_branch(child, frame)
        if hit:
            return hit
    return None


def _path_frames_to(root: DBranch, target: DFrame) -> list[DFrame]:
    """Collect all frames from root to *target* (inclusive)."""
    branch = _find_branch(root, target)
    if branch is None:
        return []
    chain = _branch_chain(branch)
    result: list[DFrame] = []
    for b in chain:
        if b is chain[-1]:
            idx = b.frames.index(target)
            result.extend(b.frames[:idx + 1])
        else:
            result.extend(b.frames)
    return result


def _derived_name(root: DBranch, frame: DFrame) -> str:
    """Derive the animation name for the path reaching *frame*."""
    branch = _find_branch(root, frame)
    if branch is None:
        return root.name
    return "-".join(b.name for b in _branch_chain(branch))



# -- DirectorPanel ------------------------------------------------------------

class DirectorPanel:
    """Tree canvas + preview for animation timing and branching."""

    ZOOM_MIN  = 0.25
    ZOOM_MAX  = 3.0
    ZOOM_STEP = 0.15

    def __init__(self, parent: tk.Frame, anim_dir: Path, win: tk.Toplevel):
        self._parent   = parent
        self._anim_dir = anim_dir
        self._win      = win

        self._root: DBranch | None = None
        self._dcfg: dict = {}
        self._raw:  dict = {}

        self._zoom = 1.0
        self._selected: DFrame | None = None

        # flat index
        self._all_frames: list[DFrame] = []
        self._uid_to_branch: dict[str, DBranch] = {}

        # canvas
        self._canvas: tk.Canvas | None = None
        self._photos: dict[str, ImageTk.PhotoImage] = {}

        # preview
        self._pv_canvas: tk.Canvas | None = None
        self._pv_photo = None
        self._pv_playing = False
        self._pv_after_id = None
        self._pv_frames: list[DFrame] = []
        self._pv_current = 0
        self._pv_delay = tk.IntVar(value=100)
        self._pv_btn: tk.Button | None = None
        self._pv_lbl: tk.Label  | None = None

        self._dirty = False
        self._name_lbl: tk.Label | None = None
        self._zoom_lbl: tk.Label | None = None

        # canvas-level drag state (drag frame left/right to adjust hold,
        # or drag vertically to copy frame to another branch)
        self._cv_drag_frame:  DFrame | None  = None
        self._cv_drag_prev:   DFrame | None  = None
        self._cv_drag_x0:     float = 0.0
        self._cv_drag_y0:     float = 0.0
        self._cv_drag_hold0:  int   = 1
        self._cv_drag_moved:  bool  = False
        self._cv_drag_mode:   str   = ""          # "hold" | "copy" | ""
        self._cv_copy_target: DBranch | None = None

        # pool (left pane) drag state
        self._pool_drag_png:    Path | None      = None
        self._pool_drag_active: bool             = False
        self._pool_drag_press:  tuple[int, int]  = (0, 0)
        self._pool_drag_ghost:  tk.Toplevel | None = None
        self._pool_photos:      dict[Path, ImageTk.PhotoImage] = {}
        self._pool_inner:       tk.Frame | None  = None

        self._build(parent)

    # -- public ---------------------------------------------------------------

    def load(self, anim_dir: Path | None = None):
        """(Re-)load the tree from disk."""
        if anim_dir:
            self._anim_dir = anim_dir
        if not self._anim_dir or not (self._anim_dir / "frames.json").exists():
            self._root = DBranch(name=(self._anim_dir.name
                                       if self._anim_dir else "untitled"))
            self._dcfg = {"ticks_ms": 100}
            self._raw  = {}
            self._build_index()
            self._redraw()
            return
        self._root, self._dcfg, self._raw = load_tree(self._anim_dir)
        self._dcfg.setdefault("ticks_ms", 100)
        self._pv_delay.set(self._dcfg.get("ticks_ms", 100))
        self._build_index()
        _layout_tree(self._root)
        self._selected = None
        self._dirty = False
        self._pool_rebuild()
        self._redraw()
        if self._root and self._root.frames:
            self._select_frame(self._root.frames[0])

    def save(self):
        if self._root and self._anim_dir:
            self._dcfg["ticks_ms"] = self._pv_delay.get()
            save_tree(self._anim_dir, self._root, self._dcfg, self._raw)
            self._dirty = False

    @property
    def dirty(self) -> bool:
        return self._dirty

    # -- build UI -------------------------------------------------------------

    def _build(self, parent: tk.Frame):
        hdr = tk.Frame(parent, bg=BG_PANEL, padx=8, pady=4)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="Director", bg=BG_PANEL, fg=ACCENT,
                 font=("", 9, "bold")).pack(side=tk.LEFT)
        self._name_lbl = tk.Label(hdr, text="", bg=BG_PANEL, fg=FG,
                                  font=("Consolas", 10))
        self._name_lbl.pack(side=tk.LEFT, padx=(12, 0))

        # save button
        tk.Button(hdr, text="Save", command=self.save,
                  bg=BG_CARD, fg=GREEN, activeforeground=GREEN,
                  activebackground=BG_SEL, relief=tk.FLAT,
                  padx=10, pady=2, font=("", 8),
                  cursor="hand2").pack(side=tk.RIGHT, padx=4)

        # export pack button
        tk.Button(hdr, text="Export Pack\u2026", command=self._export_pack_dialog,
                  bg=BG_CARD, fg=ACCENT, activeforeground=ACCENT,
                  activebackground=BG_SEL, relief=tk.FLAT,
                  padx=10, pady=2, font=("", 8),
                  cursor="hand2").pack(side=tk.RIGHT, padx=4)

        # zoom controls
        self._zoom_lbl = tk.Label(hdr, text="100%", bg=BG_PANEL, fg=FG_DIM,
                                  font=("Consolas", 8), width=5)
        self._zoom_lbl.pack(side=tk.RIGHT, padx=4)
        tk.Button(hdr, text="+", command=lambda: self._zoom_by(self.ZOOM_STEP),
                  bg=BG_CARD, fg=FG, relief=tk.FLAT, padx=4, font=("", 8),
                  cursor="hand2").pack(side=tk.RIGHT)
        tk.Button(hdr, text="\u2212",
                  command=lambda: self._zoom_by(-self.ZOOM_STEP),
                  bg=BG_CARD, fg=FG, relief=tk.FLAT, padx=4, font=("", 8),
                  cursor="hand2").pack(side=tk.RIGHT)

        # three-pane layout: pool | canvas | preview
        hp = tk.PanedWindow(parent, orient=tk.HORIZONTAL, bg=BG,
                            sashwidth=5, sashrelief=tk.FLAT)
        hp.pack(fill=tk.BOTH, expand=True)
        pool_f = tk.Frame(hp, bg=BG_PANEL, width=160)
        tree_f = tk.Frame(hp, bg=BG_PANEL)
        pv_f   = tk.Frame(hp, bg=BG_PANEL, width=240)
        hp.add(pool_f, minsize=120)
        hp.add(tree_f, minsize=260)
        hp.add(pv_f,   minsize=180)
        self._build_pool(pool_f)
        self._build_canvas(tree_f)
        self._build_preview(pv_f)

    def _build_pool(self, parent: tk.Frame):
        tk.Label(parent, text="Frames", bg=BG_PANEL, fg=ACCENT,
                 font=("", 8, "bold"), pady=4).pack(fill=tk.X, padx=6)
        tk.Label(parent, text="drag \u2192 canvas",
                 bg=BG_PANEL, fg=FG_DIM, font=("", 7)).pack(fill=tk.X, padx=6)

        cf = tk.Frame(parent, bg=BG_PANEL)
        cf.pack(fill=tk.BOTH, expand=True)

        vbar = tk.Scrollbar(cf, orient=tk.VERTICAL,
                            bg=BG_CARD, troughcolor=BG_PANEL, relief=tk.FLAT)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        pool_cv = tk.Canvas(cf, bg=BG_PANEL, yscrollcommand=vbar.set,
                            highlightthickness=0)
        pool_cv.pack(fill=tk.BOTH, expand=True)
        vbar.config(command=pool_cv.yview)
        pool_cv.bind("<MouseWheel>",
                     lambda e: pool_cv.yview_scroll(
                         -1 if e.delta > 0 else 1, "units"))

        inner = tk.Frame(pool_cv, bg=BG_PANEL)
        pool_cv.create_window((0, 0), window=inner, anchor=tk.NW)
        inner.bind("<Configure>",
                   lambda _: pool_cv.configure(
                       scrollregion=pool_cv.bbox("all")))
        self._pool_inner = inner
        self._pool_rebuild()

    def _pool_rebuild(self):
        """Refresh the pool thumbnails from the current anim_dir root PNGs."""
        inner = self._pool_inner
        if inner is None:
            return
        for w in inner.winfo_children():
            w.destroy()
        self._pool_photos.clear()

        if not self._anim_dir or not self._anim_dir.is_dir():
            return

        pngs = sorted(self._anim_dir.glob("*.png"))
        if not pngs:
            tk.Label(inner, text="(no frames)", bg=BG_PANEL, fg=FG_DIM,
                     font=("", 8)).pack(pady=8, padx=4)
            return

        scale = _thumb_scale(pngs, THUMB_H)
        for png in pngs:
            photo = _make_thumb(png, scale)
            self._pool_photos[png] = photo

            lbl = tk.Label(inner, image=photo, bg=BG_CARD,
                           relief=tk.FLAT, borderwidth=1, cursor="hand2")
            lbl.pack(padx=4, pady=2)

            # filename label below thumbnail
            tk.Label(inner, text=png.stem, bg=BG_PANEL, fg=FG_DIM,
                     font=("Consolas", 7)).pack()

            lbl.bind("<ButtonPress-1>",   lambda e, p=png: self._pool_press(e, p))
            lbl.bind("<B1-Motion>",       lambda e, p=png: self._pool_motion(e, p))
            lbl.bind("<ButtonRelease-1>", lambda e, p=png: self._pool_release(e, p))

    def _build_canvas(self, parent: tk.Frame):
        cf = tk.Frame(parent, bg=BG_PANEL)
        cf.pack(fill=tk.BOTH, expand=True)
        hbar = tk.Scrollbar(cf, orient=tk.HORIZONTAL,
                            bg=BG_CARD, troughcolor=BG_PANEL, relief=tk.FLAT)
        hbar.pack(side=tk.BOTTOM, fill=tk.X)
        vbar = tk.Scrollbar(cf, orient=tk.VERTICAL,
                            bg=BG_CARD, troughcolor=BG_PANEL, relief=tk.FLAT)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas = tk.Canvas(cf, bg=BG_PANEL,
                                 xscrollcommand=hbar.set,
                                 yscrollcommand=vbar.set,
                                 highlightthickness=0)
        self._canvas.pack(fill=tk.BOTH, expand=True)
        hbar.config(command=self._canvas.xview)
        vbar.config(command=self._canvas.yview)

        cv = self._canvas
        cv.bind("<ButtonPress-1>",   self._cv_press)
        cv.bind("<B1-Motion>",       self._cv_drag)
        cv.bind("<ButtonRelease-1>", self._cv_release)
        cv.bind("<Control-MouseWheel>", self._on_zoom_wheel)
        cv.bind("<MouseWheel>",
                lambda e: cv.yview_scroll(-1 if e.delta > 0 else 1, "units"))
        cv.bind("<Shift-MouseWheel>",
                lambda e: cv.xview_scroll(-1 if e.delta > 0 else 1, "units"))
        cv.bind("<Shift-ButtonPress-1>",
                lambda e: cv.scan_mark(e.x, e.y))
        cv.bind("<Shift-B1-Motion>",
                lambda e: cv.scan_dragto(e.x, e.y, gain=1))

    def _build_preview(self, parent: tk.Frame):
        tk.Label(parent, text="Preview", bg=BG_PANEL, fg=ACCENT,
                 font=("", 9, "bold"), pady=4).pack(fill=tk.X, padx=8)
        self._pv_canvas = tk.Canvas(parent, bg=BG_PANEL, highlightthickness=0)
        self._pv_canvas.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        self._pv_canvas.bind("<Configure>", lambda _: self._pv_render())

        ctrl = tk.Frame(parent, bg=BG_PANEL, pady=4)
        ctrl.pack(fill=tk.X, padx=4)
        self._pv_btn = tk.Button(
            ctrl, text="\u25b6", command=self._pv_toggle,
            bg=BG_CARD, fg=GREEN, activeforeground=GREEN,
            activebackground=BG_SEL, relief=tk.FLAT,
            padx=6, pady=2, font=("", 9), cursor="hand2")
        self._pv_btn.pack(side=tk.LEFT)
        tk.Button(ctrl, text="\u25a0", command=self._pv_stop,
                  bg=BG_CARD, fg=RED, activeforeground=RED,
                  activebackground=BG_SEL, relief=tk.FLAT,
                  padx=6, pady=2, font=("", 9),
                  cursor="hand2").pack(side=tk.LEFT, padx=(2, 6))
        self._pv_lbl = tk.Label(ctrl, text="\u2014 / \u2014",
                                bg=BG_PANEL, fg=FG_DIM,
                                font=("Consolas", 8), width=7)
        self._pv_lbl.pack(side=tk.LEFT)

        sldr = tk.Frame(parent, bg=BG_PANEL, pady=2)
        sldr.pack(fill=tk.X, padx=6, pady=(0, 4))
        tk.Label(sldr, text="Tick ms", bg=BG_PANEL, fg=FG_DIM,
                 font=("", 7)).pack(side=tk.LEFT)
        tk.Scale(sldr, variable=self._pv_delay,
                 from_=20, to=2000, resolution=10, orient=tk.HORIZONTAL,
                 bg=BG_PANEL, fg=FG, troughcolor=BG_CARD,
                 highlightthickness=0, showvalue=True,
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

    # -- pool drag ------------------------------------------------------------

    _DRAG_THRESHOLD = 8

    def _pool_press(self, event, png: Path):
        self._pool_drag_png    = png
        self._pool_drag_active = False
        self._pool_drag_press  = (event.x_root, event.y_root)

    def _pool_motion(self, event, png: Path):
        dx = event.x_root - self._pool_drag_press[0]
        dy = event.y_root - self._pool_drag_press[1]
        if not self._pool_drag_active:
            if dx * dx + dy * dy < self._DRAG_THRESHOLD ** 2:
                return
            self._pool_drag_active = True
            self._pool_ghost_create(png)

        if self._pool_drag_ghost:
            self._pool_drag_ghost.geometry(
                f"+{event.x_root + 14}+{event.y_root + 14}")

        # highlight target branch on canvas
        tgt = self._canvas_branch_at_root(event.x_root, event.y_root)
        if tgt is not self._cv_copy_target:
            self._cv_copy_target = tgt
            self._redraw()

    def _pool_release(self, event, png: Path):
        self._pool_ghost_destroy()
        tgt = self._cv_copy_target
        self._cv_copy_target = None

        if self._pool_drag_active and tgt is not None:
            self._drop_png_to_branch(png, tgt)
        elif not self._pool_drag_active:
            # plain click — drop into active branch
            tgt2 = self._active_branch()
            if tgt2 is not None:
                self._drop_png_to_branch(png, tgt2)

        self._pool_drag_png    = None
        self._pool_drag_active = False
        self._redraw()

    def _pool_ghost_create(self, png: Path):
        if self._pool_drag_ghost:
            return
        photo = self._pool_photos.get(png)
        if photo is None:
            return
        g = tk.Toplevel(self._win)
        g.wm_overrideredirect(True)
        g.wm_attributes("-topmost", True)
        try:
            g.wm_attributes("-alpha", 0.72)
        except Exception:
            pass
        tk.Label(g, image=photo, bg=BG_CARD,
                 relief=tk.SOLID, borderwidth=1).pack()
        g._photo = photo
        self._pool_drag_ghost = g

    def _pool_ghost_destroy(self):
        if self._pool_drag_ghost:
            try:
                self._pool_drag_ghost.destroy()
            except Exception:
                pass
            self._pool_drag_ghost = None

    def _cursor_over_canvas(self, x_root: int, y_root: int) -> bool:
        cv = self._canvas
        if cv is None or not cv.winfo_exists():
            return False
        return (cv.winfo_rootx() <= x_root <= cv.winfo_rootx() + cv.winfo_width()
                and cv.winfo_rooty() <= y_root <= cv.winfo_rooty() + cv.winfo_height())

    def _canvas_branch_at_root(self, x_root: int, y_root: int) -> "DBranch | None":
        """Return the branch under the screen-absolute cursor, or None if off-canvas."""
        if not self._cursor_over_canvas(x_root, y_root):
            return None
        cv = self._canvas
        cx = cv.canvasx(x_root - cv.winfo_rootx())
        cy = cv.canvasy(y_root - cv.winfo_rooty())
        return self._branch_for_row(self._row_at_canvas_y(cy))

    def _active_branch(self) -> "DBranch | None":
        """The branch containing the currently selected frame, or the stem."""
        if self._selected:
            return self._uid_to_branch.get(self._selected.uid)
        return self._root

    def _drop_png_to_branch(self, png: Path, branch: DBranch):
        """Append *png* as a new keyframe to *branch*."""
        new_f = DFrame(uid=_uid(), png=png, keyframe=True, hold=1)
        branch.frames.append(new_f)
        self._dirty = True
        self._build_index()
        _layout_tree(self._root)
        self._redraw()
        self._select_frame(new_f)

    # -- index ----------------------------------------------------------------

    def _build_index(self):
        self._all_frames.clear()
        self._uid_to_branch.clear()

        def _walk(branch: DBranch):
            for f in branch.frames:
                self._all_frames.append(f)
                self._uid_to_branch[f.uid] = branch
            for child in branch.branches.values():
                _walk(child)

        if self._root:
            _walk(self._root)

    # -- zoom -----------------------------------------------------------------

    def _on_zoom_wheel(self, event):
        self._zoom_by(self.ZOOM_STEP if event.delta > 0 else -self.ZOOM_STEP)

    def _zoom_by(self, delta: float):
        old = self._zoom
        self._zoom = max(self.ZOOM_MIN, min(self.ZOOM_MAX, self._zoom + delta))
        if self._zoom != old:
            if self._zoom_lbl:
                self._zoom_lbl.config(text=f"{int(self._zoom * 100)}%")
            self._redraw()

    # -- drawing --------------------------------------------------------------

    def _fc(self, frame: DFrame, z: float) -> tuple[float, float]:
        """Centre coords for a frame card at current zoom *z*.

        Horizontal axis is proportional to abs_tick so frame spacing
        reflects actual timing.
        """
        x = frame.abs_tick * COL_W * z + (CARD_W * z) / 2 + (GAP_X * z) / 2
        y = frame.row * ROW_H * z + (CARD_H * z) / 2 + (GAP_Y * z) / 2
        return x, y

    def _redraw(self):
        cv = self._canvas
        if cv is None or not cv.winfo_exists():
            return
        cv.delete("all")
        self._photos.clear()

        if not self._all_frames:
            cv.create_text(200, 80,
                           text="No frames \u2014 save from Compose first",
                           fill=FG_DIM, font=("", 10))
            cv.configure(scrollregion=(0, 0, 400, 160))
            return

        z = self._zoom
        cw = CARD_W * z
        ch = CARD_H * z

        # compute uniform thumbnail scale
        pngs = [f.png for f in self._all_frames if f.png.exists()]
        th = max(8, int(THUMB_H * z))
        scale = _thumb_scale(pngs, th) if pngs else 1.0

        # 1) connections (behind everything)
        self._draw_connections(cv, z)

        # 2) nodes
        for f in self._all_frames:
            cx, cy = self._fc(f, z)
            x0, y0 = cx - cw / 2, cy - ch / 2
            x1, y1 = cx + cw / 2, cy + ch / 2

            tag = f"n_{f.uid}"
            is_sel = f is self._selected
            fill = BG_SEL if is_sel else BG_CARD
            outline = COL_KF if f.keyframe else (FG_DIM if is_sel else BG_CARD)
            bw = KF_BORDER if f.keyframe else 1

            cv.create_rectangle(x0, y0, x1, y1,
                                fill=fill, outline=outline, width=bw, tags=(tag,))

            # thumbnail
            try:
                photo = _make_thumb(f.png, scale)
                self._photos[f.uid] = photo
                cv.create_image(cx, cy, image=photo, anchor=tk.CENTER,
                                tags=(tag,))
            except Exception:
                cv.create_text(cx, cy, text="err", fill=RED,
                               font=("", max(7, int(8 * z))), tags=(tag,))

            # tick label (abs_tick)
            cv.create_text(cx, y1 - 8 * z, text=str(f.abs_tick), fill=FG_DIM,
                           font=("Consolas", max(6, int(7 * z))), tags=(tag,))

            # hold badge
            if f.keyframe and f.hold > 1:
                cv.create_text(x1 - 4 * z, y0 + 10 * z,
                               text=f"\u00d7{f.hold}", anchor=tk.NE,
                               fill=YELLOW,
                               font=("", max(6, int(7 * z))), tags=(tag,))

            # right-click target (invisible rect on top)
            hit = cv.create_rectangle(x0, y0, x1, y1,
                                      fill="", outline="", width=0,
                                      tags=(f"hit_{f.uid}",))
            cv.tag_bind(hit, "<Button-3>",
                        lambda e, fr=f: self._on_right_click(e, fr))

        # 3) empty-branch placeholders
        self._draw_empty_branches(cv, z, cw, ch)

        # 4) branch labels
        self._draw_branch_labels(cv, z)

        # 5) per-row tick rulers
        self._draw_row_rulers(cv, z)

        # 6) copy-drag target highlight
        if self._cv_copy_target is not None and self._cv_drag_mode == "copy":
            self._draw_copy_target(cv, z, cw, ch)

        # scrollregion — include empty branch rows
        all_rows = [f.row for f in self._all_frames]
        if self._root:
            all_rows += self._all_branch_rows(self._root)
        max_tick = max((f.abs_tick for f in self._all_frames), default=0)
        max_hold = max((f.hold if f.keyframe else 1
                        for f in self._all_frames), default=1)
        max_row  = max(all_rows, default=0)
        canvas_w = (max_tick + max_hold + 2) * COL_W * z
        canvas_h = (max_row * ROW_H + GAP_Y / 2 + CARD_H) * z + RULER_H * z + 20
        cv.configure(scrollregion=(0, 0, canvas_w, canvas_h))

    # -- connections ----------------------------------------------------------

    def _draw_connections(self, cv: tk.Canvas, z: float):
        if not self._root:
            return

        def _draw(branch: DBranch):
            # Index of the last frame that spawns a child branch; forward
            # arrows are not drawn past this point (the branch connector
            # already shows the continuation, and frames beyond are orphaned).
            spawn_uids = {c.spawn_uid for c in branch.branches.values()
                          if c.spawn_uid}
            frame_indices = {f.uid: i for i, f in enumerate(branch.frames)}
            last_spawn_idx = max(
                (frame_indices[uid] for uid in spawn_uids if uid in frame_indices),
                default=-1)

            # arrows between consecutive keyframes, up to the last spawn point
            kfs = [f for f in branch.frames if f.keyframe]
            for i in range(len(kfs) - 1):
                a, b = kfs[i], kfs[i + 1]
                if last_spawn_idx >= 0 and frame_indices.get(a.uid, 0) >= last_spawn_idx:
                    continue
                ax, ay = self._fc(a, z)
                bx, by = self._fc(b, z)
                sx = ax + CARD_W * z / 2
                ex = bx - CARD_W * z / 2
                aw = max(1, int(ARROW_W * z))
                ashape = (10 * z, 13 * z, 4 * z)

                if a.row == b.row:
                    cv.create_line(sx, ay, ex, by,
                                   fill=COL_ARROW, width=aw,
                                   arrow=tk.LAST, arrowshape=ashape)
                else:
                    mx = (sx + ex) / 2
                    cv.create_line(sx, ay, mx, ay, mx, by, ex, by,
                                   fill=COL_ARROW, width=aw,
                                   arrow=tk.LAST, arrowshape=ashape,
                                   smooth=True)


            # branch connection lines — drop from spawn frame bottom, enter child left
            for child in branch.branches.values():
                spawn_f = None
                if child.spawn_uid:
                    spawn_f = next((f for f in branch.frames
                                    if f.uid == child.spawn_uid), None)
                if spawn_f is None:
                    spawn_f = branch.last_frame()
                if spawn_f is None:
                    continue
                px, py = self._fc(spawn_f, z)
                if child.frames:
                    cx0, cy0 = self._fc(child.frames[0], z)
                else:
                    # empty branch — connect to placeholder position
                    cx0 = px
                    cy0 = child.row * ROW_H * z + (CARD_H * z) / 2 + (GAP_Y * z) / 2
                # start: bottom-centre of spawn frame
                start_x = px
                start_y = py + CARD_H * z / 2
                # end: left-centre of first child frame
                end_x = cx0 - CARD_W * z / 2
                end_y = cy0
                aw = max(1, int(ARROW_W * z))
                ashape = (10 * z, 13 * z, 4 * z)
                # right-angle path: drop straight down, then go right
                cv.create_line(start_x, start_y,
                               start_x, end_y,
                               end_x,   end_y,
                               fill=COL_BRANCH, width=aw,
                               arrow=tk.LAST, arrowshape=ashape,
                               dash=(int(4 * z), int(3 * z)))

            for child in branch.branches.values():
                _draw(child)

        _draw(self._root)

    def _draw_branch_labels(self, cv: tk.Canvas, z: float):
        def _draw(branch: DBranch):
            for bname, child in branch.branches.items():
                # compute label position from spawn frame or first child frame
                if child.frames:
                    lx, ly = self._fc(child.frames[0], z)
                elif child.spawn_uid:
                    spawn_f = next((f for f in branch.frames
                                    if f.uid == child.spawn_uid), None)
                    if spawn_f:
                        lx, _ = self._fc(spawn_f, z)
                    else:
                        lx = (CARD_W * z) / 2 + (GAP_X * z) / 2
                    ly = (child.row * ROW_H * z
                          + (CARD_H * z) / 2 + (GAP_Y * z) / 2)
                else:
                    _draw(child)
                    continue

                tag = f"blbl_{id(child)}"
                label = f"{bname} \u21a9" if child.reversible else bname
                item = cv.create_text(
                    lx, ly - CARD_H * z / 2 - 6 * z,
                    text=label, fill=COL_BRANCH,
                    font=("", max(7, int(9 * z)), "bold"),
                    anchor=tk.S, tags=(tag,))
                cv.tag_bind(item, "<Button-3>",
                            lambda e, b=child, p=branch: self._branch_label_menu(e, b, p))
                _draw(child)

        if self._root:
            _draw(self._root)

    def _branch_label_menu(self, event, branch: DBranch, parent_branch: DBranch):
        menu = tk.Menu(self._win, tearoff=False, bg=BG_CARD, fg=FG,
                       activebackground=BG_SEL, activeforeground=ACCENT)
        menu.add_command(
            label=f"Rename \u2018{branch.name}\u2019\u2026",
            command=lambda: self._rename_branch(branch, parent_branch))
        rev_label = ("\u2611 Reversible" if branch.reversible
                     else "\u2610 Reversible")
        menu.add_command(label=rev_label,
                         command=lambda: self._toggle_reversible(branch))
        menu.add_separator()
        menu.add_command(
            label=f"Delete Branch \u2018{branch.name}\u2019\u2026",
            command=lambda: self._delete_branch(branch))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _toggle_reversible(self, branch: DBranch):
        branch.reversible = not branch.reversible
        self._dirty = True
        self.save()
        self._redraw()

    def _rename_branch(self, branch: DBranch, parent_branch: DBranch):
        dlg = _InputDialog(self._win, "Rename Branch", "New name:", branch.name)
        name = dlg.result
        if not name or name == branch.name:
            return
        if name in parent_branch.branches:
            messagebox.showerror("Duplicate",
                                 f"Branch '{name}' already exists.",
                                 parent=self._win)
            return
        if any(c in name for c in r'\/:*?"<>|'):
            messagebox.showerror("Invalid Name",
                                 "Name contains invalid characters.",
                                 parent=self._win)
            return
        old_sub = self._anim_dir / self._branch_subdir(branch)
        del parent_branch.branches[branch.name]
        branch.name = name
        parent_branch.branches[name] = branch
        new_sub = self._anim_dir / self._branch_subdir(branch)
        if old_sub.exists() and old_sub != new_sub:
            try:
                old_sub.rename(new_sub)
            except Exception:
                pass
        self._dirty = True
        self._redraw()

    # -- per-row rulers -------------------------------------------------------

    def _draw_row_rulers(self, cv: tk.Canvas, z: float):
        """Draw a tick ruler directly below each branch row.

        Each ruler shows the full playback path for that branch (root + branch
        frames), with markers at each frame's abs_tick position.
        """
        if not self._root:
            return

        ppt = COL_W * z
        step = max(1, int(40 / max(1, ppt)))
        big_every = max(1, step * 5)
        # x-origin aligns with abs_tick=0 frame centre
        x_orig = ppt / 2

        def _draw_for_branch(branch: DBranch):
            if not branch.frames:
                for child in branch.branches.values():
                    _draw_for_branch(child)
                return

            row  = branch.frames[0].row
            path = _path_frames_to(self._root, branch.frames[-1])
            if not path:
                return

            last  = path[-1]
            total = last.abs_tick + (last.hold if last.keyframe else 1)

            # Ruler line sits in the GAP_Y space below the card row
            ry = row * ROW_H * z + (GAP_Y / 2 + CARD_H) * z + 8 * z

            x1 = x_orig + total * ppt
            cv.create_line(x_orig, ry, x1, ry, fill=FG_DIM, width=1)

            # Tick marks
            for t in range(0, total + 1, step):
                x   = x_orig + t * ppt
                big = (t % big_every == 0)
                hm  = 5 if big else 3
                cv.create_line(x, ry - hm, x, ry + hm, fill=FG_DIM, width=1)
                if big or step <= 2:
                    cv.create_text(x, ry + hm + 2, text=str(t), fill=FG_DIM,
                                   font=("Consolas", max(5, int(6 * z))),
                                   anchor=tk.N)

            # Frame markers
            for f in path:
                x      = x_orig + f.abs_tick * ppt
                is_sel = f is self._selected
                r      = max(3, int(4 * z))
                if f.keyframe:
                    fill = ACCENT if is_sel else COL_KF
                    cv.create_oval(x - r, ry - r, x + r, ry + r,
                                   fill=fill, outline=FG, width=1)
                else:
                    r2   = max(2, int(2.5 * z))
                    fill = ACCENT if is_sel else FG_DIM
                    cv.create_rectangle(x - r2, ry - r2, x + r2, ry + r2,
                                        fill=fill, outline="", width=0)

            for child in branch.branches.values():
                _draw_for_branch(child)

        _draw_for_branch(self._root)

    # -- canvas drag ----------------------------------------------------------

    def _cv_press(self, event):
        cv = self._canvas
        cx = cv.canvasx(event.x)
        cy = cv.canvasy(event.y)
        z  = self._zoom

        self._cv_drag_frame  = None
        self._cv_drag_prev   = None
        self._cv_drag_moved  = False
        self._cv_drag_mode   = ""
        self._cv_copy_target = None

        for f in self._all_frames:
            fx, fy = self._fc(f, z)
            if abs(cx - fx) <= CARD_W * z / 2 and abs(cy - fy) <= CARD_H * z / 2:
                self._cv_drag_frame = f
                self._cv_drag_x0    = cx
                self._cv_drag_y0    = cy
                branch = self._uid_to_branch.get(f.uid)
                if branch:
                    idx = branch.frames.index(f)
                    if idx > 0:
                        prev = branch.frames[idx - 1]
                        self._cv_drag_prev  = prev
                        self._cv_drag_hold0 = prev.hold
                    else:
                        self._cv_drag_hold0 = 0
                return

    def _cv_drag(self, event):
        if self._cv_drag_frame is None:
            return
        cv = self._canvas
        cx = cv.canvasx(event.x)
        cy = cv.canvasy(event.y)
        dx = cx - self._cv_drag_x0
        dy = cy - self._cv_drag_y0
        THRESHOLD = 8

        # determine drag mode on first significant movement
        if self._cv_drag_mode == "":
            if abs(dx) < THRESHOLD and abs(dy) < THRESHOLD:
                return
            self._cv_drag_mode = "copy" if abs(dy) > abs(dx) else "hold"
            self._cv_drag_moved = True

        if self._cv_drag_mode == "copy":
            # find the branch row under the cursor
            target_row   = self._row_at_canvas_y(cy)
            target_branch = self._branch_for_row(target_row)
            src_branch   = self._uid_to_branch.get(self._cv_drag_frame.uid)
            if target_branch is not None and target_branch is not src_branch:
                self._cv_copy_target = target_branch
            else:
                self._cv_copy_target = None
            self._redraw()

        elif self._cv_drag_mode == "hold":
            if self._cv_drag_prev is None:
                return
            if abs(dx) < 4:
                return
            ppt      = COL_W * self._zoom
            delta    = round(dx / max(1, ppt))
            new_hold = max(1, self._cv_drag_hold0 + delta)
            prev     = self._cv_drag_prev
            if new_hold != prev.hold:
                if not prev.keyframe:
                    prev.keyframe = True
                prev.hold  = new_hold
                self._dirty = True
                _layout_tree(self._root)
                self._redraw()

    def _cv_release(self, event):
        if not self._cv_drag_moved and self._cv_drag_frame is not None:
            self._select_frame(self._cv_drag_frame)
        elif self._cv_drag_mode == "copy" and self._cv_copy_target is not None:
            self._copy_frame_to_branch(self._cv_drag_frame, self._cv_copy_target)
        self._cv_drag_frame  = None
        self._cv_drag_prev   = None
        self._cv_drag_moved  = False
        self._cv_drag_mode   = ""
        self._cv_copy_target = None
        self._redraw()

    # -- interactions ---------------------------------------------------------

    def _select_frame(self, frame: DFrame):
        old = self._selected
        self._selected = frame
        if self._root and self._name_lbl:
            self._name_lbl.config(text=_derived_name(self._root, frame))
        if self._root:
            self._pv_frames = _path_frames_to(self._root, frame)
            self._pv_stop()
            self._pv_play()
        if old is not frame:
            self._redraw()

    def _on_right_click(self, event, frame: DFrame):
        branch = self._uid_to_branch.get(frame.uid)
        if not branch:
            return

        menu = tk.Menu(self._win, tearoff=False, bg=BG_CARD, fg=FG,
                       activebackground=BG_SEL, activeforeground=ACCENT)

        # keyframe toggle
        kf_label = "Remove Keyframe" if frame.keyframe else "Set as Keyframe"
        menu.add_command(label=kf_label,
                         command=lambda: self._toggle_kf(frame))

        if frame.keyframe:
            menu.add_separator()
            menu.add_command(label=f"Hold: {frame.hold} tick(s)\u2026",
                             command=lambda: self._set_hold(frame))

        # branching — any keyframe can spawn a new branch
        if frame.keyframe:
            menu.add_separator()
            menu.add_command(label="New Branch\u2026",
                             command=lambda: self._new_branch(branch, frame))

        # branch deletion (any non-root branch)
        if branch.parent is not None:
            menu.add_separator()
            menu.add_command(label=f"Delete Branch \u2018{branch.name}\u2019\u2026",
                             command=lambda b=branch: self._delete_branch(b))

        menu.add_separator()
        menu.add_command(label="Duplicate Frame",
                         command=lambda: self._duplicate_frame(branch, frame))
        can_remove = len(branch.frames) > 1 or branch.parent is not None
        if can_remove:
            menu.add_command(label="Remove Frame",
                             command=lambda: self._remove_frame(branch, frame))
        menu.add_separator()
        menu.add_command(label="Mark All as Keyframes",
                         command=lambda: self._mark_all_kf(branch))

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _toggle_kf(self, frame: DFrame):
        frame.keyframe = not frame.keyframe
        if not frame.keyframe:
            frame.hold = 1
        self._dirty = True
        self._redraw()

    def _set_hold(self, frame: DFrame):
        dlg = _InputDialog(self._win, "Set Hold",
                           "Hold ticks (1 = normal):", str(frame.hold))
        if dlg.result:
            try:
                v = int(dlg.result)
                if v >= 1:
                    frame.hold = v
                    self._dirty = True
                    self._redraw()
            except ValueError:
                pass


    def _mark_all_kf(self, branch: DBranch):
        for f in branch.frames:
            f.keyframe = True
        self._dirty = True
        self._redraw()

    def _new_branch(self, parent_branch: DBranch, spawn_frame: DFrame):
        dlg = _InputDialog(self._win, "New Branch", "Branch name:", "")
        name = dlg.result
        if not name:
            return
        if name in parent_branch.branches:
            messagebox.showerror("Duplicate",
                                 f"Branch '{name}' already exists.",
                                 parent=self._win)
            return
        if any(c in name for c in r'\/:*?"<>|'):
            messagebox.showerror("Invalid Name",
                                 "Name contains invalid characters.",
                                 parent=self._win)
            return

        # compute subdirectory path
        sub = self._branch_subdir(parent_branch) / name
        full = self._anim_dir / sub
        full.mkdir(parents=True, exist_ok=True)

        # branch starts empty — frames are added by dragging
        child = DBranch(name=name, frames=[], parent=parent_branch,
                        spawn_uid=spawn_frame.uid)
        parent_branch.branches[name] = child

        self._dirty = True
        self._build_index()
        _layout_tree(self._root)
        self._redraw()

    def _branch_subdir(self, branch: DBranch) -> Path:
        """Relative subdirectory within the animation folder for *branch*."""
        chain = _branch_chain(branch)
        parts = [b.name for b in chain[1:]]   # skip root (stem)
        return Path(*parts) if parts else Path(".")

    def _branch_in_subtree(self, query: DBranch, subtree_root: DBranch) -> bool:
        """Return True if *query* is *subtree_root* or any of its descendants."""
        if query is subtree_root:
            return True
        return any(self._branch_in_subtree(query, c)
                   for c in subtree_root.branches.values())

    def _delete_branch(self, branch: DBranch):
        if branch.parent is None:
            return  # never delete the root
        name = branch.name
        if not messagebox.askyesno(
                "Delete Branch",
                f"Delete branch \u2018{name}\u2019 and all its sub-branches?\n"
                "Branch files will be removed from disk.",
                parent=self._win):
            return

        # Compute disk path before unlinking from tree
        sub = self._anim_dir / self._branch_subdir(branch)

        # Clear selection if it lives inside the branch being deleted
        if self._selected:
            sel_b = self._uid_to_branch.get(self._selected.uid)
            if sel_b and self._branch_in_subtree(sel_b, branch):
                self._selected = None

        # Unlink from parent
        del branch.parent.branches[name]

        # Remove directory tree from disk
        if sub.exists() and sub.is_dir() and sub != self._anim_dir:
            shutil.rmtree(sub, ignore_errors=True)

        self._dirty = True
        self._build_index()
        _layout_tree(self._root)
        self._redraw()

    # -- frame operations -----------------------------------------------------

    def _copy_frame_to_branch(self, frame: DFrame, target: DBranch):
        """Append a copy of *frame* to *target* branch."""
        new_f = DFrame(
            uid=_uid(),
            png=frame.png,
            keyframe=frame.keyframe,
            hold=frame.hold,
        )
        target.frames.append(new_f)
        self._dirty = True
        self._build_index()
        _layout_tree(self._root)
        self._redraw()
        self._select_frame(new_f)

    def _duplicate_frame(self, branch: DBranch, frame: DFrame):
        """Insert a copy of *frame* immediately after itself in *branch*."""
        idx = branch.frames.index(frame)
        new_f = DFrame(
            uid=_uid(),
            png=frame.png,
            keyframe=frame.keyframe,
            hold=frame.hold,
        )
        branch.frames.insert(idx + 1, new_f)
        self._dirty = True
        self._build_index()
        _layout_tree(self._root)
        self._redraw()

    def _remove_frame(self, branch: DBranch, frame: DFrame):
        """Remove *frame* from *branch* (guards against removing stem's last frame)."""
        if branch.parent is None and len(branch.frames) <= 1:
            return
        branch.frames.remove(frame)
        if self._selected is frame:
            self._selected = branch.frames[0] if branch.frames else None
        self._dirty = True
        self._build_index()
        _layout_tree(self._root)
        self._redraw()

    # -- row / branch helpers -------------------------------------------------

    def _row_at_canvas_y(self, canvas_y: float) -> int:
        """Return the branch row number that contains canvas Y coordinate."""
        z = self._zoom
        return max(0, int(canvas_y / (ROW_H * z)))

    def _branch_for_row(self, row: int) -> "DBranch | None":
        """Return the branch occupying *row*, including empty branches."""
        # check populated frames first
        for f in self._all_frames:
            if f.row == row:
                return self._uid_to_branch.get(f.uid)
        # fall back to empty branches
        if self._root:
            return self._find_branch_by_row(self._root, row)
        return None

    def _find_branch_by_row(self, branch: DBranch, row: int) -> "DBranch | None":
        if branch.row == row and not branch.frames:
            return branch
        for child in branch.branches.values():
            hit = self._find_branch_by_row(child, row)
            if hit:
                return hit
        return None

    def _all_branch_rows(self, branch: DBranch) -> list[int]:
        """Collect every branch.row in the subtree (for scrollregion sizing)."""
        rows = [branch.row]
        for child in branch.branches.values():
            rows.extend(self._all_branch_rows(child))
        return rows

    # -- empty-branch placeholder drawing ------------------------------------

    def _draw_empty_branches(self, cv: tk.Canvas, z: float,
                             cw: float, ch: float):
        if not self._root:
            return

        def _draw(branch: DBranch):
            if not branch.frames and branch.parent is not None:
                # compute placeholder position from spawn frame
                spawn_f = None
                if branch.spawn_uid:
                    spawn_f = next((f for f in branch.parent.frames
                                    if f.uid == branch.spawn_uid), None)
                cx = ((spawn_f.abs_tick * COL_W * z + (CARD_W * z) / 2 + (GAP_X * z) / 2)
                      if spawn_f else (CARD_W * z) / 2 + (GAP_X * z) / 2)
                cy = (branch.row * ROW_H * z
                      + (CARD_H * z) / 2 + (GAP_Y * z) / 2)
                x0, y0 = cx - cw / 2, cy - ch / 2
                x1, y1 = cx + cw / 2, cy + ch / 2
                cv.create_rectangle(x0, y0, x1, y1,
                                    outline=COL_BRANCH, fill=BG_PANEL,
                                    width=max(1, int(z)), dash=(int(4 * z), int(3 * z)))
                cv.create_text(cx, cy, text="drag\nhere",
                               fill=COL_BRANCH,
                               font=("", max(6, int(7 * z))), justify=tk.CENTER)
            for child in branch.branches.values():
                _draw(child)

        _draw(self._root)

    def _draw_copy_target(self, cv: tk.Canvas, z: float,
                          cw: float, ch: float):
        """Highlight the branch row that is the current copy-drag target."""
        tgt = self._cv_copy_target
        if tgt is None:
            return
        if tgt.frames:
            xs = [self._fc(f, z)[0] for f in tgt.frames]
            x0 = min(xs) - cw / 2 - 4
            x1 = max(xs) + cw / 2 + 4
            row = tgt.frames[0].row
        else:
            spawn_f = None
            if tgt.spawn_uid and tgt.parent:
                spawn_f = next((f for f in tgt.parent.frames
                                if f.uid == tgt.spawn_uid), None)
            cx = ((spawn_f.abs_tick * COL_W * z + (CARD_W * z) / 2 + (GAP_X * z) / 2)
                  if spawn_f else (CARD_W * z) / 2 + (GAP_X * z) / 2)
            x0 = cx - cw / 2 - 4
            x1 = cx + cw / 2 + 4
            row = tgt.row
        y0 = row * ROW_H * z + (GAP_Y / 2) * z - 4
        y1 = y0 + ch + 8
        cv.create_rectangle(x0, y0, x1, y1,
                             outline=ACCENT, fill="", width=2,
                             dash=(int(4 * z), int(3 * z)))

    # -- preview --------------------------------------------------------------

    def _pv_render(self):
        cv = self._pv_canvas
        if cv is None or not cv.winfo_exists():
            return
        cv.delete("all")
        if not self._pv_frames:
            cv.create_text(cv.winfo_width() // 2 or 60,
                           cv.winfo_height() // 2 or 60,
                           text="Select a frame", fill=FG_DIM, font=("", 9))
            if self._pv_lbl and self._pv_lbl.winfo_exists():
                self._pv_lbl.config(text="\u2014 / \u2014")
            return

        idx = min(self._pv_current, len(self._pv_frames) - 1)
        frame = self._pv_frames[idx]
        try:
            img = Image.open(frame.png).convert("RGBA")
        except Exception:
            return

        cw_px = cv.winfo_width() or 240
        ch_px = cv.winfo_height() or 240
        s = max(1.0, min(MAX_SCALE,
                         min(cw_px / max(1, img.width),
                             ch_px / max(1, img.height))))
        w = max(1, round(img.width * s))
        h = max(1, round(img.height * s))
        img = img.resize((w, h), Image.NEAREST)

        bg_hex = cv.cget("bg")
        rgb = tuple(int(bg_hex.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        flat = Image.new("RGBA", (w, h), (*rgb, 255))
        flat = Image.alpha_composite(flat, img)
        self._pv_photo = ImageTk.PhotoImage(flat)
        cv.create_image(cw_px // 2, ch_px // 2,
                        image=self._pv_photo, anchor=tk.CENTER)

        if self._pv_lbl and self._pv_lbl.winfo_exists():
            self._pv_lbl.config(text=f"{idx + 1} / {len(self._pv_frames)}")

    def _pv_tick(self):
        if not self._pv_playing or not self._pv_frames:
            return
        nxt = self._pv_current + 1
        # Loop back to start instead of stopping
        self._pv_current = nxt if nxt < len(self._pv_frames) else 0
        self._pv_render()
        if self._pv_canvas and self._pv_canvas.winfo_exists():
            f = self._pv_frames[self._pv_current]
            hold = f.hold if f.keyframe else 1
            self._pv_after_id = self._win.after(
                self._pv_delay.get() * hold, self._pv_tick)

    def _pv_toggle(self):
        if self._pv_playing:
            self._pv_pause()
        else:
            self._pv_play()

    def _pv_play(self):
        if not self._pv_frames:
            return
        self._pv_playing = True
        if self._pv_btn and self._pv_btn.winfo_exists():
            self._pv_btn.config(text="\u23f8")
        self._pv_current = 0
        self._pv_render()
        # Schedule first advance after frame 0's hold — do NOT call _pv_tick()
        # directly or frame 0 is overwritten before it is ever seen.
        if self._pv_canvas and self._pv_canvas.winfo_exists():
            f = self._pv_frames[0]
            hold = f.hold if f.keyframe else 1
            self._pv_after_id = self._win.after(
                self._pv_delay.get() * hold, self._pv_tick)

    def _pv_pause(self):
        self._pv_playing = False
        if self._pv_btn and self._pv_btn.winfo_exists():
            self._pv_btn.config(text="\u25b6")
        if self._pv_after_id:
            try:
                self._win.after_cancel(self._pv_after_id)
            except Exception:
                pass
            self._pv_after_id = None

    def _pv_stop(self):
        self._pv_pause()
        self._pv_current = 0
        self._pv_render()

    # -- prefs ----------------------------------------------------------------

    _PREFS_PATH = Path.home() / ".gooey-sprites" / "prefs.json"

    def _load_prefs(self) -> dict:
        try:
            return json.loads(self._PREFS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_prefs(self, prefs: dict):
        try:
            self._PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._PREFS_PATH.write_text(
                json.dumps(prefs, indent=2), encoding="utf-8")
        except Exception:
            pass

    # -- export pack ----------------------------------------------------------

    _PACK_NAME_RE = re.compile(r"^[a-z0-9_-]+$")

    def _export_pack_dialog(self):
        if not self._root or not self._anim_dir:
            messagebox.showinfo("Export Pack",
                                "No animation loaded.",
                                parent=self._win)
            return

        prefs = self._load_prefs()

        # -- pack name --------------------------------------------------------
        dlg = _InputDialog(self._win, "Export Pack",
                           "Pack name ([a-z0-9_-]):",
                           prefs.get("last_pack_name", self._anim_dir.name))
        pack_name = dlg.result
        if not pack_name:
            return
        if not self._PACK_NAME_RE.match(pack_name):
            messagebox.showerror("Invalid Name",
                                 "Pack name must match [a-z0-9_-]+.",
                                 parent=self._win)
            return

        # -- target folder ----------------------------------------------------
        initial_dir = prefs.get("last_export_dir", str(Path.home()))
        chosen = filedialog.askdirectory(
            title="Choose export folder",
            initialdir=initial_dir,
            parent=self._win,
            mustexist=True,
        )
        if not chosen:
            return

        target_dir = Path(chosen)

        # -- run export -------------------------------------------------------
        missing = pack_export.export_pack(self._root, pack_name, target_dir)

        # -- update prefs only on success / partial success ------------------
        prefs["last_pack_name"] = pack_name
        prefs["last_export_dir"] = str(target_dir)
        self._save_prefs(prefs)

        # -- report -----------------------------------------------------------
        if missing:
            names = "\n".join(str(p) for p in missing[:10])
            suffix = f"\n\u2026and {len(missing) - 10} more" if len(missing) > 10 else ""
            messagebox.showwarning(
                "Export Pack — missing files",
                f"Pack written to:\n{target_dir / pack_name}\n\n"
                f"The following PNGs were missing or unreadable "
                f"({len(missing)} total):\n{names}{suffix}",
                parent=self._win)
        else:
            frame_count = sum(
                1 for f in self._all_frames if f.png.exists())
            messagebox.showinfo(
                "Export Pack",
                f"Pack \u2018{pack_name}\u2019 exported successfully.\n"
                f"{frame_count} frame(s) written to:\n{target_dir / pack_name}",
                parent=self._win)
