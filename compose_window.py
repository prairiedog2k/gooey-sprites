"""
ComposeWindow — Toplevel for building a new animation from existing frames.

Layout
------
┌───────────────────────────────────────────────────────────────┐
│  Name: [______________]                      [Save]  [Close] │
├───────────────────────────────────────────────────────────────┤
│  SOURCE ANIMATIONS  (vertically scrollable)                   │
│   unknown-001  [f0][f1][f2] …                                │
├──────────────────────────────────┬────────────────────────────┤
│  TIMELINE  [controls]            │  PREVIEW                  │
│   [f][f][f] …                   │  [frame image]            │
│   (horizontally scrollable)      │  [▶] [■] Loop  ──delay──  │
└──────────────────────────────────┴────────────────────────────┘

Interactions
------------
• Click source frame       → append to end of timeline.
• Drag source frame        → insert at timeline position.
• Click timeline frame     → toggle selection.
• Right-click timeline     → Duplicate / Rotate / Skew / Remove / Move.
• Drag timeline frame      → reorder.
"""

import json
import tkinter as tk
from tkinter import messagebox
from pathlib import Path

from PIL import Image, ImageTk

from constants import (
    BG, BG_PANEL, BG_CARD, BG_SEL,
    FG, FG_DIM, ACCENT, RED, GREEN, YELLOW,
    THUMB_SRC_H, THUMB_TL_H, MAX_SCALE,
)
from image_helpers import (
    _CItem, _apply_transform, _compose_thumb, _make_thumb, _thumb_scale,
)
from dialogs import _InputDialog


