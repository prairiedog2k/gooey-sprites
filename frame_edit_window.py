"""
FrameEditWindow — per-frame rotate / flip / perspective warp / erase tool.

Usage (from sprite_gui.py or compose_window.py):
    FrameEditWindow(root, frame_path, on_save)

`on_save(result: PIL.Image, replace: bool)` is called when the user
confirms.  replace=True → overwrite the source frame;
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


# ── perspective helpers ───────────────────────────────────────────────────────

def _perspective_coeffs(src_pts, dst_pts):
    """
    Compute 8 PIL PERSPECTIVE coefficients that map *dst* coords → *src* coords.

    PIL's PERSPECTIVE transform samples the source image using:
        src_x = (a*x + b*y + c) / (g*x + h*y + 1)
        src_y = (d*x + e*y + f) / (g*x + h*y + 1)
    where (x,y) are destination pixel coordinates.

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
    """Interactive rotate / flip / corner-warp / erase tool for a single sprite frame."""

    _HANDLE_R  = 7      # handle hit radius (px)
    _CANVAS_W  = 480
    _CANVAS_H  = 380
    _PAD       = 60     # canvas padding so handles can move outside image
    # Corner order: TL, TR, BR, BL
    _HANDLE_COLORS = (ACCENT, GREEN, YELLOW, RED)

    def __init__(self, parent: tk.Tk, frame_path: Path, on_save):
        self._frame_path = frame_path
        self._on_save    = on_save

        self._win = tk.Toplevel(parent)
        self._win.title(f"Edit Frame  —  {frame_path.name}")
        self._win.configure(bg=BG)
        self._win.geometry("520x600")
        self._win.resizable(True, True)
        self._win.minsize(400, 480)

        # Load source image (never mutated)
        self._src = Image.open(frame_path).convert("RGBA")

        # Transform state
        self._flip_h   = False
        self._flip_v   = False
        self._rotation = tk.DoubleVar(value=0.0)

        # Corner handles in "base-image space" (pixels relative to base top-left)
        # Order: TL, TR, BR, BL — reset whenever flip/rotate changes
        self._corners: list[list[float]] = []
        self._drag_idx: int | None = None

        # Display geometry (updated each render)
        self._scale    = 1.0
        self._origin_x = 0.0
        self._origin_y = 0.0

        # Cached result display geometry for erase coordinate mapping
        self._res_canvas_x  = 0.0   # canvas x of result image top-left
        self._res_canvas_y  = 0.0   # canvas y of result image top-left
        self._res_disp_w    = 1     # displayed width of result image
        self._res_disp_h    = 1     # displayed height of result image
        self._res_img_w     = 1     # actual result image width (pixels)
        self._res_img_h     = 1     # actual result image height (pixels)

        # Erase tool state
        self._erase_mode   = False
        self._erased_result: Image.Image | None = None
        self._eraser_radius = tk.IntVar(value=15)

        self._photo: ImageTk.PhotoImage | None = None
        self._erase_btn: tk.Button | None = None

        self._build_ui()
        self._reset_corners()
        self._win.grab_set()

    # ── base image ───────────────────────────────────────────────────────────

    def _get_base(self) -> Image.Image:
        """Original image after flip and rotation (no perspective yet)."""
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
        """Set handles to the natural corners of the current base image."""
        base = self._get_base()
        W, H = base.size
        self._corners = [
            [0.0,    0.0   ],   # TL
            [float(W), 0.0 ],   # TR
            [float(W), float(H)],  # BR
            [0.0,    float(H)],  # BL
        ]
        self._erased_result = None
        self._render()

    # ── result image ─────────────────────────────────────────────────────────

    def _build_result(self) -> Image.Image:
        """Apply full transform pipeline and return final RGBA image."""
        base = self._get_base()
        W, H = base.size

        nat = [[0., 0.], [float(W), 0.], [float(W), float(H)], [0., float(H)]]
        if self._corners_match(nat):
            return base

        xs = [p[0] for p in self._corners]
        ys = [p[1] for p in self._corners]
        min_x, min_y = min(xs), min(ys)
        max_x, max_y = max(xs), max(ys)
        out_w = max(1, round(max_x - min_x))
        out_h = max(1, round(max_y - min_y))

        # dst: handle positions relative to output bounding box
        dst = [(p[0] - min_x, p[1] - min_y) for p in self._corners]
        # src: original base image corners
        src = [(0., 0.), (float(W), 0.), (float(W), float(H)), (0., float(H))]

        coeffs = _perspective_coeffs(src, dst)
        return base.transform((out_w, out_h), Image.PERSPECTIVE, coeffs,
                              Image.BICUBIC)

    def _get_final_image(self) -> Image.Image:
        """Return erased result if available, otherwise the plain result."""
        if self._erased_result is not None:
            return self._erased_result
        return self._build_result()

    def _corners_match(self, nat):
        return all(
            abs(a[0] - b[0]) < 0.5 and abs(a[1] - b[1]) < 0.5
            for a, b in zip(self._corners, nat))

    # ── rendering ────────────────────────────────────────────────────────────

    def _update_display_params(self):
        cw = self._canvas.winfo_width()  or self._CANVAS_W
        ch = self._canvas.winfo_height() or self._CANVAS_H

        xs = [p[0] for p in self._corners]
        ys = [p[1] for p in self._corners]
        bbox_w = max(max(xs) - min(xs), 1)
        bbox_h = max(max(ys) - min(ys), 1)
        pad    = self._PAD

        scale = min((cw - pad * 2) / bbox_w,
                    (ch - pad * 2) / bbox_h,
                    8.0)
        self._scale = max(scale, 0.05)

        cx_mid = (min(xs) + max(xs)) / 2
        cy_mid = (min(ys) + max(ys)) / 2
        self._origin_x = cw / 2 - cx_mid * self._scale
        self._origin_y = ch / 2 - cy_mid * self._scale

    def _img_to_canvas(self, ix, iy):
        return (self._origin_x + ix * self._scale,
                self._origin_y + iy * self._scale)

    def _canvas_to_img(self, cx, cy):
        s = self._scale
        return ((cx - self._origin_x) / s,
                (cy - self._origin_y) / s)

    def _canvas_to_result(self, cx, cy):
        """Convert canvas coords to result image pixel coords."""
        rx = (cx - self._res_canvas_x) * self._res_img_w / max(self._res_disp_w, 1)
        ry = (cy - self._res_canvas_y) * self._res_img_h / max(self._res_disp_h, 1)
        return int(rx), int(ry)

    def _render(self):
        if not self._corners:
            return
        self._update_display_params()

        try:
            result = self._get_final_image()
        except Exception:
            return

        canvas = self._canvas
        cw = canvas.winfo_width()  or self._CANVAS_W
        ch = canvas.winfo_height() or self._CANVAS_H

        # Corner positions on canvas
        cpts = [self._img_to_canvas(p[0], p[1]) for p in self._corners]
        xs_c = [p[0] for p in cpts]
        ys_c = [p[1] for p in cpts]
        min_cx = min(xs_c)
        min_cy = min(ys_c)
        disp_w = max(1, round(max(xs_c) - min_cx))
        disp_h = max(1, round(max(ys_c) - min_cy))

        # Cache result display geometry for erase coordinate mapping
        self._res_canvas_x = min_cx
        self._res_canvas_y = min_cy
        self._res_disp_w   = disp_w
        self._res_disp_h   = disp_h
        self._res_img_w    = result.width
        self._res_img_h    = result.height

        # Composite on a dark bg for transparency
        panel_rgb = tuple(int(BG_CARD.lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
        bg  = Image.new("RGBA", result.size, (*panel_rgb, 255))
        flat = Image.alpha_composite(bg, result.convert("RGBA"))
        flat = flat.resize((disp_w, disp_h), Image.NEAREST)
        self._photo = ImageTk.PhotoImage(flat)

        canvas.delete("all")

        # Checkerboard behind image
        sz = 10
        for row in range(0, disp_h, sz):
            for col in range(0, disp_w, sz):
                dark = (row // sz + col // sz) % 2 == 0
                col_c = "#1e1e2e" if dark else "#2a2a3e"
                canvas.create_rectangle(
                    int(min_cx) + col, int(min_cy) + row,
                    int(min_cx) + col + sz, int(min_cy) + row + sz,
                    fill=col_c, outline="")

        canvas.create_image(int(min_cx), int(min_cy),
                            image=self._photo, anchor=tk.NW)

        if not self._erase_mode:
            # Draw edge lines between handles
            for i in range(4):
                ax, ay = cpts[i]
                bx, by = cpts[(i + 1) % 4]
                canvas.create_line(ax, ay, bx, by,
                                   fill="#555577", width=1, dash=(4, 3))

            # Draw handles
            r = self._HANDLE_R
            for i, (cx_h, cy_h) in enumerate(cpts):
                canvas.create_oval(cx_h - r, cy_h - r, cx_h + r, cy_h + r,
                                   fill=self._HANDLE_COLORS[i],
                                   outline="#ffffff", width=1.5)

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Canvas
        self._canvas = tk.Canvas(self._win,
                                 width=self._CANVAS_W, height=self._CANVAS_H,
                                 bg=BG_PANEL, highlightthickness=0,
                                 cursor="crosshair")
        self._canvas.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self._canvas.bind("<Configure>",       lambda _: self._render())
        self._canvas.bind("<ButtonPress-1>",   self._on_press)
        self._canvas.bind("<B1-Motion>",       self._on_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)
        self._canvas.bind("<Motion>",          self._on_motion)
        self._canvas.bind("<Leave>",           self._on_leave)

        # Controls panel
        ctrl = tk.Frame(self._win, bg=BG_PANEL, pady=4)
        ctrl.pack(fill=tk.X, padx=8)

        # Rotation row
        rot_row = tk.Frame(ctrl, bg=BG_PANEL)
        rot_row.pack(fill=tk.X, pady=2)
        tk.Label(rot_row, text="Rotate", bg=BG_PANEL, fg=FG_DIM,
                 font=("", 9), width=7, anchor=tk.W).pack(side=tk.LEFT)
        self._rot_lbl = tk.Label(rot_row, text="0°", bg=BG_PANEL,
                                 fg=FG, font=("Consolas", 9), width=5,
                                 anchor=tk.E)
        self._rot_lbl.pack(side=tk.RIGHT, padx=4)
        tk.Scale(rot_row, variable=self._rotation,
                 from_=-180, to=180, resolution=1,
                 orient=tk.HORIZONTAL, showvalue=False,
                 command=self._on_rotation_change,
                 bg=BG_PANEL, fg=FG, troughcolor=BG_CARD,
                 highlightthickness=0, relief=tk.FLAT,
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        # Flip / reset row
        act_row = tk.Frame(ctrl, bg=BG_PANEL)
        act_row.pack(fill=tk.X, pady=2)
        self._btn(act_row, "Flip Horizontal", self._do_flip_h, ACCENT
                  ).pack(side=tk.LEFT, padx=(0, 4))
        self._btn(act_row, "Flip Vertical", self._do_flip_v, ACCENT
                  ).pack(side=tk.LEFT, padx=4)
        self._btn(act_row, "Reset All", self._do_reset, RED, small=True
                  ).pack(side=tk.RIGHT, padx=4)

        # Erase row
        erase_row = tk.Frame(ctrl, bg=BG_PANEL)
        erase_row.pack(fill=tk.X, pady=2)
        self._erase_btn = tk.Button(
            erase_row, text="Erase", command=self._toggle_erase,
            bg=BG_CARD, fg=FG_DIM,
            activeforeground=FG_DIM, activebackground=BG_SEL,
            relief=tk.FLAT, font=("", 9),
            padx=8, pady=3, cursor="hand2",
            borderwidth=0, highlightthickness=0)
        self._erase_btn.pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(erase_row, text="Size", bg=BG_PANEL, fg=FG_DIM,
                 font=("", 9)).pack(side=tk.LEFT)
        self._eraser_size_lbl = tk.Label(erase_row, text="15px", bg=BG_PANEL,
                                         fg=FG, font=("Consolas", 9), width=4,
                                         anchor=tk.E)
        self._eraser_size_lbl.pack(side=tk.RIGHT, padx=4)
        tk.Scale(erase_row, variable=self._eraser_radius,
                 from_=2, to=60, resolution=1,
                 orient=tk.HORIZONTAL, showvalue=False,
                 command=self._on_eraser_size_change,
                 bg=BG_PANEL, fg=FG, troughcolor=BG_CARD,
                 highlightthickness=0, relief=tk.FLAT,
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        # Hint label
        tk.Label(ctrl,
                 text="Drag corner handles to warp  ·  TL=blue  TR=green  BR=yellow  BL=red",
                 bg=BG_PANEL, fg=FG_DIM, font=("", 8)
                 ).pack(pady=(4, 0))

        # Bottom buttons
        bot = tk.Frame(self._win, bg=BG_PANEL, pady=6)
        bot.pack(fill=tk.X, padx=8, pady=(0, 8))
        self._btn(bot, "Cancel",            self._win.destroy,  FG_DIM
                  ).pack(side=tk.LEFT)
        self._btn(bot, "Replace Frame",     self._save_replace, ACCENT
                  ).pack(side=tk.RIGHT, padx=(4, 0))
        self._btn(bot, "Save as New Frame", self._save_new,     GREEN
                  ).pack(side=tk.RIGHT, padx=4)

    def _btn(self, parent, text, cmd, color=FG_DIM, small=False):
        return tk.Button(parent, text=text, command=cmd,
                         bg=BG_CARD, fg=color,
                         activeforeground=color, activebackground=BG_SEL,
                         relief=tk.FLAT, font=("", 8 if small else 9),
                         padx=8, pady=3, cursor="hand2",
                         borderwidth=0, highlightthickness=0)

    # ── control callbacks ─────────────────────────────────────────────────────

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

    def _toggle_erase(self):
        self._erase_mode = not self._erase_mode
        if self._erase_mode:
            self._erase_btn.config(fg=YELLOW, bg=BG_SEL)
            self._canvas.config(cursor="none")
        else:
            self._erase_btn.config(fg=FG_DIM, bg=BG_CARD)
            self._canvas.config(cursor="crosshair")
            self._canvas.delete("eraser_cursor")
        self._render()

    def _on_eraser_size_change(self, _=None):
        self._eraser_size_lbl.config(text=f"{self._eraser_radius.get()}px")

    # ── handle drag ───────────────────────────────────────────────────────────

    def _on_press(self, event):
        if self._erase_mode:
            self._erase_at(event.x, event.y)
            return
        r = self._HANDLE_R + 4  # generous hit area
        for i, corner in enumerate(self._corners):
            cx, cy = self._img_to_canvas(*corner)
            if abs(event.x - cx) <= r and abs(event.y - cy) <= r:
                self._drag_idx = i
                return
        self._drag_idx = None

    def _on_drag(self, event):
        if self._erase_mode:
            self._erase_at(event.x, event.y)
            self._draw_eraser_cursor(event.x, event.y)
            return
        if self._drag_idx is None:
            return
        ix, iy = self._canvas_to_img(event.x, event.y)
        self._corners[self._drag_idx] = [ix, iy]
        self._render()

    def _on_release(self, _event):
        self._drag_idx = None

    def _on_motion(self, event):
        if self._erase_mode:
            self._draw_eraser_cursor(event.x, event.y)

    def _on_leave(self, _event):
        if self._erase_mode:
            self._canvas.delete("eraser_cursor")

    # ── erase tool ───────────────────────────────────────────────────────────

    def _draw_eraser_cursor(self, cx, cy):
        """Draw eraser circle outline on canvas (canvas pixel radius)."""
        self._canvas.delete("eraser_cursor")
        # radius in canvas pixels = eraser_radius * (disp_w / img_w)
        img_w = max(self._res_img_w, 1)
        disp_w = max(self._res_disp_w, 1)
        r_canvas = self._eraser_radius.get() * disp_w / img_w
        r = max(2, r_canvas)
        self._canvas.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            outline=YELLOW, width=1.5, dash=(4, 2),
            tags="eraser_cursor")

    def _erase_at(self, cx, cy):
        """Erase a circle of pixels at canvas position (cx, cy)."""
        # Ensure we have a mutable copy of the result to erase into
        if self._erased_result is None:
            try:
                self._erased_result = self._build_result().copy()
            except Exception:
                return

        img_arr = np.array(self._erased_result)
        H, W = img_arr.shape[:2]

        # Convert canvas → result image coords
        rx, ry = self._canvas_to_result(cx, cy)
        r = self._eraser_radius.get()

        # Circular mask using grid distances
        Y, X = np.ogrid[:H, :W]
        mask = (X - rx) ** 2 + (Y - ry) ** 2 <= r * r

        img_arr[mask, 3] = 0  # set alpha channel to 0

        self._erased_result = Image.fromarray(img_arr, "RGBA")
        self._render()

    # ── save ─────────────────────────────────────────────────────────────────

    def _save_new(self):
        try:
            img = self._get_final_image()
            self._on_save(img, replace=False)
            self._win.destroy()
        except Exception as exc:
            messagebox.showerror("Transform Error", str(exc), parent=self._win)

    def _save_replace(self):
        try:
            img = self._get_final_image()
            self._on_save(img, replace=True)
            self._win.destroy()
        except Exception as exc:
            messagebox.showerror("Transform Error", str(exc), parent=self._win)
