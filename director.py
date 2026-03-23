"""
DirectorPanel -- tree-based animation timing, branching, and interpolation editor.

Embedded in the ComposeWindow as a tab alongside the Compose timeline.
Operates on a saved animation folder (reads/writes frames.json).
"""

import json
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import tkinter as tk
from tkinter import messagebox
from PIL import Image, ImageTk

from constants import (
    BG, BG_PANEL, BG_CARD, BG_SEL,
    FG, FG_DIM, ACCENT, RED, GREEN, YELLOW,
    MAX_SCALE,
)
from image_helpers import _make_thumb, _thumb_scale
from dialogs import _InputDialog


# -- constants ----------------------------------------------------------------

CARD_W  = 72       # card width at zoom 1
CARD_H  = 96       # card height at zoom 1
GAP_X   = 40       # horizontal gap between cards
GAP_Y   = 50       # vertical gap between rows
COL_W   = CARD_W + GAP_X
ROW_H   = CARD_H + GAP_Y
THUMB_H = 72       # thumbnail target height at zoom 1
KF_BORDER = 3
ARROW_W = 2

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
    tween: str | None = None        # None (hold previous) or "crossfade"
    tween_steps: int = 0
    col: int = 0                    # layout column
    row: int = 0                    # layout row
    _photo: object = field(default=None, repr=False)


@dataclass
class DBranch:
    """A branch (or the stem) of the animation tree."""
    name: str
    anchor_uid: str | None = None
    frames: list[DFrame] = field(default_factory=list)
    branches: dict[str, "DBranch"] = field(default_factory=dict)
    parent: "DBranch | None" = field(default=None, repr=False)

    def height(self) -> int:
        """Number of rows this subtree occupies."""
        if not self.branches:
            return 1
        return 1 + sum(b.height() for b in self.branches.values())

    def last_frame(self) -> "DFrame | None":
        return self.frames[-1] if self.frames else None

    def is_rightmost(self, frame: DFrame) -> bool:
        return bool(self.frames) and frame is self.frames[-1]


# -- tree I/O -----------------------------------------------------------------

def load_tree(anim_dir: Path) -> tuple[DBranch, dict, dict]:
    """Load animation tree from frames.json.

    Returns (root_branch, director_config, raw_json_data).
    """
    fj = anim_dir / "frames.json"
    if not fj.exists():
        return DBranch(name=anim_dir.name), {}, {}
    raw = json.loads(fj.read_text(encoding="utf-8"))
    dcfg = raw.get("director", {})

    def _parse(data: dict, name: str, parent=None) -> DBranch:
        branch = DBranch(name=name, anchor_uid=data.get("anchor_uid"),
                         parent=parent)
        for f in data.get("frames", []):
            branch.frames.append(DFrame(
                uid=f.get("uid", _uid()),
                png=anim_dir / f["file"],
                keyframe=f.get("keyframe", False),
                hold=f.get("hold", 1),
                tween=f.get("tween"),
                tween_steps=f.get("tween_steps", 0),
            ))
        for bname, bdata in data.get("branches", {}).items():
            branch.branches[bname] = _parse(bdata, bname, parent=branch)
        return branch

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
                if f.tween:
                    entry["tween"] = f.tween
                    entry["tween_steps"] = f.tween_steps
            frames.append(entry)
        result: dict = {"frames": frames}
        if branch.branches:
            bdict = {}
            for bname, child in branch.branches.items():
                bser = _ser(child)
                bser["anchor_uid"] = child.anchor_uid
                bdict[bname] = bser
            result["branches"] = bdict
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
    """Assign col / row to every DFrame in the tree."""

    def _assign(branch: DBranch, start_row: int, start_col: int):
        for i, f in enumerate(branch.frames):
            f.col = start_col + i
            f.row = start_row
        child_col = (start_col + len(branch.frames) - 1) if branch.frames else start_col
        child_row = start_row + 1
        for child in branch.branches.values():
            _assign(child, child_row, child_col)
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
    """Collect frames from root to *target* (inclusive), skipping anchor dupes."""
    branch = _find_branch(root, target)
    if branch is None:
        return []
    chain = _branch_chain(branch)
    result: list[DFrame] = []
    for b in chain:
        if b is chain[-1]:
            idx = b.frames.index(target)
            seg = b.frames[:idx + 1]
        else:
            seg = list(b.frames)
        for j, f in enumerate(seg):
            # skip anchor (duplicate of parent's last) except for the stem
            if b is not chain[0] and j == 0:
                continue
            result.append(f)
    return result


