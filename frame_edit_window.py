"""
FrameEditWindow — per-frame rotate / flip / perspective warp / erase tool.

Usage (from sprite_gui.py or compose_window.py):
    FrameEditWindow(root, frame_path, on_save)

`on_save(result: PIL.Image, replace: bool, hitboxes: list)` is called when the
user confirms.  replace=True → overwrite the source frame;
replace=False → append as a new frame.
"""

import math
import tkinter as tk
from tkinter import messagebox
from pathlib import Path

import numpy as np
from PIL import Image, ImageTk

from constants import (
    BG, BG_PANEL, BG_CARD, BG_SEL,
    FG, FG_DIM, ACCENT, RED, GREEN, YELLOW,
)
from dialogs import _InputDialog


# ── tooltip ───────────────────────────────────────────────────────────────────

class _Tooltip:
    """Lightweight hover tooltip that appears to the right of the target widget."""

    _DELAY = 500   # ms before showing

    def __init__(self, widget: tk.Widget, text: str):
        self._widget   = widget
        self._text     = text
        self._after_id = None
        self._win: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide,     add="+")

    def _schedule(self, _=None):
        self._hide()
        self._after_id = self._widget.after(self._DELAY, self._show)

    def _hide(self, _=None):
        if self._after_id:
            self._widget.after_cancel(self._after_id)
            self._after_id = None
        if self._win:
            self._win.destroy()
            self._win = None

    def _show(self):
        x = self._widget.winfo_rootx() + self._widget.winfo_width() + 6
        y = self._widget.winfo_rooty() + self._widget.winfo_height() // 2 - 10
        self._win = tk.Toplevel(self._widget)
        self._win.wm_overrideredirect(True)
        self._win.wm_geometry(f"+{x}+{y}")
        tk.Label(self._win, text=self._text,
                 bg="#2a2a3e", fg="#ffffff",
                 font=("", 8), padx=6, pady=3,
                 relief=tk.FLAT).pack()


# ── perspective helpers ───────────────────────────────────────────────────────

def _perspective_coeffs(src_pts, dst_pts):
    """
    Compute 8 PIL PERSPECTIVE coefficients that map *dst* coords → *src* coords.
    src_pts / dst_pts: [(x0,y0), (x1,y1), (x2,y2), (x3,y3)]
    """
    A, rhs = [], []
    for (sx, sy), (dx, dy) in zip(src_pts, dst_pts):
        A.append([dx, dy, 1, 0,  0,  0, -dx * sx, -dy * sx])
        A.append([0,  0,  0, dx, dy, 1, -dx * sy, -dy * sy])
        rhs.extend([sx, sy])
    coeffs, *_ = np.linalg.lstsq(
        np.array(A, dtype=np.float64),
        np.array(rhs, dtype=np.float64),
        rcond=None)
    return coeffs.tolist()


# ── FrameEditWindow ───────────────────────────────────────────────────────────