class ComposeWindow:
    _DRAG_THRESHOLD = 8   # px of movement before a click becomes a drag
    _SKEW_PRESETS   = (-30, -15, 15, 30)

    def __init__(self, parent: tk.Tk, output_dir: Path, on_save,
                 initial_anim: Path | None = None):
        self._output_dir  = output_dir
        self._on_save     = on_save
        self._initial_anim = initial_anim

        # Timeline state
        self._items:  list[_CItem] = []
        self._tl_sel: set[int]     = set()

        # Photo-image caches (keep refs to prevent GC)
        self._src_photos: dict[Path, ImageTk.PhotoImage] = {}
        self._tl_photos:  list[ImageTk.PhotoImage]       = []

        # Source drag state
        self._drag_item:   _CItem | None = None
        self._drag_active: bool          = False
        self._drag_press:  tuple[int, int] = (0, 0)
        self._drag_ghost:  tk.Toplevel | None = None

        # Timeline drag-to-reorder state
        self._tl_drag_src:    int | None = None
        self._tl_drag_active: bool       = False
        self._tl_drag_press:  tuple[int, int] = (0, 0)

        # Timeline widget refs
        self._tl_canvas: tk.Canvas | None = None
        self._tl_inner:  tk.Frame  | None = None
        self._tl_cells:  list[tk.Frame]   = []
        self._drop_ind:  tk.Frame  | None = None

        # Preview state
        self._cp_canvas:      tk.Canvas | None = None
        self._cp_btn_play:    tk.Button | None = None
        self._cp_lbl_counter: tk.Label  | None = None
        self._cp_photo                         = None
        self._cp_playing: bool  = False
        self._cp_current: int   = 0
        self._cp_after_id       = None
        self._cp_delay  = tk.IntVar(value=100)
        self._cp_loop   = tk.BooleanVar(value=True)
        self._cp_max_w: int = 1
        self._cp_max_h: int = 1

        self._build(parent)
        if initial_anim and initial_anim.is_dir():
            self._prepopulate(initial_anim)

    # ── construction ──────────────────────────────────────────────────────────

    def _build(self, parent: tk.Tk):
        win = tk.Toplevel(parent)
        win.title("Compose New Animation")
        win.geometry("1200x780")
        win.minsize(900, 560)
        win.configure(bg=BG)
        win.protocol("WM_DELETE_WINDOW", self._close)
        self._win = win

        # header
        hdr = tk.Frame(win, bg=BG_PANEL, padx=10, pady=8)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="Name:", bg=BG_PANEL, fg=FG,
                 font=("", 9)).pack(side=tk.LEFT)
        self._v_name = tk.StringVar()
        tk.Entry(hdr, textvariable=self._v_name, width=30,
                 bg=BG_CARD, fg=FG, insertbackground=FG,
                 font=("Consolas", 10), relief=tk.FLAT
                 ).pack(side=tk.LEFT, padx=(4, 20))
        for txt, cmd, col in (("Save",  self._save,  GREEN),
                               ("Close", self._close, FG_DIM)):
            tk.Button(hdr, text=txt, command=cmd,
                      bg=BG_CARD, fg=col, activeforeground=col,
                      activebackground=BG_SEL, relief=tk.FLAT,
                      padx=12, pady=4, font=("", 9),
                      cursor="hand2").pack(side=tk.RIGHT, padx=4)

        # body: source on top, timeline+preview on bottom
        body = tk.PanedWindow(win, orient=tk.VERTICAL, bg=BG,
                              sashwidth=5, sashrelief=tk.FLAT)
        body.pack(fill=tk.BOTH, expand=True)

        src_f    = tk.Frame(body, bg=BG_PANEL)
        bottom_f = tk.Frame(body, bg=BG)
        body.add(src_f,    minsize=180)
        body.add(bottom_f, minsize=200)

        self._build_source(src_f)
        self._build_bottom(bottom_f)

    def _prepopulate(self, anim_dir: Path):
        """Pre-fill the timeline and name field from an existing animation."""
        self._v_name.set(anim_dir.name)
        for png in sorted(anim_dir.glob("*.png")):
            self._items.append(_CItem(anim_dir, png))
        self._rebuild_timeline()

    # ── source browser ────────────────────────────────────────────────────────

    def _build_source(self, parent: tk.Frame):
        tk.Label(parent,
                 text="Source Animations"
                      "   —   click a frame to append it, or drag it to a"
                      " timeline position",
                 bg=BG_PANEL, fg=ACCENT,
                 font=("", 9, "bold"), pady=4).pack(fill=tk.X, padx=8)

        cf = tk.Frame(parent, bg=BG_PANEL)
        cf.pack(fill=tk.BOTH, expand=True)

        vbar = tk.Scrollbar(cf, orient=tk.VERTICAL,
                            bg=BG_CARD, troughcolor=BG_PANEL, relief=tk.FLAT)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        hbar = tk.Scrollbar(cf, orient=tk.HORIZONTAL,
                            bg=BG_CARD, troughcolor=BG_PANEL, relief=tk.FLAT)
        hbar.pack(side=tk.BOTTOM, fill=tk.X)

        src_cv = tk.Canvas(cf, bg=BG_PANEL,
                           yscrollcommand=vbar.set, xscrollcommand=hbar.set,
                           highlightthickness=0)
        src_cv.pack(fill=tk.BOTH, expand=True)
        vbar.config(command=src_cv.yview)
        hbar.config(command=src_cv.xview)
        src_cv.bind("<MouseWheel>",
                    lambda e: src_cv.yview_scroll(
                        -1 if e.delta > 0 else 1, "units"))

        inner = tk.Frame(src_cv, bg=BG_PANEL)
        src_cv.create_window((0, 0), window=inner, anchor=tk.NW)
        inner.bind("<Configure>",
                   lambda _: src_cv.configure(
                       scrollregion=src_cv.bbox("all")))

        anim_dirs = sorted(
            d for d in self._output_dir.iterdir()
            if d.is_dir() and (d / "frames.json").exists())

        for anim_dir in anim_dirs:
            self._add_source_row(inner, anim_dir)

    def _add_source_row(self, parent: tk.Frame, anim_dir: Path):
        row = tk.Frame(parent, bg=BG_PANEL, pady=3)
        row.pack(fill=tk.X, padx=4, pady=1)

        tk.Label(row, text=anim_dir.name,
                 bg=BG_PANEL, fg=FG_DIM,
                 font=("Consolas", 8), width=16, anchor=tk.W
                 ).pack(side=tk.LEFT, padx=(2, 6))

        pngs = sorted(anim_dir.glob("*.png"))
        if not pngs:
            return
        scale = _thumb_scale(pngs, THUMB_SRC_H)

        for png in pngs:
            item = _CItem(anim_dir, png)
            if png not in self._src_photos:
                self._src_photos[png] = _make_thumb(png, scale)
            photo = self._src_photos[png]

            lbl = tk.Label(row, image=photo, bg=BG_CARD,
                           relief=tk.FLAT, borderwidth=1, cursor="hand2")
            lbl.pack(side=tk.LEFT, padx=1)

            lbl.bind("<Button-1>",
                     lambda e, it=item: self._src_press(e, it))
            lbl.bind("<B1-Motion>",
                     lambda e, it=item: self._src_motion(e, it))
            lbl.bind("<ButtonRelease-1>",
                     lambda e, it=item: self._src_release(e, it))

    # ── source drag / click ───────────────────────────────────────────────────

    def _src_press(self, event, item: _CItem):
        self._drag_item   = item
        self._drag_active = False
        self._drag_press  = (event.x_root, event.y_root)

    def _src_motion(self, event, item: _CItem):
        dx = event.x_root - self._drag_press[0]
        dy = event.y_root - self._drag_press[1]
        if not self._drag_active:
            if dx*dx + dy*dy < self._DRAG_THRESHOLD**2:
                return
            self._drag_active = True
            self._create_ghost(item.png)

        if self._drag_ghost:
            self._drag_ghost.geometry(
                f"+{event.x_root + 14}+{event.y_root + 14}")

        self._show_drop_indicator(self._drop_index(event.x_root, event.y_root))

    def _src_release(self, event, item: _CItem):
        self._destroy_ghost()
        self._hide_drop_indicator()

        if not self._drag_active:
            self._items.append(item.copy())
        else:
            self._drag_active = False
            idx = self._drop_index(event.x_root, event.y_root)
            if idx >= 0:
                self._items.insert(idx, item.copy())
            else:
                self._items.append(item.copy())

        self._rebuild_timeline()

    # ── ghost window ──────────────────────────────────────────────────────────

    def _create_ghost(self, png: Path):
        if self._drag_ghost:
            return
        photo = self._src_photos.get(png)
        if photo is None:
            return
        ghost = tk.Toplevel(self._win)
        ghost.wm_overrideredirect(True)
        ghost.wm_attributes("-topmost", True)
        try:
            ghost.wm_attributes("-alpha", 0.72)
        except Exception:
            pass
        tk.Label(ghost, image=photo, bg=BG_CARD,
                 relief=tk.SOLID, borderwidth=1).pack()
        ghost._photo = photo
        self._drag_ghost = ghost

    def _destroy_ghost(self):
        if self._drag_ghost:
            try:
                self._drag_ghost.destroy()
            except Exception:
                pass
            self._drag_ghost = None

    # ── bottom pane: timeline + preview ──────────────────────────────────────

    def _build_bottom(self, parent: tk.Frame):
        hp = tk.PanedWindow(parent, orient=tk.HORIZONTAL, bg=BG,
                            sashwidth=5, sashrelief=tk.FLAT)
        hp.pack(fill=tk.BOTH, expand=True)

        tl_f = tk.Frame(hp, bg=BG_PANEL)
        pv_f = tk.Frame(hp, bg=BG_PANEL, width=280)
        hp.add(tl_f, minsize=300)
        hp.add(pv_f, minsize=220)

        self._build_timeline(tl_f)
        self._build_preview(pv_f)

    # ── timeline ──────────────────────────────────────────────────────────────

    def _build_timeline(self, parent: tk.Frame):
        ctrl = tk.Frame(parent, bg=BG_PANEL, padx=8, pady=4)
        ctrl.pack(fill=tk.X)

        tk.Label(ctrl, text="Timeline", bg=BG_PANEL, fg=ACCENT,
                 font=("", 9, "bold")).pack(side=tk.LEFT)

        for txt, cmd in (("Remove Selected", self._tl_remove_sel),
                         ("Clear All",       self._tl_clear)):
            tk.Button(ctrl, text=txt, command=cmd,
                      bg=BG_CARD, fg=FG, activeforeground=ACCENT,
                      activebackground=BG_SEL, relief=tk.FLAT,
                      padx=8, pady=2, font=("", 8),
                      cursor="hand2").pack(side=tk.LEFT, padx=(8, 0))

        tk.Button(ctrl, text="◀", command=lambda: self._tl_move_sel(-1),
                  bg=BG_CARD, fg=FG_DIM, activeforeground=ACCENT,
                  activebackground=BG_SEL, relief=tk.FLAT,
                  padx=6, pady=2, font=("", 8),
                  cursor="hand2").pack(side=tk.LEFT, padx=(14, 0))
        tk.Button(ctrl, text="▶", command=lambda: self._tl_move_sel(+1),
                  bg=BG_CARD, fg=FG_DIM, activeforeground=ACCENT,
                  activebackground=BG_SEL, relief=tk.FLAT,
                  padx=6, pady=2, font=("", 8),
                  cursor="hand2").pack(side=tk.LEFT, padx=(2, 0))

        tk.Label(ctrl,
                 text="  click=select  drag=reorder  right-click=menu",
                 bg=BG_PANEL, fg=FG_DIM, font=("", 7)
                 ).pack(side=tk.LEFT, padx=10)

        cf = tk.Frame(parent, bg=BG_PANEL)
        cf.pack(fill=tk.BOTH, expand=True)

        hbar = tk.Scrollbar(cf, orient=tk.HORIZONTAL,
                            bg=BG_CARD, troughcolor=BG_PANEL, relief=tk.FLAT)
        hbar.pack(side=tk.BOTTOM, fill=tk.X)

        self._tl_canvas = tk.Canvas(cf, bg=BG_PANEL,
                                    xscrollcommand=hbar.set,
                                    highlightthickness=0)
        self._tl_canvas.pack(fill=tk.BOTH, expand=True)
        hbar.config(command=self._tl_canvas.xview)

        self._tl_inner = tk.Frame(self._tl_canvas, bg=BG_PANEL)
        self._tl_canvas.create_window((0, 0), window=self._tl_inner,
                                      anchor=tk.NW)
        self._tl_inner.bind(
            "<Configure>",
            lambda _: self._tl_canvas.configure(
                scrollregion=self._tl_canvas.bbox("all")))

        tk.Label(self._tl_inner,
                 text="Click or drag source frames above to build the animation",
                 bg=BG_PANEL, fg=FG_DIM, font=("", 9)).pack(pady=28)

    def _rebuild_timeline(self):
        for w in self._tl_inner.winfo_children():
            w.destroy()
        self._tl_cells.clear()
        self._tl_photos.clear()
        self._drop_ind = None

        if not self._items:
            tk.Label(self._tl_inner,
                     text="Click or drag source frames above to build the animation",
                     bg=BG_PANEL, fg=FG_DIM, font=("", 9)).pack(pady=28)
            self._cp_load()
            return

        all_pngs = [it.png for it in self._items]
        scale    = _thumb_scale(all_pngs, THUMB_TL_H)

        for i, item in enumerate(self._items):
            photo = _compose_thumb(item, scale)
            self._tl_photos.append(photo)

            card = tk.Frame(self._tl_inner, bg=BG_PANEL, padx=2, pady=4)
            card.grid(row=0, column=i, sticky=tk.N)

            badge = []
            if item.rotate:
                badge.append(f"{item.rotate}°")
            if item.skew_x:
                badge.append(f"sk{item.skew_x:+.0f}°")

            img_lbl = tk.Label(card, image=photo, bg=BG_CARD,
                               relief=tk.FLAT, borderwidth=2, cursor="hand2")
            img_lbl.pack()
            tk.Label(card, text=str(i),
                     bg=BG_PANEL, fg=FG_DIM, font=("", 7)).pack()
            tk.Label(card, text=item.anim_dir.name,
                     bg=BG_PANEL, fg=FG_DIM, font=("Consolas", 6)).pack()
            if badge:
                tk.Label(card, text=" ".join(badge),
                         bg=BG_PANEL, fg=YELLOW, font=("", 6)).pack()

            self._tl_cells.append(card)

            for w in (card, img_lbl):
                w.bind("<Button-1>",
                       lambda e, idx=i: self._tl_press(e, idx))
                w.bind("<B1-Motion>",
                       lambda e, idx=i: self._tl_motion(e, idx))
                w.bind("<ButtonRelease-1>",
                       lambda e, idx=i: self._tl_release(e, idx))
                w.bind("<Button-3>",
                       lambda e, idx=i: self._tl_right_click(e, idx))

        self._drop_ind = tk.Frame(self._tl_inner, bg=ACCENT, width=3)
        self._drop_ind.place_forget()

        self._tl_sel = {i for i in self._tl_sel if i < len(self._items)}
        self._tl_highlight()
        self._cp_load()

    # ── timeline interactions ─────────────────────────────────────────────────

    def _tl_press(self, event, idx: int):
        self._tl_drag_src    = idx
        self._tl_drag_active = False
        self._tl_drag_press  = (event.x_root, event.y_root)

    def _tl_motion(self, event, idx: int):  # noqa: ARG002
        dx = event.x_root - self._tl_drag_press[0]
        dy = event.y_root - self._tl_drag_press[1]
        if not self._tl_drag_active:
            if dx*dx + dy*dy < self._DRAG_THRESHOLD**2:
                return
            self._tl_drag_active = True
        self._show_drop_indicator(self._drop_index(event.x_root, event.y_root))

    def _tl_release(self, event, idx: int):
        self._hide_drop_indicator()

        if not self._tl_drag_active:
            if idx in self._tl_sel:
                self._tl_sel.discard(idx)
            else:
                self._tl_sel.add(idx)
            self._tl_highlight()
            self._tl_drag_src = None
            return

        self._tl_drag_active = False
        src = self._tl_drag_src
        self._tl_drag_src = None

        dst = self._drop_index(event.x_root, event.y_root)
        if dst < 0 or src is None or dst == src or dst == src + 1:
            return

        item = self._items.pop(src)
        if dst > src:
            dst -= 1
        self._items.insert(dst, item)
        self._tl_sel.clear()
        self._rebuild_timeline()

    def _tl_right_click(self, event, idx: int):
        item = self._items[idx]
        menu = tk.Menu(self._win, tearoff=False, bg=BG_CARD, fg=FG,
                       activebackground=BG_SEL, activeforeground=ACCENT)

        menu.add_command(label="Duplicate",
                         command=lambda: self._tl_duplicate(idx))
        menu.add_separator()

        # ── Rotate ───────────────────────────────────────────────────────────
        rot_menu = tk.Menu(menu, tearoff=False, bg=BG_CARD, fg=FG,
                           activebackground=BG_SEL, activeforeground=ACCENT)
        rot_menu.add_command(
            label=f"Rotate Left 90°  (now {item.rotate}°)",
            command=lambda: self._tl_rotate(idx, -90))
        rot_menu.add_command(
            label="Rotate Right 90°",
            command=lambda: self._tl_rotate(idx, +90))
        rot_menu.add_separator()
        rot_menu.add_command(label="Reset rotation",
                             command=lambda: self._tl_set_rotate(idx, 0))
        menu.add_cascade(label="Rotate", menu=rot_menu)

        # ── Skew ─────────────────────────────────────────────────────────────
        skew_menu = tk.Menu(menu, tearoff=False, bg=BG_CARD, fg=FG,
                            activebackground=BG_SEL, activeforeground=ACCENT)
        for deg in self._SKEW_PRESETS:
            skew_menu.add_command(
                label=f"{deg:+d}°",
                command=lambda d=deg: self._tl_set_skew(idx, d))
        skew_menu.add_separator()
        skew_menu.add_command(label="Custom…",
                              command=lambda: self._tl_skew_custom(idx))
        skew_menu.add_command(label="Reset skew",
                              command=lambda: self._tl_set_skew(idx, 0.0))
        menu.add_cascade(label=f"Skew  (now {item.skew_x:+.0f}°)", menu=skew_menu)

        menu.add_separator()
        if idx > 0:
            menu.add_command(label="Move Left",
                             command=lambda: self._tl_move_one(idx, idx - 1))
        if idx < len(self._items) - 1:
            menu.add_command(label="Move Right",
                             command=lambda: self._tl_move_one(idx, idx + 1))
        menu.add_separator()
        menu.add_command(label="Remove",
                         command=lambda: self._tl_remove_one(idx))

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _tl_duplicate(self, idx: int):
        self._items.insert(idx + 1, self._items[idx].copy())
        self._rebuild_timeline()

    def _tl_rotate(self, idx: int, delta: int):
        item = self._items[idx]
        item.rotate = (item.rotate + delta) % 360
        self._rebuild_timeline()

    def _tl_set_rotate(self, idx: int, degrees: int):
        self._items[idx].rotate = degrees % 360
        self._rebuild_timeline()

    def _tl_set_skew(self, idx: int, degrees: float):
        self._items[idx].skew_x = degrees
        self._rebuild_timeline()

    def _tl_skew_custom(self, idx: int):
        dlg = _InputDialog(self._win, "Custom Skew",
                           "Horizontal skew angle (degrees):",
                           str(self._items[idx].skew_x))
        if dlg.result is None:
            return
        try:
            self._items[idx].skew_x = float(dlg.result)
            self._rebuild_timeline()
        except ValueError:
            messagebox.showerror("Invalid", "Enter a number.", parent=self._win)

    def _tl_highlight(self):
        for i, card in enumerate(self._tl_cells):
            sel     = i in self._tl_sel
            card_bg = BG_SEL if sel else BG_PANEL
            card.config(bg=card_bg)
            for w in card.winfo_children():
                try:
                    img_w = bool(w.cget("image"))
                    w.config(bg=BG_SEL if sel else
                             (BG_CARD if img_w else BG_PANEL))
                except Exception:
                    pass

    def _tl_remove_one(self, idx: int):
        self._items.pop(idx)
        self._tl_sel.discard(idx)
        self._rebuild_timeline()

    def _tl_remove_sel(self):
        self._items = [it for i, it in enumerate(self._items)
                       if i not in self._tl_sel]
        self._tl_sel.clear()
        self._rebuild_timeline()

    def _tl_clear(self):
        self._items.clear()
        self._tl_sel.clear()
        self._rebuild_timeline()

    def _tl_move_one(self, src: int, dst: int):
        self._items.insert(dst, self._items.pop(src))
        self._tl_sel.clear()
        self._rebuild_timeline()

    def _tl_move_sel(self, direction: int):
        indices = sorted(self._tl_sel, reverse=(direction > 0))
        n = len(self._items)
        for i in indices:
            j = i + direction
            if 0 <= j < n:
                self._items[i], self._items[j] = self._items[j], self._items[i]
                self._tl_sel.discard(i)
                self._tl_sel.add(j)
        self._rebuild_timeline()

    # ── drop indicator ────────────────────────────────────────────────────────

    def _drop_index(self, abs_x: int, abs_y: int) -> int:
        cv = self._tl_canvas
        if cv is None or not cv.winfo_exists():
            return -1
        cx, cy = cv.winfo_rootx(), cv.winfo_rooty()
        cw, ch = cv.winfo_width(), cv.winfo_height()
        if not (cx <= abs_x <= cx + cw and cy <= abs_y <= cy + ch):
            return -1
        n = len(self._tl_cells)
        if n == 0:
            return 0
        scroll_frac = cv.xview()[0]
        mouse_inner = (abs_x - cx) + scroll_frac * self._tl_inner.winfo_width()
        for i, card in enumerate(self._tl_cells):
            if mouse_inner < card.winfo_x() + card.winfo_width() // 2:
                return i
        return n

    def _show_drop_indicator(self, idx: int):
        ind = self._drop_ind
        if ind is None or not ind.winfo_exists():
            return
        if idx < 0:
            ind.place_forget()
            return
        h = max(THUMB_TL_H + 36, self._tl_inner.winfo_height() or 1)
        n = len(self._tl_cells)
        if n == 0:
            x = 4
        elif idx < n:
            x = self._tl_cells[idx].winfo_x() - 2
        else:
            last = self._tl_cells[-1]
            x    = last.winfo_x() + last.winfo_width() + 2
        ind.place(x=x, y=0, width=3, height=h)
        ind.lift()

    def _hide_drop_indicator(self):
        if self._drop_ind and self._drop_ind.winfo_exists():
            self._drop_ind.place_forget()

    # ── preview panel ─────────────────────────────────────────────────────────

    def _build_preview(self, parent: tk.Frame):
        tk.Label(parent, text="Preview", bg=BG_PANEL, fg=ACCENT,
                 font=("", 9, "bold"), pady=4).pack(fill=tk.X, padx=8)

        self._cp_canvas = tk.Canvas(parent, bg=BG_PANEL,
                                    highlightthickness=0)
        self._cp_canvas.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)
        self._cp_canvas.bind("<Configure>", lambda _: self._cp_render())

        ctrl = tk.Frame(parent, bg=BG_PANEL, pady=4)
        ctrl.pack(fill=tk.X, padx=4)

        self._cp_btn_play = tk.Button(
            ctrl, text="▶", command=self._cp_toggle,
            bg=BG_CARD, fg=GREEN, activeforeground=GREEN,
            activebackground=BG_SEL, relief=tk.FLAT,
            padx=6, pady=2, font=("", 9), cursor="hand2")
        self._cp_btn_play.pack(side=tk.LEFT)

        tk.Button(ctrl, text="■", command=self._cp_stop,
                  bg=BG_CARD, fg=RED, activeforeground=RED,
                  activebackground=BG_SEL, relief=tk.FLAT,
                  padx=6, pady=2, font=("", 9),
                  cursor="hand2").pack(side=tk.LEFT, padx=(2, 6))

        self._cp_lbl_counter = tk.Label(ctrl, text="— / —",
                                        bg=BG_PANEL, fg=FG_DIM,
                                        font=("Consolas", 8), width=7)
        self._cp_lbl_counter.pack(side=tk.LEFT)

        tk.Checkbutton(ctrl, text="Loop", variable=self._cp_loop,
                       bg=BG_PANEL, fg=FG, selectcolor=BG_CARD,
                       activebackground=BG_PANEL, activeforeground=FG,
                       relief=tk.FLAT, borderwidth=0).pack(side=tk.LEFT, padx=4)

        sldr = tk.Frame(parent, bg=BG_PANEL, pady=2)
        sldr.pack(fill=tk.X, padx=6, pady=(0, 4))
        tk.Label(sldr, text="Delay", bg=BG_PANEL, fg=FG_DIM,
                 font=("", 7)).pack(side=tk.LEFT)
        tk.Scale(sldr, variable=self._cp_delay,
                 from_=20, to=2000, resolution=10, orient=tk.HORIZONTAL,
                 bg=BG_PANEL, fg=FG, troughcolor=BG_CARD,
                 highlightthickness=0, showvalue=True,
                 ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

    def _cp_load(self):
        """Called from _rebuild_timeline to sync the preview."""
        self._cp_stop()
        mw = mh = 1
        for item in self._items:
            try:
                img = Image.open(item.png).convert("RGBA")
                if item.rotate or item.skew_x:
                    img = _apply_transform(img, item.rotate, item.skew_x)
                mw = max(mw, img.width)
                mh = max(mh, img.height)
            except Exception:
                pass
        self._cp_max_w = mw
        self._cp_max_h = mh
        self._cp_current = 0
        self._cp_render()

    def _cp_render(self):
        cv = self._cp_canvas
        if cv is None or not cv.winfo_exists():
            return
        cv.delete("all")
        if not self._items:
            cv.create_text(cv.winfo_width() // 2 or 60,
                           cv.winfo_height() // 2 or 60,
                           text="No frames", fill=FG_DIM, font=("", 9))
            if self._cp_lbl_counter and self._cp_lbl_counter.winfo_exists():
                self._cp_lbl_counter.config(text="— / —")
            return

        idx = min(self._cp_current, len(self._items) - 1)
        try:
            img = Image.open(self._items[idx].png).convert("RGBA")
            if self._items[idx].rotate or self._items[idx].skew_x:
                img = _apply_transform(img,
                                       self._items[idx].rotate,
                                       self._items[idx].skew_x)
        except Exception:
            return

        cw = cv.winfo_width()  or 240
        ch = cv.winfo_height() or 240
        max_w = self._cp_max_w or img.width
        max_h = self._cp_max_h or img.height
        scale = max(1.0, min(MAX_SCALE, min(cw / max_w, ch / max_h)))

        w = max(1, round(img.width  * scale))
        h = max(1, round(img.height * scale))
        img = img.resize((w, h), Image.NEAREST)

        bg_hex = cv.cget("bg")
        rgb    = tuple(int(bg_hex.lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
        flat   = Image.new("RGBA", (w, h), (*rgb, 255))
        flat   = Image.alpha_composite(flat, img)
        self._cp_photo = ImageTk.PhotoImage(flat)

        ax = cw // 2 - round(max_w * scale) // 2
        ay = ch // 2 + round(max_h * scale) // 2
        cv.create_image(ax, ay, image=self._cp_photo, anchor=tk.SW)

        n = len(self._items)
        if self._cp_lbl_counter and self._cp_lbl_counter.winfo_exists():
            self._cp_lbl_counter.config(text=f"{idx + 1} / {n}")

    def _cp_tick(self):
        if not self._cp_playing or not self._items:
            return
        nxt = self._cp_current + 1
        if nxt >= len(self._items):
            if self._cp_loop.get():
                nxt = 0
            else:
                self._cp_pause()
                return
        self._cp_current = nxt
        self._cp_render()
        if self._cp_canvas and self._cp_canvas.winfo_exists():
            self._cp_after_id = self._win.after(
                self._cp_delay.get(), self._cp_tick)

    def _cp_toggle(self):
        if self._cp_playing:
            self._cp_pause()
        else:
            self._cp_play()

    def _cp_play(self):
        if not self._items:
            return
        self._cp_playing = True
        if self._cp_btn_play and self._cp_btn_play.winfo_exists():
            self._cp_btn_play.config(text="⏸")
        self._cp_tick()

    def _cp_pause(self):
        self._cp_playing = False
        if self._cp_btn_play and self._cp_btn_play.winfo_exists():
            self._cp_btn_play.config(text="▶")
        if self._cp_after_id:
            try:
                self._win.after_cancel(self._cp_after_id)
            except Exception:
                pass
            self._cp_after_id = None

    def _cp_stop(self):
        self._cp_pause()
        self._cp_current = 0
        self._cp_render()

    # ── save ──────────────────────────────────────────────────────────────────

    def _save(self):
        import shutil as _shutil

        name = self._v_name.get().strip()
        if not name:
            messagebox.showwarning("No Name",
                                   "Enter a name for the new animation.",
                                   parent=self._win)
            return
        if not self._items:
            messagebox.showwarning("Empty Timeline",
                                   "Add at least one frame to the timeline first.",
                                   parent=self._win)
            return

        out_dir = self._output_dir / name
        if out_dir.exists():
            if not messagebox.askyesno("Overwrite?",
                                       f"'{name}' already exists. Overwrite it?",
                                       parent=self._win):
                return
            _shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)

        new_frames = []
        for i, item in enumerate(self._items):
            dst = out_dir / f"{i:03d}.png"
            if item.rotate or item.skew_x:
                img = Image.open(item.png).convert("RGBA")
                img = _apply_transform(img, item.rotate, item.skew_x)
                img.save(dst)
            else:
                _shutil.copy2(item.png, dst)

            blobs = []
            try:
                meta = json.loads(
                    (item.anim_dir / "frames.json").read_text(encoding="utf-8"))
                hit = next((f for f in meta["frames"]
                            if Path(f["file"]).stem == item.png.stem), None)
                if hit:
                    blobs = hit["blobs"]
            except Exception:
                pass

            new_frames.append({"index": i, "file": f"{i:03d}.png",
                                "blobs": blobs})

        (out_dir / "frames.json").write_text(
            json.dumps({"gif": "", "bg": [0, 0, 0], "tol": 20,
                        "frames": new_frames}, indent=2),
            encoding="utf-8")

        self._on_save()
        messagebox.showinfo("Saved",
                            f"'{name}' saved with {len(self._items)} frames.",
                            parent=self._win)

    def _close(self):
        self._cp_pause()
        self._destroy_ghost()
        try:
            self._win.destroy()
        except Exception:
            pass