def _derived_name(root: DBranch, frame: DFrame) -> str:
    """Derive the animation name for the path reaching *frame*."""
    branch = _find_branch(root, frame)
    if branch is None:
        return root.name
    return "-".join(b.name for b in _branch_chain(branch))


# -- crossfade ----------------------------------------------------------------

def crossfade_images(img_a: Image.Image, img_b: Image.Image,
                     steps: int) -> list[Image.Image]:
    """Generate *steps* blended frames between *img_a* and *img_b*."""
    w = max(img_a.width, img_b.width)
    h = max(img_a.height, img_b.height)

    def _pad(img: Image.Image) -> Image.Image:
        if img.size == (w, h):
            return img.convert("RGBA")
        canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        canvas.paste(img.convert("RGBA"),
                     ((w - img.width) // 2, (h - img.height) // 2))
        return canvas

    a, b = _pad(img_a), _pad(img_b)
    return [Image.blend(a, b, i / (steps + 1)) for i in range(1, steps + 1)]


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

        hp = tk.PanedWindow(parent, orient=tk.HORIZONTAL, bg=BG,
                            sashwidth=5, sashrelief=tk.FLAT)
        hp.pack(fill=tk.BOTH, expand=True)
        tree_f = tk.Frame(hp, bg=BG_PANEL)
        pv_f   = tk.Frame(hp, bg=BG_PANEL, width=260)
        hp.add(tree_f, minsize=300)
        hp.add(pv_f,   minsize=200)
        self._build_canvas(tree_f)
        self._build_preview(pv_f)

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
        """Centre coords for a frame card at current zoom *z*."""
        x = frame.col * COL_W * z + (CARD_W * z) / 2 + (GAP_X * z) / 2
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

            # anchor marker
            branch = self._uid_to_branch.get(f.uid)
            if branch and branch.anchor_uid and f is branch.frames[0]:
                cv.create_text(cx, y0 + 10 * z, text="\u2693",
                               fill=COL_ANCHOR,
                               font=("", max(7, int(9 * z))), tags=(tag,))

            # thumbnail
            try:
                photo = _make_thumb(f.png, scale)
                self._photos[f.uid] = photo
                cv.create_image(cx, cy, image=photo, anchor=tk.CENTER,
                                tags=(tag,))
            except Exception:
                cv.create_text(cx, cy, text="err", fill=RED,
                               font=("", max(7, int(8 * z))), tags=(tag,))

            # index label
            idx = branch.frames.index(f) if branch and f in branch.frames else 0
            cv.create_text(cx, y1 - 8 * z, text=str(idx), fill=FG_DIM,
                           font=("Consolas", max(6, int(7 * z))), tags=(tag,))

            # hold badge
            if f.keyframe and f.hold > 1:
                cv.create_text(x1 - 4 * z, y0 + 10 * z,
                               text=f"\u00d7{f.hold}", anchor=tk.NE,
                               fill=YELLOW,
                               font=("", max(6, int(7 * z))), tags=(tag,))

            # click target (invisible rect on top)
            hit = cv.create_rectangle(x0, y0, x1, y1,
                                      fill="", outline="", width=0)
            cv.tag_bind(hit, "<Button-1>",
                        lambda _e, fr=f: self._on_click(fr))
            cv.tag_bind(hit, "<Button-3>",
                        lambda e, fr=f: self._on_right_click(e, fr))

        # 3) branch labels
        self._draw_branch_labels(cv, z)

        # scrollregion
        max_col = max((f.col for f in self._all_frames), default=0)
        max_row = max((f.row for f in self._all_frames), default=0)
        cv.configure(scrollregion=(
            0, 0, (max_col + 2) * COL_W * z, (max_row + 2) * ROW_H * z))

    # -- connections ----------------------------------------------------------

    def _draw_connections(self, cv: tk.Canvas, z: float):
        if not self._root:
            return

        def _draw(branch: DBranch):
            # arrows between consecutive keyframes
            kfs = [f for f in branch.frames if f.keyframe]
            for i in range(len(kfs) - 1):
                a, b = kfs[i], kfs[i + 1]
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

                # interpolation label
                if b.tween:
                    label = b.tween
                    if b.tween_steps:
                        label += f" \u00d7{b.tween_steps}"
                    cv.create_text((sx + ex) / 2,
                                   (ay + by) / 2 - 10 * z,
                                   text=label, fill=FG_DIM,
                                   font=("", max(6, int(7 * z))))

            # branch connection lines
            last = branch.last_frame()
            if last:
                for child in branch.branches.values():
                    if not child.frames:
                        continue
                    anchor = child.frames[0]
                    px, py = self._fc(last, z)
                    ax, ay = self._fc(anchor, z)
                    s_y = py + CARD_H * z / 2
                    e_y = ay - CARD_H * z / 2
                    m_y = (s_y + e_y) / 2
                    aw = max(1, int(ARROW_W * z))
                    ashape = (10 * z, 13 * z, 4 * z)
                    cv.create_line(px, s_y, px, m_y, ax, m_y, ax, e_y,
                                   fill=COL_BRANCH, width=aw,
                                   arrow=tk.LAST, arrowshape=ashape,
                                   smooth=True, dash=(int(4 * z), int(3 * z)))

            for child in branch.branches.values():
                _draw(child)

        _draw(self._root)

    def _draw_branch_labels(self, cv: tk.Canvas, z: float):
        def _draw(branch: DBranch):
            for bname, child in branch.branches.items():
                if child.frames:
                    cx, cy = self._fc(child.frames[0], z)
                    cv.create_text(cx, cy - CARD_H * z / 2 - 6 * z,
                                   text=bname, fill=COL_BRANCH,
                                   font=("", max(7, int(9 * z)), "bold"),
                                   anchor=tk.S)
                _draw(child)
        if self._root:
            _draw(self._root)

    # -- interactions ---------------------------------------------------------

    def _on_click(self, frame: DFrame):
        self._select_frame(frame)

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
            # tween submenu
            tw = tk.Menu(menu, tearoff=False, bg=BG_CARD, fg=FG,
                         activebackground=BG_SEL, activeforeground=ACCENT)
            cur = frame.tween or "none"
            tw.add_command(
                label=f"{'> ' if cur == 'none' else '  '}None (hold previous)",
                command=lambda: self._set_tween(frame, None, 0))
            tw.add_command(
                label=f"{'> ' if cur == 'crossfade' else '  '}Crossfade\u2026",
                command=lambda: self._set_tween_cf(frame))
            menu.add_cascade(label="Tween", menu=tw)

        # branching (rightmost frame only)
        if branch.is_rightmost(frame):
            menu.add_separator()
            menu.add_command(label="New Branch\u2026",
                             command=lambda: self._new_branch(branch, frame))

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
            frame.tween = None
            frame.tween_steps = 0
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

    def _set_tween(self, frame: DFrame, tween, steps):
        frame.tween = tween
        frame.tween_steps = steps
        self._dirty = True
        self._redraw()

    def _set_tween_cf(self, frame: DFrame):
        dlg = _InputDialog(self._win, "Crossfade Steps",
                           "Interpolated frames to generate:",
                           str(frame.tween_steps or 4))
        if dlg.result:
            try:
                v = int(dlg.result)
                if v >= 1:
                    frame.tween = "crossfade"
                    frame.tween_steps = v
                    self._dirty = True
                    self._redraw()
            except ValueError:
                pass

    def _mark_all_kf(self, branch: DBranch):
        for f in branch.frames:
            f.keyframe = True
        self._dirty = True
        self._redraw()

    def _new_branch(self, parent_branch: DBranch, branch_point: DFrame):
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

        # copy anchor
        anchor_dst = full / "000.png"
        shutil.copy2(branch_point.png, anchor_dst)

        anchor = DFrame(uid=_uid(), png=anchor_dst, keyframe=True, hold=1)
        child = DBranch(name=name, anchor_uid=branch_point.uid,
                        frames=[anchor], parent=parent_branch)
        parent_branch.branches[name] = child

        self._dirty = True
        self._build_index()
        _layout_tree(self._root)
        self._redraw()
        self._select_frame(anchor)

    def _branch_subdir(self, branch: DBranch) -> Path:
        """Relative subdirectory within the animation folder for *branch*."""
        chain = _branch_chain(branch)
        parts = [b.name for b in chain[1:]]   # skip root (stem)
        return Path(*parts) if parts else Path(".")

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
        if nxt >= len(self._pv_frames):
            self._pv_pause()
            return
        self._pv_current = nxt
        self._pv_render()
        if self._pv_canvas and self._pv_canvas.winfo_exists():
            f = self._pv_frames[min(nxt, len(self._pv_frames) - 1)]
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
        self._pv_tick()

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