class FrameEditWindow:
    """Interactive rotate / flip / corner-warp / erase / hitbox editor."""

    _HANDLE_R   = 7
    _HB_R       = 6
    _CANVAS_W   = 460
    _CANVAS_H   = 380
    _PAD        = 60
    _HB_PANEL_W = 160
    _TOOLBAR_W  = 46

    _HANDLE_COLORS = (ACCENT, GREEN, YELLOW, RED)
    _HB_PALETTE    = (ACCENT, GREEN, YELLOW, RED, "#ff88ff", "#00ccff")

    _TOOL_NAMES = {
        "warp":    "Warp",
        "erase":   "Erase",
        "pencil":  "Draw",
        "dropper": "Pick Color",
        "clip":    "Clip",
        "crop":    "Crop",
        "select":  "Select",
        "rotate":  "Rotate",
    }

    _PALETTE = [
        ("#ffffff", "White"),  ("#ff3333", "Red"),    ("#33dd33", "Green"),
        ("#3399ff", "Blue"),   ("#ffff33", "Yellow"), ("#ff33ff", "Magenta"),
        ("#33ffff", "Cyan"),   ("#ff8833", "Orange"), ("#888888", "Gray"),
        ("#000000", "Black"),  ("#ffaacc", "Pink"),   ("#88ccff", "Sky"),
    ]

    # ── init ─────────────────────────────────────────────────────────────────

    def __init__(self, parent: tk.Tk, frame_path: Path, on_save,
                 frame_meta: dict | None = None,
                 palette: list | None = None,
                 frame_list: list | None = None,
                 frame_index: int = 0,
                 get_frame_data=None):
        self._frame_path    = frame_path
        self._on_save       = on_save
        self._palette       = palette if palette else self._PALETTE
        self._frame_list    = frame_list    # list[Path] — all frames in animation
        self._frame_index   = frame_index   # index of current frame in frame_list
        self._get_frame_data = get_frame_data  # callable(idx) -> (path, meta, on_save)

        self._win = tk.Toplevel(parent)
        self._win.configure(bg=BG)
        self._win.geometry("980x740")
        self._win.resizable(True, True)
        self._win.minsize(700, 560)

        self._src = Image.open(frame_path).convert("RGBA")

        # active tool: None | "warp" | "erase" | "pencil" | "clip" | "crop"
        self._active_tool: str | None = None

        # pencil
        self._pencil_mode  = False
        self._pencil_color = "#ffffff"

        # transform
        self._flip_h   = False
        self._flip_v   = False
        self._rotation = tk.DoubleVar(value=0.0)
        self._corners: list[list[float]] = []
        self._drag_idx: int | None = None

        # display geometry
        self._scale    = 1.0
        self._origin_x = 0.0
        self._origin_y = 0.0
        self._res_canvas_x = 0.0
        self._res_canvas_y = 0.0
        self._res_disp_w   = 1
        self._res_disp_h   = 1
        self._res_img_w    = 1
        self._res_img_h    = 1

        # erase
        self._erase_mode    = False
        self._erased_result: Image.Image | None = None
        self._eraser_radius = tk.IntVar(value=1)

        # clip
        self._clip_mode  = False
        self._clip_start: tuple[int, int] | None = None
        self._clip_cur:   tuple[int, int] | None = None

        # crop
        self._crop_mode  = False
        self._crop_start: tuple[int, int] | None = None
        self._crop_cur:   tuple[int, int] | None = None

        # select
        self._sel_state: str | None = None   # None | "drawing" | "floating"
        self._sel_draw_start: tuple | None = None
        self._sel_draw_cur:   tuple | None = None
        self._sel_img:   Image.Image | None = None   # lifted sub-image
        self._sel_base:  Image.Image | None = None   # base with transparent hole
        self._sel_orig:  Image.Image | None = None   # original before lift
        self._sel_cx:    float = 0.0   # center in image coords
        self._sel_cy:    float = 0.0
        self._sel_hw:    float = 0.0   # half-width
        self._sel_hh:    float = 0.0   # half-height
        self._sel_angle: float = 0.0   # CW rotation degrees
        self._sel_drag_op:   str | None = None
        self._sel_drag_sx:   float = 0.0
        self._sel_drag_sy:   float = 0.0
        self._sel_drag_orig: tuple | None = None   # (cx,cy,hw,hh,angle)

        # rotate tool
        self._rot_drag_active:      bool  = False
        self._rot_drag_img_cx:      float = 0.0
        self._rot_drag_img_cy:      float = 0.0
        self._rot_drag_start_rot:   float = 0.0
        self._rot_drag_start_angle: float = 0.0

        # hitboxes
        raw = list((frame_meta or {}).get("hitboxes", []))
        for i, hb in enumerate(raw):
            if "name" not in hb:
                hb["name"] = f"hitbox {i + 1}"
        self._hitboxes: list[dict] = raw
        self._selected_hb: int | None = None
        self._hb_visible    = True
        self._hb_draw_mode  = False
        self._hb_draw_start: tuple[int, int] | None = None
        self._hb_draw_cur:   tuple[int, int] | None = None
        self._hb_drag_op:   str  | None = None
        self._hb_drag_sx    = 0
        self._hb_drag_sy    = 0
        self._hb_drag_orig: dict | None = None

        # undo — each entry: (erased_result_copy | None, hitboxes_copy, corners_copy)
        self._undo_stack: list[tuple] = []

        # widget refs
        self._photo:           ImageTk.PhotoImage | None = None
        self._warp_btn:        tk.Button | None = None
        self._erase_btn:       tk.Button | None = None
        self._pencil_btn:      tk.Button | None = None
        self._clip_btn:        tk.Button | None = None
        self._crop_btn:        tk.Button | None = None
        self._sel_btn:         tk.Button | None = None
        self._dropper_btn:     tk.Button | None = None
        self._rotate_btn:      tk.Button | None = None
        self._nav_left_btn:    tk.Button | None = None
        self._nav_right_btn:   tk.Button | None = None
        self._color_swatch:    tk.Frame  | None = None
        self._hb_add_btn:      tk.Button | None = None
        self._hb_hide_btn:     tk.Button | None = None
        self._hb_listbox:      tk.Listbox | None = None
        self._rot_lbl:         tk.Label  | None = None
        self._eraser_size_lbl: tk.Label  | None = None

        self._build_ui()
        self._reset_corners()
        self._win.bind("<Control-z>", lambda _e: self._undo())
        self._win.bind("<Escape>", lambda _e: self._sel_cancel())
        self._win.bind("<Return>", lambda _e: self._sel_commit())
        # Single-letter hotkeys
        self._win.bind("<Key-s>", lambda _e: self._set_tool("select"),  add="+")
        self._win.bind("<Key-w>", lambda _e: self._set_tool("warp"),    add="+")
        self._win.bind("<Key-e>", lambda _e: self._set_tool("erase"),   add="+")
        self._win.bind("<Key-d>", lambda _e: self._set_tool("pencil"),  add="+")
        self._win.bind("<Key-i>", lambda _e: self._set_tool("dropper"), add="+")
        self._win.bind("<Key-c>", lambda _e: self._set_tool("clip"),    add="+")
        self._win.bind("<Key-p>", lambda _e: self._set_tool("crop"),    add="+")
        self._win.bind("<Key-r>", lambda _e: self._set_tool("rotate"),  add="+")
        self._win.bind("<question>", lambda _e: self._show_help(),      add="+")
        self._win.grab_set()
        self._update_title()

    # ── title ─────────────────────────────────────────────────────────────────

    def _update_title(self):
        base = f"Edit Frame  —  {self._frame_path.name}"
        if self._active_tool:
            label = self._TOOL_NAMES.get(self._active_tool, self._active_tool)
            self._win.title(f"{base}  [{label}]")
        elif self._hb_draw_mode:
            self._win.title(f"{base}  [Add Hitbox]")
        else:
            self._win.title(base)

    # ── base / result image ───────────────────────────────────────────────────

    def _get_base(self) -> Image.Image:
        img = self._src.copy()
        if self._flip_h:
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        if self._flip_v:
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
        angle = self._rotation.get()
        if angle:
            img = img.rotate(-angle, expand=True, resample=Image.BICUBIC)
        return img

    def _reset_corners(self):
        base = self._get_base()
        W, H = base.size
        self._corners = [
            [0.0,      0.0     ],
            [float(W), 0.0     ],
            [float(W), float(H)],
            [0.0,      float(H)],
        ]
        self._erased_result = None
        self._render()

    def _build_result(self) -> Image.Image:
        base = self._get_base()
        W, H = base.size
        nat = [[0., 0.], [float(W), 0.], [float(W), float(H)], [0., float(H)]]
        if self._corners_match(nat):
            return base
        xs = [p[0] for p in self._corners]
        ys = [p[1] for p in self._corners]
        min_x, min_y = min(xs), min(ys)
        out_w = max(1, round(max(xs) - min_x))
        out_h = max(1, round(max(ys) - min_y))
        dst = [(p[0]-min_x, p[1]-min_y) for p in self._corners]
        src = [(0.,0.), (float(W),0.), (float(W),float(H)), (0.,float(H))]
        return base.transform((out_w, out_h), Image.PERSPECTIVE,
                              _perspective_coeffs(src, dst), Image.BICUBIC)

    def _get_final_image(self) -> Image.Image:
        return self._erased_result if self._erased_result is not None \
               else self._build_result()

    def _corners_match(self, nat):
        return all(abs(a[0]-b[0]) < 0.5 and abs(a[1]-b[1]) < 0.5
                   for a, b in zip(self._corners, nat))

    # ── coordinate helpers ────────────────────────────────────────────────────

    def _update_display_params(self):
        cw = self._canvas.winfo_width()  or self._CANVAS_W
        ch = self._canvas.winfo_height() or self._CANVAS_H
        xs = [p[0] for p in self._corners]
        ys = [p[1] for p in self._corners]
        bbox_w = max(max(xs)-min(xs), 1)
        bbox_h = max(max(ys)-min(ys), 1)
        pad    = self._PAD
        self._scale    = max(min((cw-pad*2)/bbox_w, (ch-pad*2)/bbox_h, 8.0), 0.05)
        cx_mid = (min(xs)+max(xs)) / 2
        cy_mid = (min(ys)+max(ys)) / 2
        self._origin_x = cw/2 - cx_mid*self._scale
        self._origin_y = ch/2 - cy_mid*self._scale

    def _img_to_canvas(self, ix, iy):
        return (self._origin_x + ix*self._scale,
                self._origin_y + iy*self._scale)

    def _canvas_to_img(self, cx, cy):
        s = self._scale
        return ((cx-self._origin_x)/s, (cy-self._origin_y)/s)

    def _canvas_to_result(self, cx, cy):
        rx = (cx-self._res_canvas_x)*self._res_img_w / max(self._res_disp_w, 1)
        ry = (cy-self._res_canvas_y)*self._res_img_h / max(self._res_disp_h, 1)
        return int(rx), int(ry)

    def _result_to_canvas(self, rx, ry):
        cx = self._res_canvas_x + rx*self._res_disp_w / max(self._res_img_w, 1)
        cy = self._res_canvas_y + ry*self._res_disp_h / max(self._res_img_h, 1)
        return cx, cy

    def _canvas_delta_to_result(self, dcx, dcy):
        return (dcx*self._res_img_w / max(self._res_disp_w, 1),
                dcy*self._res_img_h / max(self._res_disp_h, 1))

    # ── rendering ────────────────────────────────────────────────────────────

    def _render(self):
        if not self._corners:
            return
        self._update_display_params()
        try:
            result = self._get_final_image()
        except Exception:
            return

        # When selection is floating, composite it over the base before display
        if self._sel_state == "floating" and self._sel_img is not None:
            try:
                result = self._sel_composite()
            except Exception:
                pass

        canvas = self._canvas
        cpts   = [self._img_to_canvas(p[0], p[1]) for p in self._corners]
        xs_c   = [p[0] for p in cpts]
        ys_c   = [p[1] for p in cpts]
        min_cx, min_cy = min(xs_c), min(ys_c)
        disp_w = max(1, round(max(xs_c)-min_cx))
        disp_h = max(1, round(max(ys_c)-min_cy))

        self._res_canvas_x, self._res_canvas_y = min_cx, min_cy
        self._res_disp_w, self._res_disp_h     = disp_w, disp_h
        self._res_img_w, self._res_img_h       = result.width, result.height

        rgb  = tuple(int(BG_CARD.lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
        bg   = Image.new("RGBA", result.size, (*rgb, 255))
        flat = Image.alpha_composite(bg, result.convert("RGBA"))
        flat = flat.resize((disp_w, disp_h), Image.NEAREST)
        self._photo = ImageTk.PhotoImage(flat)

        canvas.delete("all")

        # Checkerboard
        sz = 10
        for row in range(0, disp_h, sz):
            for col in range(0, disp_w, sz):
                col_c = "#1e1e2e" if (row//sz + col//sz) % 2 == 0 else "#2a2a3e"
                canvas.create_rectangle(
                    int(min_cx)+col, int(min_cy)+row,
                    int(min_cx)+col+sz, int(min_cy)+row+sz,
                    fill=col_c, outline="")

        canvas.create_image(int(min_cx), int(min_cy),
                            image=self._photo, anchor=tk.NW)

        # Warp handles — only visible when warp tool is active
        if self._active_tool == "warp":
            for i in range(4):
                ax, ay = cpts[i]; bx, by = cpts[(i+1) % 4]
                canvas.create_line(ax, ay, bx, by,
                                   fill="#555577", width=1, dash=(4, 3))
            r = self._HANDLE_R
            for i, (cx_h, cy_h) in enumerate(cpts):
                canvas.create_oval(cx_h-r, cy_h-r, cx_h+r, cy_h+r,
                                   fill=self._HANDLE_COLORS[i],
                                   outline="#ffffff", width=1.5)

        # Rotate handle — only visible when rotate tool is active
        if self._active_tool == "rotate":
            img_cx = sum(p[0] for p in cpts) / 4
            img_cy = sum(p[1] for p in cpts) / 4
            a  = math.radians(self._rotation.get())
            R  = 55
            rhx = img_cx - R * math.sin(a)
            rhy = img_cy - R * math.cos(a)
            canvas.create_line(img_cx, img_cy, rhx, rhy,
                               fill=GREEN, width=1.5, tags="rot_handle")
            canvas.create_oval(rhx-8, rhy-8, rhx+8, rhy+8,
                               fill=BG_CARD, outline=GREEN, width=2,
                               tags="rot_handle")
            canvas.create_text(img_cx, img_cy + 14,
                               text=f"{self._rotation.get():.0f}°",
                               fill=GREEN, font=("Consolas", 9),
                               tags="rot_handle")

        # Hitboxes
        if self._hb_visible:
            for i, hb in enumerate(self._hitboxes):
                x0c, y0c = self._result_to_canvas(hb["x"], hb["y"])
                x1c, y1c = self._result_to_canvas(hb["x"]+hb["w"], hb["y"]+hb["h"])
                col      = self._HB_PALETTE[i % len(self._HB_PALETTE)]
                selected = i == self._selected_hb
                canvas.create_rectangle(
                    x0c, y0c, x1c, y1c,
                    outline="#ffffff" if selected else col,
                    width=2.5 if selected else 1.5,
                    fill=col, stipple="gray25" if selected else "gray12",
                    tags="hitboxes")
                canvas.create_text(
                    x0c+3, y0c+2, text=hb["name"], anchor=tk.NW,
                    fill="#ffffff" if selected else col,
                    font=("", 7), tags="hitboxes")
                if selected:
                    r = self._HB_R
                    for hx, hy in [(x0c,y0c),(x1c,y0c),(x1c,y1c),(x0c,y1c)]:
                        canvas.create_rectangle(
                            hx-r, hy-r, hx+r, hy+r,
                            fill=BG_CARD, outline="#ffffff", width=1.5,
                            tags="hb_handles")

            if self._hb_draw_mode and self._hb_draw_start and self._hb_draw_cur:
                x0, y0 = self._hb_draw_start; x1, y1 = self._hb_draw_cur
                canvas.create_rectangle(
                    min(x0,x1), min(y0,y1), max(x0,x1), max(y0,y1),
                    outline=ACCENT, width=2, dash=(4,2), fill="", tags="hb_draw")

        # Clip preview — dim INSIDE (region to be removed)
        if self._clip_mode and self._clip_start and self._clip_cur:
            x0c = min(self._clip_start[0], self._clip_cur[0])
            y0c = min(self._clip_start[1], self._clip_cur[1])
            x1c = max(self._clip_start[0], self._clip_cur[0])
            y1c = max(self._clip_start[1], self._clip_cur[1])
            canvas.create_rectangle(x0c, y0c, x1c, y1c,
                                    fill="#000000", stipple="gray25",
                                    outline="", tags="clip_overlay")
            canvas.create_rectangle(x0c, y0c, x1c, y1c,
                                    outline=YELLOW, width=1.5, dash=(4,2),
                                    fill="", tags="clip_overlay")

        # Crop preview — dim OUTSIDE (region to be kept)
        if self._crop_mode and self._crop_start and self._crop_cur:
            x0c = min(self._crop_start[0], self._crop_cur[0])
            y0c = min(self._crop_start[1], self._crop_cur[1])
            x1c = max(self._crop_start[0], self._crop_cur[0])
            y1c = max(self._crop_start[1], self._crop_cur[1])
            cw_ = canvas.winfo_width()  or self._CANVAS_W
            ch_ = canvas.winfo_height() or self._CANVAS_H
            for bx0, by0, bx1, by1 in [
                (0, 0, cw_, y0c), (0, y1c, cw_, ch_),
                (0, y0c, x0c, y1c), (x1c, y0c, cw_, y1c),
            ]:
                canvas.create_rectangle(bx0, by0, bx1, by1,
                                        fill="#000000", stipple="gray25",
                                        outline="", tags="crop_overlay")
            canvas.create_rectangle(x0c, y0c, x1c, y1c,
                                    outline=ACCENT, width=1.5, dash=(4,2),
                                    fill="", tags="crop_overlay")

        # Selection overlay
        self._sel_render_overlay(canvas)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── top row: toolbar | canvas | hitbox panel ──────────────────────
        top = tk.Frame(self._win, bg=BG)
        top.pack(fill=tk.BOTH, expand=True, padx=0, pady=(4, 0))

        # Right panel first so canvas gets remaining space
        hbp = tk.Frame(top, bg=BG_CARD, width=self._HB_PANEL_W)
        hbp.pack(side=tk.RIGHT, fill=tk.Y, padx=(4, 6))
        hbp.pack_propagate(False)
        self._build_hb_panel(hbp)

        # Left toolbar
        tb = tk.Frame(top, bg=BG_CARD, width=self._TOOLBAR_W)
        tb.pack(side=tk.LEFT, fill=tk.Y, padx=(6, 4))
        tb.pack_propagate(False)
        self._build_toolbar(tb)

        # Canvas + navigation arrows (grid so arrows can be shown/hidden cleanly)
        nav = tk.Frame(top, bg=BG)
        nav.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        nav.columnconfigure(1, weight=1)
        nav.rowconfigure(0, weight=1)

        nav_btn_kw = dict(bg=BG_PANEL, fg=FG_DIM,
                          activeforeground=FG, activebackground=BG_SEL,
                          relief=tk.FLAT, font=("Segoe UI Symbol", 28),
                          width=2, cursor="hand2",
                          borderwidth=0, highlightthickness=0)
        self._nav_left_btn = tk.Button(nav, text="◀",
                                       command=lambda: self._nav_go(-1),
                                       **nav_btn_kw)
        self._nav_left_btn.grid(row=0, column=0, sticky="ns")

        self._canvas = tk.Canvas(nav,
                                  width=self._CANVAS_W, height=self._CANVAS_H,
                                  bg=BG_PANEL, highlightthickness=0,
                                  cursor="crosshair")
        self._canvas.grid(row=0, column=1, sticky="nsew")

        self._nav_right_btn = tk.Button(nav, text="▶",
                                        command=lambda: self._nav_go(+1),
                                        **nav_btn_kw)
        self._nav_right_btn.grid(row=0, column=2, sticky="ns")

        self._update_nav_buttons()

        self._canvas.bind("<Configure>",       lambda _: self._render())
        self._canvas.bind("<ButtonPress-1>",   self._on_press)
        self._canvas.bind("<B1-Motion>",       self._on_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)
        self._canvas.bind("<Motion>",          self._on_motion)
        self._canvas.bind("<Leave>",           self._on_leave)

        # ── bottom controls ────────────────────────────────────────────────
        ctrl = tk.Frame(self._win, bg=BG_PANEL)
        ctrl.pack(fill=tk.X, padx=6, pady=(4, 0))

        # Rotation readout (no slider — use the Rotate tool handle on canvas)
        rot_row = tk.Frame(ctrl, bg=BG_PANEL)
        rot_row.pack(fill=tk.X, pady=1)
        tk.Label(rot_row, text="↻", bg=BG_PANEL, fg=FG_DIM,
                 font=("Segoe UI Symbol", 12)).pack(side=tk.LEFT, padx=(4, 2))
        tk.Label(rot_row, text="rotation  (use R tool to adjust)",
                 bg=BG_PANEL, fg=FG_DIM, font=("", 8),
                 anchor=tk.W).pack(side=tk.LEFT, padx=4)
        self._rot_lbl = tk.Label(rot_row, text="0°", bg=BG_PANEL,
                                  fg=FG, font=("Consolas", 9), width=4, anchor=tk.E)
        self._rot_lbl.pack(side=tk.RIGHT, padx=4)

        # Eraser size row
        sz_row = tk.Frame(ctrl, bg=BG_PANEL)
        sz_row.pack(fill=tk.X, pady=1)
        tk.Label(sz_row, text="✏", bg=BG_PANEL, fg=FG_DIM,
                 font=("Segoe UI Symbol", 12)).pack(side=tk.LEFT, padx=(4, 2))
        self._eraser_size_lbl = tk.Label(sz_row, text="1px", bg=BG_PANEL,
                                          fg=FG, font=("Consolas", 9),
                                          width=4, anchor=tk.E)
        self._eraser_size_lbl.pack(side=tk.RIGHT, padx=4)
        tk.Scale(sz_row, variable=self._eraser_radius,
                 from_=1, to=60, resolution=1,
                 orient=tk.HORIZONTAL, showvalue=False,
                 command=self._on_eraser_size_change,
                 bg=BG_PANEL, fg=FG, troughcolor=BG_CARD,
                 highlightthickness=0, relief=tk.FLAT,
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)

        # ── save row ──────────────────────────────────────────────────────
        bot = tk.Frame(self._win, bg=BG_PANEL, pady=4)
        bot.pack(fill=tk.X, padx=6, pady=(2, 6))
        self._btn(bot, "Cancel",            self._win.destroy,  FG_DIM
                  ).pack(side=tk.LEFT)
        self._btn(bot, "Replace Frame",     self._save_replace, ACCENT
                  ).pack(side=tk.RIGHT, padx=(4, 0))
        self._btn(bot, "Save as New Frame", self._save_new,     GREEN
                  ).pack(side=tk.RIGHT, padx=4)

    def _build_toolbar(self, tb):
        """Populate the left icon toolbar."""
        def tbtn(icon, cmd, color=FG_DIM, tip=""):
            b = tk.Button(tb, text=icon, command=cmd,
                          bg=BG_CARD, fg=color,
                          activeforeground=FG, activebackground=BG_SEL,
                          relief=tk.FLAT, font=("Segoe UI Symbol", 14),
                          width=2, pady=7, cursor="hand2",
                          borderwidth=0, highlightthickness=0)
            b.pack(fill=tk.X)
            if tip:
                _Tooltip(b, tip)
            return b

        def sep():
            tk.Frame(tb, bg=BG_SEL, height=1).pack(fill=tk.X, padx=6, pady=4)

        # Transform actions (instant, not mode-gated)
        tbtn("↔", self._do_flip_h, ACCENT, "Flip Horizontal")
        tbtn("↕", self._do_flip_v, ACCENT, "Flip Vertical")
        tbtn("↺", self._do_reset,  RED,    "Reset All")
        sep()
        # Tool modes (toggle on/off)
        self._sel_btn      = tbtn("▦", self._toggle_select,  FG_DIM, "Select  [S]\nDraw rectangle; then drag to move,\ndrag corners to resize, drag circle to rotate\nEnter to commit  ·  Esc to cancel")
        self._rotate_btn   = tbtn("↻", self._toggle_rotate,  FG_DIM, "Rotate  [R]\nDrag the green handle to rotate image")
        self._warp_btn     = tbtn("⤡", self._toggle_warp,    FG_DIM, "Warp / Skew  [W]")
        self._erase_btn    = tbtn("✏", self._toggle_erase,   FG_DIM, "Erase  [E]")
        self._pencil_btn   = tbtn("✒", self._toggle_pencil,  FG_DIM, "Draw  [D]")
        self._dropper_btn  = tbtn("⊙", self._toggle_dropper, FG_DIM, "Pick Color  [I]\nClick a pixel to sample its color\nas the active draw color")
        self._clip_btn     = tbtn("✂", self._toggle_clip,    FG_DIM, "Clip  [C]")
        self._crop_btn     = tbtn("⊡", self._toggle_crop,    FG_DIM, "Crop  [P]")
        sep()
        tbtn("?", self._show_help, FG_DIM, "Keyboard shortcuts  [?]")
        sep()
        self._build_palette(tb)

    def _build_hb_panel(self, hbp):
        """Populate the right hitbox panel."""
        tk.Label(hbp, text="HITBOXES", bg=BG_CARD, fg=FG_DIM,
                 font=("", 8, "bold"), anchor=tk.W
                 ).pack(fill=tk.X, padx=6, pady=(6, 2))

        list_frame = tk.Frame(hbp, bg=BG_CARD)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=2)
        sb = tk.Scrollbar(list_frame, orient=tk.VERTICAL, bg=BG_CARD,
                          troughcolor=BG_PANEL, relief=tk.FLAT, width=10)
        self._hb_listbox = tk.Listbox(
            list_frame, yscrollcommand=sb.set,
            bg=BG_PANEL, fg=FG,
            selectbackground=BG_SEL, selectforeground=FG,
            font=("", 9), relief=tk.FLAT,
            borderwidth=0, highlightthickness=0,
            activestyle="none", selectmode=tk.EXTENDED)
        sb.config(command=self._hb_listbox.yview)
        self._hb_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._hb_listbox.bind("<<ListboxSelect>>", self._on_hb_list_select)
        self._hb_listbox.bind("<Button-3>",        self._hb_context_menu)

        row1 = tk.Frame(hbp, bg=BG_CARD)
        row1.pack(fill=tk.X, padx=6, pady=(2, 6))
        self._hb_add_btn = tk.Button(
            row1, text="+ Add", command=self._hb_toggle_draw,
            bg=BG_PANEL, fg=FG_DIM,
            activeforeground=FG_DIM, activebackground=BG_SEL,
            relief=tk.FLAT, font=("", 9), padx=6, pady=3,
            cursor="hand2", borderwidth=0, highlightthickness=0)
        self._hb_add_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        self._hb_hide_btn = tk.Button(
            row1, text="Hide All", command=self._hb_toggle_visible,
            bg=BG_PANEL, fg=FG_DIM,
            activeforeground=FG_DIM, activebackground=BG_SEL,
            relief=tk.FLAT, font=("", 9), padx=6, pady=3,
            cursor="hand2", borderwidth=0, highlightthickness=0)
        self._hb_hide_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._hb_sync_list()

    def _build_palette(self, tb):
        """Compact color palette at the bottom of the left toolbar."""
        tk.Label(tb, text="PAINT", bg=BG_CARD, fg=FG_DIM,
                 font=("", 6, "bold")).pack(fill=tk.X, padx=3, pady=(2, 1))

        # Current-color swatch — click to open custom color picker
        swatch_frame = tk.Frame(tb, bg=BG_CARD)
        swatch_frame.pack(pady=(0, 3))
        self._color_swatch = tk.Frame(
            swatch_frame, bg=self._pencil_color,
            width=28, height=14, cursor="hand2",
            highlightthickness=1, highlightbackground=FG_DIM)
        self._color_swatch.pack()
        self._color_swatch.bind("<Button-1>", lambda _e: self._pick_custom_color())
        _Tooltip(self._color_swatch, "Current draw color\n(click for custom)")

        # Preset swatches: 4 per row (fits up to 16 project colors in 4 rows)
        for row_start in range(0, len(self._palette), 4):
            row = tk.Frame(tb, bg=BG_CARD)
            row.pack(pady=1)
            for color, name in self._palette[row_start:row_start + 4]:
                sw = tk.Frame(row, bg=color, width=9, height=9,
                              cursor="hand2",
                              highlightthickness=1,
                              highlightbackground=BG_SEL)
                sw.pack(side=tk.LEFT, padx=1)
                sw.bind("<Button-1>", lambda _e, c=color: self._set_pencil_color(c))
                _Tooltip(sw, name)

    def _set_pencil_color(self, color: str):
        self._pencil_color = color
        if self._color_swatch:
            self._color_swatch.config(bg=color)

    def _pick_custom_color(self):
        from tkinter import colorchooser
        result = colorchooser.askcolor(
            color=self._pencil_color, parent=self._win, title="Pick Draw Color")
        if result and result[1]:
            self._set_pencil_color(result[1])

    def _btn(self, parent, text, cmd, color=FG_DIM, small=False):
        return tk.Button(parent, text=text, command=cmd,
                         bg=BG_CARD, fg=color,
                         activeforeground=color, activebackground=BG_SEL,
                         relief=tk.FLAT, font=("", 8 if small else 9),
                         padx=8, pady=3, cursor="hand2",
                         borderwidth=0, highlightthickness=0)

    # ── tool management ───────────────────────────────────────────────────────

    def _set_tool(self, name: str | None):
        """Activate a named tool, toggling off if already active."""
        if self._active_tool == name:
            name = None   # clicking the active tool deactivates it

        # Commit any floating selection when switching away from select
        if self._active_tool == "select" and name != "select":
            self._sel_commit()

        # Clear all mode flags and visual state
        self._erase_mode     = False
        self._pencil_mode    = False
        self._clip_mode      = False
        self._crop_mode      = False
        self._rot_drag_active = False
        self._clip_start  = self._clip_cur = None
        self._crop_start  = self._crop_cur = None
        self._canvas.delete("eraser_cursor")
        self._canvas.delete("pencil_cursor")
        self._canvas.config(cursor="crosshair")

        # Cancel hitbox draw mode too
        if self._hb_draw_mode:
            self._hb_draw_mode = False
            if self._hb_add_btn:
                self._hb_add_btn.config(fg=FG_DIM, bg=BG_PANEL)
            self._hb_draw_start = self._hb_draw_cur = None

        # Reset all toolbar button styles
        for btn in (self._sel_btn, self._rotate_btn, self._warp_btn,
                    self._erase_btn, self._pencil_btn, self._dropper_btn,
                    self._clip_btn, self._crop_btn):
            if btn:
                btn.config(fg=FG_DIM, bg=BG_CARD)

        self._active_tool = name

        # Activate the selected tool
        if name == "select":
            self._sel_btn.config(fg=ACCENT, bg=BG_SEL)
            self._sel_state = None
            self._sel_draw_start = self._sel_draw_cur = None
        elif name == "rotate":
            self._rotate_btn.config(fg=GREEN, bg=BG_SEL)
        elif name == "warp":
            self._warp_btn.config(fg=ACCENT, bg=BG_SEL)
        elif name == "erase":
            self._erase_mode = True
            self._erase_btn.config(fg=YELLOW, bg=BG_SEL)
            self._canvas.config(cursor="none")
        elif name == "pencil":
            self._pencil_mode = True
            self._pencil_btn.config(fg=ACCENT, bg=BG_SEL)
            self._canvas.config(cursor="none")
        elif name == "dropper":
            self._dropper_btn.config(fg=YELLOW, bg=BG_SEL)
            self._canvas.config(cursor="crosshair")
        elif name == "clip":
            self._clip_mode = True
            self._clip_btn.config(fg=YELLOW, bg=BG_SEL)
        elif name == "crop":
            self._crop_mode = True
            self._crop_btn.config(fg=ACCENT, bg=BG_SEL)

        self._update_title()
        self._render()

    def _toggle_select(self):  self._set_tool("select")
    def _toggle_rotate(self):  self._set_tool("rotate")
    def _toggle_dropper(self): self._set_tool("dropper")
    def _toggle_warp(self):    self._set_tool("warp")
    def _toggle_erase(self):  self._set_tool("erase")
    def _toggle_pencil(self): self._set_tool("pencil")
    def _toggle_clip(self):   self._set_tool("clip")
    def _toggle_crop(self):   self._set_tool("crop")

    # ── undo ─────────────────────────────────────────────────────────────────

    def _push_undo(self):
        snap_img     = self._erased_result.copy() if self._erased_result else None
        snap_hb      = [dict(h) for h in self._hitboxes]
        snap_corners = [list(c) for c in self._corners]
        self._undo_stack.append((snap_img, snap_hb, snap_corners))
        if len(self._undo_stack) > 30:
            self._undo_stack.pop(0)

    def _undo(self):
        if not self._undo_stack:
            return
        snap_img, snap_hb, snap_corners = self._undo_stack.pop()
        self._erased_result = snap_img
        self._hitboxes      = snap_hb
        self._corners       = snap_corners
        self._selected_hb   = None
        self._hb_sync_list()
        self._render()

    # ── transform callbacks ───────────────────────────────────────────────────

    def _on_rotation_change(self, _=None):
        self._rot_lbl.config(text=f"{self._rotation.get():.0f}°")
        self._reset_corners()

    def _do_flip_h(self):
        self._flip_h = not self._flip_h
        self._reset_corners()

    def _do_flip_v(self):
        self._flip_v = not self._flip_v
        self._reset_corners()

    def _do_reset(self):
        self._flip_h = False
        self._flip_v = False
        self._rotation.set(0.0)
        self._rot_lbl.config(text="0°")
        self._reset_corners()

    def _on_eraser_size_change(self, _=None):
        self._eraser_size_lbl.config(text=f"{self._eraser_radius.get()}px")

    # ── clip / crop apply ─────────────────────────────────────────────────────

    def _apply_clip(self):
        if self._erased_result is None:
            try:
                self._erased_result = self._build_result().copy()
            except Exception:
                return
        rx0, ry0 = self._canvas_to_result(*self._clip_start)
        rx1, ry1 = self._canvas_to_result(*self._clip_cur)
        x0 = max(0, min(rx0, rx1));  x1 = min(self._res_img_w, max(rx0, rx1))
        y0 = max(0, min(ry0, ry1));  y1 = min(self._res_img_h, max(ry0, ry1))
        if x1 > x0 and y1 > y0:
            arr = np.array(self._erased_result)
            arr[y0:y1, x0:x1, 3] = 0
            self._erased_result = Image.fromarray(arr, "RGBA")

    def _apply_crop(self):
        base = self._erased_result if self._erased_result is not None \
               else self._build_result()
        rx0, ry0 = self._canvas_to_result(*self._crop_start)
        rx1, ry1 = self._canvas_to_result(*self._crop_cur)
        iw, ih = base.width, base.height
        x0 = max(0, min(rx0, rx1));  x1 = min(iw, max(rx0, rx1))
        y0 = max(0, min(ry0, ry1));  y1 = min(ih, max(ry0, ry1))
        if x1 <= x0 or y1 <= y0:
            return
        self._erased_result = base.crop((x0, y0, x1, y1))
        new_w, new_h = x1 - x0, y1 - y0

        # Reset corners to match new image dimensions so display doesn't stretch
        self._corners = [
            [0.0,          0.0         ],
            [float(new_w), 0.0         ],
            [float(new_w), float(new_h)],
            [0.0,          float(new_h)],
        ]

        # Adjust hitboxes to new coordinate space; drop any that no longer fit
        kept = []
        for hb in self._hitboxes:
            hx0 = max(0, hb["x"]-x0);  hx1 = min(new_w, hb["x"]+hb["w"]-x0)
            hy0 = max(0, hb["y"]-y0);  hy1 = min(new_h, hb["y"]+hb["h"]-y0)
            if hx1-hx0 >= 2 and hy1-hy0 >= 2:
                kept.append({"name": hb["name"],
                             "x": hx0, "y": hy0, "w": hx1-hx0, "h": hy1-hy0})
        self._hitboxes = kept
        self._selected_hb = None
        self._hb_sync_list()

    # ── hitbox panel callbacks ────────────────────────────────────────────────

    def _hb_toggle_draw(self):
        self._hb_draw_mode = not self._hb_draw_mode
        if self._hb_draw_mode:
            # Deactivate any active toolbar tool first
            if self._active_tool:
                self._set_tool(None)   # _set_tool clears hb_draw_mode, restore it
            self._hb_draw_mode = True
            self._hb_add_btn.config(fg=GREEN, bg=BG_SEL)
        else:
            self._hb_add_btn.config(fg=FG_DIM, bg=BG_PANEL)
            self._hb_draw_start = self._hb_draw_cur = None
        self._update_title()
        self._render()

    def _hb_toggle_visible(self):
        self._hb_visible = not self._hb_visible
        self._hb_hide_btn.config(
            text="Show All" if not self._hb_visible else "Hide All")
        self._render()

    def _hb_context_menu(self, event):
        idx = self._hb_listbox.nearest(event.y)
        if idx < 0 or idx >= len(self._hitboxes):
            return
        if idx not in self._hb_listbox.curselection():
            self._hb_listbox.selection_clear(0, tk.END)
            self._hb_listbox.selection_set(idx)
            self._selected_hb = idx
            self._render()
        n_sel = len(self._hb_listbox.curselection())
        menu = tk.Menu(self._win, tearoff=0,
                       bg=BG_CARD, fg=FG,
                       activebackground=BG_SEL, activeforeground=FG,
                       relief=tk.FLAT, borderwidth=1)
        if n_sel == 1:
            menu.add_command(label="Rename",
                             command=lambda i=idx: self._hb_rename(i))
            menu.add_separator()
        menu.add_command(
            label=f"Delete  ({n_sel})" if n_sel > 1 else "Delete",
            command=self._hb_delete, foreground=RED)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _hb_rename(self, idx: int | None = None):
        if idx is None:
            idx = self._selected_hb
        if idx is None or idx >= len(self._hitboxes):
            return
        hb  = self._hitboxes[idx]
        dlg = _InputDialog(self._win, "Rename Hitbox", "Name:", hb["name"])
        if dlg.result and dlg.result != hb["name"]:
            hb["name"] = dlg.result
            self._hb_sync_list()
            self._render()

    def _hb_delete(self):
        indices = sorted(self._hb_listbox.curselection(), reverse=True)
        if not indices:
            return
        for i in indices:
            if 0 <= i < len(self._hitboxes):
                self._hitboxes.pop(i)
        if self._selected_hb is not None:
            if self._selected_hb in indices:
                self._selected_hb = None
            else:
                shift = sum(1 for i in indices if i < self._selected_hb)
                self._selected_hb -= shift
        self._hb_sync_list()
        self._render()

    def _hb_sync_list(self):
        lb = self._hb_listbox
        lb.delete(0, tk.END)
        for hb in self._hitboxes:
            lb.insert(tk.END, hb["name"])
        if self._selected_hb is not None and self._selected_hb < len(self._hitboxes):
            lb.selection_set(self._selected_hb)
            lb.see(self._selected_hb)

    def _on_hb_list_select(self, _event):
        sel = self._hb_listbox.curselection()
        self._selected_hb = sel[-1] if sel else None
        self._render()

    # ── hitbox geometry helpers ───────────────────────────────────────────────

    def _hb_next_name(self) -> str:
        existing = {hb["name"] for hb in self._hitboxes}
        n = 1
        while f"hitbox {n}" in existing:
            n += 1
        return f"hitbox {n}"

    def _hit_test_hb_handles(self, cx, cy) -> str | None:
        if self._selected_hb is None or self._selected_hb >= len(self._hitboxes):
            return None
        hb = self._hitboxes[self._selected_hb]
        x0c, y0c = self._result_to_canvas(hb["x"], hb["y"])
        x1c, y1c = self._result_to_canvas(hb["x"]+hb["w"], hb["y"]+hb["h"])
        r = self._HB_R + 2
        for op, hx, hy in [("resize_TL",x0c,y0c),("resize_TR",x1c,y0c),
                            ("resize_BR",x1c,y1c),("resize_BL",x0c,y1c)]:
            if abs(cx-hx) <= r and abs(cy-hy) <= r:
                return op
        return None

    def _hit_test_hb(self, cx, cy) -> int | None:
        if not self._hb_visible:
            return None
        for i in range(len(self._hitboxes)-1, -1, -1):
            hb = self._hitboxes[i]
            x0c, y0c = self._result_to_canvas(hb["x"], hb["y"])
            x1c, y1c = self._result_to_canvas(hb["x"]+hb["w"], hb["y"]+hb["h"])
            if x0c <= cx <= x1c and y0c <= cy <= y1c:
                return i
        return None

    def _clamp_hb(self, hb):
        iw, ih = max(self._res_img_w,1), max(self._res_img_h,1)
        x = max(0, min(int(hb["x"]), iw-2));  y = max(0, min(int(hb["y"]), ih-2))
        w = max(2, min(int(hb["w"]), iw-x));   h = max(2, min(int(hb["h"]), ih-y))
        hb["x"], hb["y"], hb["w"], hb["h"] = x, y, w, h

    # ── canvas event handlers ─────────────────────────────────────────────────

    def _on_press(self, event):
        if self._active_tool == "rotate":
            cpts = [self._img_to_canvas(*c) for c in self._corners]
            img_cx = sum(p[0] for p in cpts) / 4
            img_cy = sum(p[1] for p in cpts) / 4
            a  = math.radians(self._rotation.get())
            rhx = img_cx - 55 * math.sin(a)
            rhy = img_cy - 55 * math.cos(a)
            if (event.x - rhx)**2 + (event.y - rhy)**2 <= 144:
                self._rot_drag_active      = True
                self._rot_drag_img_cx      = img_cx
                self._rot_drag_img_cy      = img_cy
                self._rot_drag_start_rot   = self._rotation.get()
                self._rot_drag_start_angle = math.degrees(
                    math.atan2(event.x - img_cx, -(event.y - img_cy)))
            return
        if self._active_tool == "select":
            op = self._sel_hit_test(event.x, event.y)
            if op is not None:
                # Start a drag on the floating selection
                self._sel_drag_op   = op
                self._sel_drag_sx   = event.x
                self._sel_drag_sy   = event.y
                self._sel_drag_orig = (self._sel_cx, self._sel_cy,
                                       self._sel_hw, self._sel_hh,
                                       self._sel_angle)
            elif self._sel_state == "floating":
                # Click outside floating selection → commit it, start new draw
                self._sel_commit()
                self._sel_state      = "drawing"
                self._sel_draw_start = self._sel_draw_cur = (event.x, event.y)
            else:
                # Start drawing a new selection rectangle
                self._sel_state      = "drawing"
                self._sel_draw_start = self._sel_draw_cur = (event.x, event.y)
            return
        if self._active_tool == "dropper":
            self._dropper_pick(event.x, event.y)
            return
        if self._erase_mode:
            self._push_undo()
            self._erase_at(event.x, event.y)
            return
        if self._pencil_mode:
            self._push_undo()
            self._pencil_at(event.x, event.y)
            return
        if self._clip_mode:
            self._clip_start = self._clip_cur = (event.x, event.y)
            return
        if self._crop_mode:
            self._crop_start = self._crop_cur = (event.x, event.y)
            return
        if self._hb_draw_mode:
            self._hb_draw_start = self._hb_draw_cur = (event.x, event.y)
            return
        # Hitbox handles / move (available regardless of tool)
        op = self._hit_test_hb_handles(event.x, event.y)
        if op is not None:
            hb = self._hitboxes[self._selected_hb]
            self._hb_drag_op = op; self._hb_drag_sx = event.x
            self._hb_drag_sy = event.y; self._hb_drag_orig = dict(hb)
            return
        hit = self._hit_test_hb(event.x, event.y)
        if hit is not None:
            self._selected_hb = hit
            self._hb_drag_op  = "move"; self._hb_drag_sx = event.x
            self._hb_drag_sy  = event.y
            self._hb_drag_orig = dict(self._hitboxes[hit])
            self._hb_sync_list(); self._render()
            return
        # Warp corner handles — only active in warp tool mode
        if self._active_tool == "warp":
            r = self._HANDLE_R + 4
            for i, corner in enumerate(self._corners):
                cx_h, cy_h = self._img_to_canvas(*corner)
                if abs(event.x-cx_h) <= r and abs(event.y-cy_h) <= r:
                    self._drag_idx = i
                    return
        self._drag_idx = None

    def _on_drag(self, event):
        if self._active_tool == "rotate":
            if self._rot_drag_active:
                cur_a = math.degrees(math.atan2(
                    event.x - self._rot_drag_img_cx,
                    -(event.y - self._rot_drag_img_cy)))
                new_rot = self._rot_drag_start_rot + (cur_a - self._rot_drag_start_angle)
                while new_rot >  180: new_rot -= 360
                while new_rot < -180: new_rot += 360
                self._rotation.set(round(new_rot))
                self._on_rotation_change()
            return
        if self._active_tool == "select":
            if self._sel_state == "drawing":
                self._sel_draw_cur = (event.x, event.y)
                self._render()
            elif self._sel_drag_op is not None:
                self._sel_apply_drag(event.x, event.y)
                self._render()
            return
        if self._erase_mode:
            self._erase_at(event.x, event.y)
            self._draw_eraser_cursor(event.x, event.y)
            return
        if self._pencil_mode:
            self._pencil_at(event.x, event.y)
            self._draw_pencil_cursor(event.x, event.y)
            return
        if self._clip_mode:
            self._clip_cur = (event.x, event.y); self._render(); return
        if self._crop_mode:
            self._crop_cur = (event.x, event.y); self._render(); return
        if self._hb_draw_mode:
            self._hb_draw_cur = (event.x, event.y); self._render(); return
        if self._hb_drag_op is not None:
            self._apply_hb_drag(event.x, event.y); self._render(); return
        if self._drag_idx is None:
            return
        ix, iy = self._canvas_to_img(event.x, event.y)
        self._corners[self._drag_idx] = [ix, iy]
        self._render()

    def _on_release(self, event):
        if self._active_tool == "rotate":
            self._rot_drag_active = False
            return
        if self._active_tool == "select":
            if self._sel_state == "drawing" and self._sel_draw_start is not None:
                self._sel_draw_cur = (event.x, event.y)
                cx0, cy0 = self._sel_draw_start
                if abs(event.x-cx0) > 4 and abs(event.y-cy0) > 4:
                    self._push_undo()
                    self._sel_lift()
                else:
                    self._sel_state = None
                    self._render()
            elif self._sel_drag_op is not None:
                self._sel_drag_op = None
                self._sel_drag_orig = None
            return
        if self._clip_mode and self._clip_start is not None:
            self._clip_cur = (event.x, event.y)
            cx0, cy0 = self._clip_start; cx1, cy1 = event.x, event.y
            if abs(cx1-cx0) > 3 and abs(cy1-cy0) > 3:
                self._push_undo()
                self._apply_clip()
            self._clip_start = self._clip_cur = None
            self._render(); return

        if self._crop_mode and self._crop_start is not None:
            self._crop_cur = (event.x, event.y)
            cx0, cy0 = self._crop_start; cx1, cy1 = event.x, event.y
            if abs(cx1-cx0) > 3 and abs(cy1-cy0) > 3:
                self._push_undo()
                self._apply_crop()
            self._crop_start = self._crop_cur = None
            self._render(); return

        if self._hb_draw_mode and self._hb_draw_start is not None:
            x0c, y0c = self._hb_draw_start; x1c, y1c = event.x, event.y
            if abs(x1c-x0c) > 5 or abs(y1c-y0c) > 5:
                rx0, ry0 = self._canvas_to_result(x0c, y0c)
                rx1, ry1 = self._canvas_to_result(x1c, y1c)
                w, h = abs(rx1-rx0), abs(ry1-ry0)
                if w >= 2 and h >= 2:
                    new_hb = {"name": self._hb_next_name(),
                              "x": min(rx0,rx1), "y": min(ry0,ry1), "w": w, "h": h}
                    self._clamp_hb(new_hb)
                    self._hitboxes.append(new_hb)
                    self._selected_hb = len(self._hitboxes)-1
                    self._hb_sync_list()
            self._hb_draw_start = self._hb_draw_cur = None
            self._hb_draw_mode = False
            self._hb_add_btn.config(fg=FG_DIM, bg=BG_PANEL)
            self._update_title()
            self._render(); return

        if self._hb_drag_op is not None:
            self._apply_hb_drag(event.x, event.y)
            self._hb_drag_op = None; self._hb_drag_orig = None
            self._render(); return

        self._drag_idx = None

    def _apply_hb_drag(self, cx, cy):
        if self._selected_hb is None or self._hb_drag_orig is None:
            return
        hb   = self._hitboxes[self._selected_hb]
        orig = self._hb_drag_orig
        drx, dry = self._canvas_delta_to_result(cx-self._hb_drag_sx,
                                                cy-self._hb_drag_sy)
        fx2, fy2 = orig["x"]+orig["w"], orig["y"]+orig["h"]
        op = self._hb_drag_op
        if op == "move":
            hb["x"], hb["y"], hb["w"], hb["h"] = \
                orig["x"]+drx, orig["y"]+dry, orig["w"], orig["h"]
        elif op == "resize_BR":
            hb["x"], hb["y"] = orig["x"], orig["y"]
            hb["w"], hb["h"] = max(2, orig["w"]+drx), max(2, orig["h"]+dry)
        elif op == "resize_TL":
            nx, ny = min(orig["x"]+drx, fx2-2), min(orig["y"]+dry, fy2-2)
            hb["x"], hb["y"], hb["w"], hb["h"] = nx, ny, fx2-nx, fy2-ny
        elif op == "resize_TR":
            ny = min(orig["y"]+dry, fy2-2)
            hb["x"], hb["y"] = orig["x"], ny
            hb["w"], hb["h"] = max(2, orig["w"]+drx), fy2-ny
        elif op == "resize_BL":
            nx = min(orig["x"]+drx, fx2-2)
            hb["x"], hb["y"] = nx, orig["y"]
            hb["w"], hb["h"] = fx2-nx, max(2, orig["h"]+dry)
        self._clamp_hb(hb)

    def _on_motion(self, event):
        if self._erase_mode:
            self._draw_eraser_cursor(event.x, event.y)
        elif self._pencil_mode:
            self._draw_pencil_cursor(event.x, event.y)
        elif self._active_tool == "select" and self._sel_state == "floating":
            op = self._sel_hit_test(event.x, event.y)
            cursors = {"rotate": "exchange", "move": "fleur",
                       "tl": "top_left_corner", "tr": "top_right_corner",
                       "br": "bottom_right_corner", "bl": "bottom_left_corner"}
            self._canvas.config(cursor=cursors.get(op, "crosshair"))

    def _on_leave(self, _event):
        if self._erase_mode:
            self._canvas.delete("eraser_cursor")
        elif self._pencil_mode:
            self._canvas.delete("pencil_cursor")

    # ── erase tool ───────────────────────────────────────────────────────────

    def _draw_eraser_cursor(self, cx, cy):
        self._canvas.delete("eraser_cursor")
        r_px = self._eraser_radius.get()
        r = max(1, r_px * max(self._res_disp_w,1) / max(self._res_img_w,1))
        if r_px == 1:
            self._canvas.create_rectangle(cx-1, cy-1, cx+1, cy+1,
                                          outline=YELLOW, width=1,
                                          tags="eraser_cursor")
        else:
            self._canvas.create_oval(cx-r, cy-r, cx+r, cy+r,
                                     outline=YELLOW, width=1.5, dash=(4,2),
                                     tags="eraser_cursor")

    def _erase_at(self, cx, cy):
        if self._erased_result is None:
            try:
                self._erased_result = self._build_result().copy()
            except Exception:
                return
        arr   = np.array(self._erased_result)
        H, W  = arr.shape[:2]
        rx, ry = self._canvas_to_result(cx, cy)
        r = self._eraser_radius.get()
        Y, X = np.ogrid[:H, :W]
        mask = (X-rx)**2 + (Y-ry)**2 <= (0 if r == 1 else r*r)
        arr[mask, 3] = 0
        self._erased_result = Image.fromarray(arr, "RGBA")
        self._render()

    # ── pencil tool ───────────────────────────────────────────────────────────

    def _draw_pencil_cursor(self, cx, cy):
        self._canvas.delete("pencil_cursor")
        r_px = self._eraser_radius.get()
        r    = max(1, r_px * max(self._res_disp_w, 1) / max(self._res_img_w, 1))
        col  = self._pencil_color
        if r_px == 1:
            self._canvas.create_rectangle(cx-1, cy-1, cx+1, cy+1,
                                          outline="#ffffff", width=1,
                                          fill=col, tags="pencil_cursor")
        else:
            self._canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                     outline="#ffffff", width=1.5,
                                     fill=col, stipple="gray50",
                                     tags="pencil_cursor")

    def _pencil_at(self, cx, cy):
        if self._erased_result is None:
            try:
                self._erased_result = self._build_result().copy()
            except Exception:
                return
        arr    = np.array(self._erased_result)
        H, W   = arr.shape[:2]
        rx, ry = self._canvas_to_result(cx, cy)
        r      = self._eraser_radius.get()
        Y, X   = np.ogrid[:H, :W]
        mask   = (X - rx) ** 2 + (Y - ry) ** 2 <= (0 if r == 1 else r * r)
        col    = self._pencil_color.lstrip("#")
        rgb    = tuple(int(col[i:i + 2], 16) for i in (0, 2, 4))
        arr[mask] = [*rgb, 255]
        self._erased_result = Image.fromarray(arr, "RGBA")
        self._render()

    # ── dropper tool ─────────────────────────────────────────────────────────

    def _dropper_pick(self, cx, cy):
        """Sample the pixel under (cx,cy) and set it as the draw color."""
        try:
            img = self._get_final_image().convert("RGBA")
            rx, ry = self._canvas_to_result(cx, cy)
            rx = max(0, min(img.width  - 1, round(rx)))
            ry = max(0, min(img.height - 1, round(ry)))
            r, g, b, a = img.getpixel((rx, ry))
            color = f"#{r:02x}{g:02x}{b:02x}"
            self._set_pencil_color(color)
            # Auto-switch to pencil after picking
            self._set_tool("pencil")
        except Exception:
            pass

    # ── select tool ──────────────────────────────────────────────────────────

    def _sel_corners_img(self):
        """4 corners (TL,TR,BR,BL) of the selection in image coords."""
        a = math.radians(self._sel_angle)
        ca, sa = math.cos(a), math.sin(a)
        cx, cy, hw, hh = self._sel_cx, self._sel_cy, self._sel_hw, self._sel_hh
        locs = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
        return [(cx + lx*ca - ly*sa, cy + lx*sa + ly*ca) for lx, ly in locs]

    def _sel_rotation_handle_img(self):
        """Rotation handle position in image coords (above top-centre)."""
        a = math.radians(self._sel_angle)
        R = self._sel_hh + 20
        return (self._sel_cx + R * math.sin(a),
                self._sel_cy - R * math.cos(a))

    def _sel_composite(self) -> Image.Image:
        """Return base + transformed selection composited together."""
        composite = self._sel_base.copy() if self._sel_base else \
                    (self._erased_result or self._build_result()).copy()
        overlay = Image.new("RGBA", composite.size, (0, 0, 0, 0))
        try:
            sw = max(1, round(self._sel_hw * 2))
            sh = max(1, round(self._sel_hh * 2))
            sel = self._sel_img.resize((sw, sh), Image.LANCZOS)
            if abs(self._sel_angle) > 0.1:
                sel = sel.rotate(-self._sel_angle, expand=True,
                                 resample=Image.BICUBIC)
            px = round(self._sel_cx - sel.width / 2)
            py = round(self._sel_cy - sel.height / 2)
            overlay.paste(sel, (px, py), sel)
        except Exception:
            pass
        return Image.alpha_composite(composite, overlay)

    def _sel_lift(self):
        """Lift the drawn rectangle out of the image into a floating selection."""
        img = (self._erased_result if self._erased_result is not None
               else self._build_result().copy()).convert("RGBA")
        cx0, cy0 = self._canvas_to_result(*self._sel_draw_start)
        cx1, cy1 = self._canvas_to_result(*self._sel_draw_cur)
        x0, y0 = max(0.0, min(cx0, cx1)), max(0.0, min(cy0, cy1))
        x1, y1 = min(float(img.width), max(cx0, cx1)), \
                 min(float(img.height), max(cy0, cy1))
        if x1 - x0 < 2 or y1 - y0 < 2:
            self._sel_state = None
            return
        ix0, iy0, ix1, iy1 = round(x0), round(y0), round(x1), round(y1)
        self._sel_orig = img.copy()
        self._sel_img  = img.crop((ix0, iy0, ix1, iy1))
        arr = np.array(img)
        arr[iy0:iy1, ix0:ix1, 3] = 0
        self._sel_base       = Image.fromarray(arr, "RGBA")
        self._erased_result  = self._sel_base
        self._sel_cx    = (x0 + x1) / 2
        self._sel_cy    = (y0 + y1) / 2
        self._sel_hw    = (x1 - x0) / 2
        self._sel_hh    = (y1 - y0) / 2
        self._sel_angle = 0.0
        self._sel_state = "floating"
        self._sel_draw_start = self._sel_draw_cur = None
        self._render()

    def _sel_commit(self):
        """Bake the floating selection back into _erased_result."""
        if self._sel_state != "floating" or self._sel_img is None:
            return
        self._push_undo()
        self._erased_result = self._sel_composite()
        self._sel_state = None
        self._sel_img = self._sel_base = self._sel_orig = None
        self._render()

    def _sel_cancel(self):
        """Cancel the selection, restoring the original image."""
        if self._sel_state == "floating" and self._sel_orig is not None:
            self._erased_result = self._sel_orig
        self._sel_state = None
        self._sel_img = self._sel_base = self._sel_orig = None
        self._sel_draw_start = self._sel_draw_cur = None
        self._render()

    def _sel_point_inside(self, cx, cy) -> bool:
        """True if canvas point is inside the floating selection box."""
        ix, iy = self._canvas_to_result(cx, cy)
        dx, dy = ix - self._sel_cx, iy - self._sel_cy
        a = math.radians(self._sel_angle)
        lx =  dx * math.cos(a) + dy * math.sin(a)
        ly = -dx * math.sin(a) + dy * math.cos(a)
        return abs(lx) <= self._sel_hw and abs(ly) <= self._sel_hh

    def _sel_hit_test(self, cx, cy) -> str | None:
        """Return drag op for canvas click: 'rotate','tl','tr','br','bl','move'."""
        if self._sel_state != "floating":
            return None
        # Rotation handle
        rh = self._sel_rotation_handle_img()
        rhcx, rhcy = self._result_to_canvas(*rh)
        if (cx - rhcx)**2 + (cy - rhcy)**2 <= 64:
            return "rotate"
        # Corner handles
        corners_img = self._sel_corners_img()
        corners_cv  = [self._result_to_canvas(ix, iy) for ix, iy in corners_img]
        r = 8
        for op, (hcx, hcy) in zip(("tl", "tr", "br", "bl"), corners_cv):
            if abs(cx - hcx) <= r and abs(cy - hcy) <= r:
                return op
        if self._sel_point_inside(cx, cy):
            return "move"
        return None

    def _sel_apply_drag(self, cx, cy):
        """Update selection transform based on current cursor position."""
        op = self._sel_drag_op
        if op is None or self._sel_drag_orig is None:
            return
        orig_cx, orig_cy, orig_hw, orig_hh, orig_angle = self._sel_drag_orig

        if op == "move":
            dcx, dcy = self._canvas_delta_to_result(cx - self._sel_drag_sx,
                                                    cy - self._sel_drag_sy)
            self._sel_cx = orig_cx + dcx
            self._sel_cy = orig_cy + dcy

        elif op == "rotate":
            ix, iy = self._canvas_to_result(cx, cy)
            sx_img, sy_img = self._canvas_to_result(self._sel_drag_sx,
                                                    self._sel_drag_sy)
            start_a = math.degrees(math.atan2(sx_img - orig_cx,
                                              -(sy_img - orig_cy)))
            cur_a   = math.degrees(math.atan2(ix - orig_cx, -(iy - orig_cy)))
            self._sel_angle = orig_angle + (cur_a - start_a)

        else:  # corner resize: tl, tr, br, bl
            a  = math.radians(orig_angle)
            ca, sa = math.cos(a), math.sin(a)
            # Fixed corner opposite to the one being dragged
            fixed_local = {"tl": (orig_hw,  orig_hh),
                           "tr": (-orig_hw, orig_hh),
                           "br": (-orig_hw, -orig_hh),
                           "bl": (orig_hw,  -orig_hh)}[op]
            fl_x, fl_y = fixed_local
            fixed_gx = orig_cx + fl_x * ca - fl_y * sa
            fixed_gy = orig_cy + fl_x * sa + fl_y * ca
            ix, iy = self._canvas_to_result(cx, cy)
            diag_gx, diag_gy = ix - fixed_gx, iy - fixed_gy
            local_w = diag_gx * ca + diag_gy * sa
            local_h = -diag_gx * sa + diag_gy * ca
            self._sel_hw = max(4.0, abs(local_w) / 2)
            self._sel_hh = max(4.0, abs(local_h) / 2)
            self._sel_cx = (fixed_gx + ix) / 2
            self._sel_cy = (fixed_gy + iy) / 2

    def _sel_render_overlay(self, canvas):
        """Draw selection rectangle + handles on the canvas."""
        canvas.delete("sel_overlay")
        canvas.delete("sel_handles")
        if self._active_tool != "select":
            return
        if self._sel_state == "drawing" and self._sel_draw_start and self._sel_draw_cur:
            cx0, cy0 = self._sel_draw_start
            cx1, cy1 = self._sel_draw_cur
            canvas.create_rectangle(min(cx0,cx1), min(cy0,cy1),
                                    max(cx0,cx1), max(cy0,cy1),
                                    outline=ACCENT, width=1.5, dash=(4,2),
                                    fill="", tags="sel_overlay")
        elif self._sel_state == "floating":
            corners_img = self._sel_corners_img()
            corners_cv  = [self._result_to_canvas(ix, iy) for ix, iy in corners_img]
            # Dashed border
            for i in range(4):
                ax, ay = corners_cv[i]; bx, by = corners_cv[(i+1)%4]
                canvas.create_line(ax, ay, bx, by,
                                   fill=ACCENT, width=1.5, dash=(4,2),
                                   tags="sel_overlay")
            # Corner handles
            r = 5
            for hcx, hcy in corners_cv:
                canvas.create_rectangle(hcx-r, hcy-r, hcx+r, hcy+r,
                                        fill=BG_CARD, outline=ACCENT, width=1.5,
                                        tags="sel_handles")
            # Rotation handle + connecting line
            tcx, tcy = corners_cv[0]; tr_x, tr_y = corners_cv[1]
            top_cx, top_cy = (tcx+tr_x)/2, (tcy+tr_y)/2
            rh = self._sel_rotation_handle_img()
            rhcx, rhcy = self._result_to_canvas(*rh)
            canvas.create_line(top_cx, top_cy, rhcx, rhcy,
                               fill=GREEN, width=1, tags="sel_overlay")
            canvas.create_oval(rhcx-6, rhcy-6, rhcx+6, rhcy+6,
                               fill=BG_CARD, outline=GREEN, width=1.5,
                               tags="sel_handles")

    # ── help dialog ───────────────────────────────────────────────────────────

    def _show_help(self):
        dlg = tk.Toplevel(self._win)
        dlg.title("Keyboard Shortcuts")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self._win)
        dlg.grab_set()

        tk.Label(dlg, text="Frame Editor  —  Keyboard Shortcuts",
                 bg=BG, fg=FG, font=("", 10, "bold")).pack(padx=20, pady=(12, 4))

        frame = tk.Frame(dlg, bg=BG_CARD)
        frame.pack(padx=16, pady=(4, 4), fill=tk.BOTH)

        rows = [
            ("S",       "Select",     "draw rect, then move / resize / rotate region"),
            ("R",       "Rotate",     "drag canvas handle to rotate image"),
            ("W",       "Warp",       "drag corners to skew / perspective"),
            ("E",       "Erase",      "paint transparency"),
            ("D",       "Draw",       "pencil / paint"),
            ("I",       "Dropper",    "sample pixel color → active draw color"),
            ("C",       "Clip",       "erase a rectangle region"),
            ("P",       "Crop",       "crop to a rectangle"),
            ("Enter",   "Commit",     "bake floating selection"),
            ("Esc",     "Cancel",     "cancel / deselect"),
            ("Ctrl+Z",  "Undo",       "undo last change"),
            ("?",       "Help",       "show this dialog"),
        ]
        for key, label, desc in rows:
            row = tk.Frame(frame, bg=BG_CARD)
            row.pack(fill=tk.X, padx=8, pady=2)
            tk.Label(row, text=key, bg=BG_CARD, fg=ACCENT,
                     font=("Consolas", 9, "bold"), width=7, anchor=tk.E
                     ).pack(side=tk.LEFT, padx=(0, 8))
            tk.Label(row, text=label, bg=BG_CARD, fg=FG,
                     font=("", 9, "bold"), width=9, anchor=tk.W
                     ).pack(side=tk.LEFT)
            tk.Label(row, text=desc, bg=BG_CARD, fg=FG_DIM,
                     font=("", 8), anchor=tk.W
                     ).pack(side=tk.LEFT)

        tk.Button(dlg, text="Close", command=dlg.destroy,
                  bg=BG_PANEL, fg=FG_DIM, relief=tk.FLAT,
                  font=("", 9), padx=12, pady=3,
                  cursor="hand2", borderwidth=0, highlightthickness=0
                  ).pack(pady=(8, 12))

    # ── frame navigation ──────────────────────────────────────────────────────

    def _is_dirty(self) -> bool:
        """True if the image has been modified from its original state."""
        if self._erased_result is not None:
            return True
        if self._rotation.get() != 0.0:
            return True
        if self._flip_h or self._flip_v:
            return True
        try:
            base = self._get_base()
            W, H = base.size
            nat = [[0., 0.], [float(W), 0.], [float(W), float(H)], [0., float(H)]]
            if not self._corners_match(nat):
                return True
        except Exception:
            pass
        return False

    def _update_nav_buttons(self):
        """Show/hide the left and right nav arrows based on current frame index."""
        has_nav = bool(self._frame_list and self._get_frame_data)
        if not has_nav or self._frame_index <= 0:
            self._nav_left_btn.grid_remove()
        else:
            self._nav_left_btn.grid()
        if not has_nav or self._frame_index >= len(self._frame_list) - 1:
            self._nav_right_btn.grid_remove()
        else:
            self._nav_right_btn.grid()

    def _nav_go(self, direction: int):
        """Navigate to an adjacent frame (-1=prev, +1=next)."""
        if not self._frame_list or not self._get_frame_data:
            return
        new_idx = self._frame_index + direction
        if new_idx < 0 or new_idx >= len(self._frame_list):
            return
        if self._is_dirty():
            self._nav_prompt(new_idx)
        else:
            self._nav_load(new_idx)

    def _nav_prompt(self, new_idx: int):
        """Prompt the user about unsaved changes, then navigate."""
        dlg = tk.Toplevel(self._win)
        dlg.title("Unsaved Changes")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self._win)
        dlg.grab_set()

        fname = self._frame_path.name
        tk.Label(dlg, text=f"Save changes to  {fname}?",
                 bg=BG, fg=FG, font=("", 10)).pack(padx=24, pady=(16, 12))

        result = [None]

        def choose(r):
            result[0] = r
            dlg.destroy()

        btn_row = tk.Frame(dlg, bg=BG)
        btn_row.pack(padx=16, pady=(0, 16))
        self._btn(btn_row, "Replace Frame",  lambda: choose("replace"), ACCENT ).pack(side=tk.LEFT, padx=3)
        self._btn(btn_row, "Add as New",     lambda: choose("new"),     GREEN  ).pack(side=tk.LEFT, padx=3)
        self._btn(btn_row, "Discard",        lambda: choose("discard"), RED    ).pack(side=tk.LEFT, padx=3)
        self._btn(btn_row, "Cancel",         lambda: choose("cancel"),  FG_DIM ).pack(side=tk.LEFT, padx=3)

        dlg.wait_window()

        if result[0] in ("replace", "new"):
            try:
                self._on_save(self._get_final_image(),
                              replace=(result[0] == "replace"),
                              hitboxes=list(self._hitboxes))
            except Exception as exc:
                messagebox.showerror("Save Error", str(exc), parent=self._win)
                return
            self._nav_load(new_idx)
        elif result[0] == "discard":
            self._nav_load(new_idx)
        # "cancel" → do nothing

    def _nav_load(self, new_idx: int):
        """Reload the editor in-place for a different frame."""
        fp, fm, new_on_save = self._get_frame_data(new_idx)

        # Update identity
        self._frame_path  = fp
        self._frame_index = new_idx
        self._on_save     = new_on_save

        # Reload source image
        self._src = Image.open(fp).convert("RGBA")

        # Reset transform state
        self._flip_h = False
        self._flip_v = False
        self._rotation.set(0.0)
        if self._rot_lbl:
            self._rot_lbl.config(text="0°")

        # Reset all edit/tool state
        self._erased_result   = None
        self._active_tool     = None
        self._erase_mode      = False
        self._pencil_mode     = False
        self._clip_mode       = False
        self._crop_mode       = False
        self._rot_drag_active = False
        self._sel_state       = None
        self._sel_img         = self._sel_base = self._sel_orig = None
        self._sel_draw_start  = self._sel_draw_cur = None
        self._clip_start      = self._clip_cur = None
        self._crop_start      = self._crop_cur = None
        self._canvas.config(cursor="crosshair")

        # Reset toolbar button highlights
        for btn in (self._sel_btn, self._rotate_btn, self._warp_btn,
                    self._erase_btn, self._pencil_btn, self._dropper_btn,
                    self._clip_btn, self._crop_btn):
            if btn:
                btn.config(fg=FG_DIM, bg=BG_CARD)
        if self._hb_add_btn:
            self._hb_add_btn.config(fg=FG_DIM, bg=BG_PANEL)

        # Load hitboxes from new frame meta
        raw = list((fm or {}).get("hitboxes", []))
        for i, hb in enumerate(raw):
            if "name" not in hb:
                hb["name"] = f"hitbox {i + 1}"
        self._hitboxes    = raw
        self._selected_hb = None
        self._hb_draw_mode = False
        self._hb_draw_start = self._hb_draw_cur = None

        # Clear undo history — prior frame's history doesn't apply
        self._undo_stack.clear()

        # Rebuild nav button visibility, sync hitbox list, reset corners → renders
        self._update_nav_buttons()
        self._hb_sync_list()
        self._reset_corners()   # calls _render()
        self._update_title()

    # ── save ─────────────────────────────────────────────────────────────────

    def _save_new(self):
        try:
            self._on_save(self._get_final_image(),
                          replace=False, hitboxes=list(self._hitboxes))
            self._win.destroy()
        except Exception as exc:
            messagebox.showerror("Transform Error", str(exc), parent=self._win)

    def _save_replace(self):
        try:
            self._on_save(self._get_final_image(),
                          replace=True, hitboxes=list(self._hitboxes))
            self._win.destroy()
        except Exception as exc:
            messagebox.showerror("Transform Error", str(exc), parent=self._win)
