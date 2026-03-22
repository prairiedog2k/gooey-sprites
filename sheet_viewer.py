"""SheetViewerWindow — zoomable sprite sheet viewer."""

import tkinter as tk
from pathlib import Path

from PIL import Image, ImageTk

from constants import BG, BG_PANEL, BG_CARD, BG_SEL, FG, FG_DIM, ACCENT


class SheetViewerWindow:
    _MIN_ZOOM  = 0.05
    _MAX_ZOOM  = 16.0
    _ZOOM_STEP = 1.25

    def __init__(self, parent: tk.Tk, sheet_path: Path):
        self._path = sheet_path
        self._zoom = 1.0
        self._photo: ImageTk.PhotoImage | None = None

        self._win = tk.Toplevel(parent)
        self._win.title(f"Sprite Sheet  —  {sheet_path.name}")
        self._win.configure(bg=BG)
        self._win.geometry("900x660")
        self._win.minsize(400, 300)

        try:
            raw = Image.open(sheet_path)
            try:
                raw.seek(0)   # use first frame for animated GIFs
            except Exception:
                pass
            self._img = raw.convert("RGBA")
        except Exception as exc:
            tk.messagebox.showerror("Sprite Sheet Viewer",
                                    f"Cannot open image:\n{exc}", parent=parent)
            self._win.destroy()
            return

        self._build_ui()
        # Delay fit-zoom until after the window has been laid out
        self._win.after(80, self._zoom_fit)

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Toolbar
        bar = tk.Frame(self._win, bg=BG_PANEL, pady=4, padx=8)
        bar.pack(fill=tk.X)

        def _btn(parent, text, cmd, font_size=9):
            return tk.Button(parent, text=text, command=cmd,
                             bg=BG_CARD, fg=FG,
                             activebackground=BG_SEL, activeforeground=ACCENT,
                             relief=tk.FLAT, borderwidth=0,
                             highlightthickness=0, cursor="hand2",
                             padx=8, pady=2, font=("", font_size))

        _btn(bar, "−",   self._zoom_out, font_size=11).pack(side=tk.LEFT)
        self._zoom_lbl = tk.Label(bar, text="100%", width=6,
                                  bg=BG_PANEL, fg=FG, font=("Consolas", 9))
        self._zoom_lbl.pack(side=tk.LEFT, padx=2)
        _btn(bar, "+",    self._zoom_in,    font_size=11).pack(side=tk.LEFT)
        _btn(bar, "Fit",  self._zoom_fit).pack(side=tk.LEFT, padx=(12, 2))
        _btn(bar, "1:1",  self._zoom_actual).pack(side=tk.LEFT, padx=2)

        w, h = self._img.size
        ext = self._path.suffix.upper().lstrip(".")
        tk.Label(bar, text=f"{w} × {h} px  ·  {ext}",
                 bg=BG_PANEL, fg=FG_DIM, font=("", 8)).pack(side=tk.RIGHT, padx=8)

        # Canvas area
        cf = tk.Frame(self._win, bg=BG)
        cf.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

        hbar = tk.Scrollbar(cf, orient=tk.HORIZONTAL,
                            bg=BG_CARD, troughcolor=BG_PANEL, relief=tk.FLAT)
        vbar = tk.Scrollbar(cf, orient=tk.VERTICAL,
                            bg=BG_CARD, troughcolor=BG_PANEL, relief=tk.FLAT)
        self._canvas = tk.Canvas(cf, bg="#1a1a2e", highlightthickness=0,
                                 xscrollcommand=hbar.set,
                                 yscrollcommand=vbar.set)
        hbar.config(command=self._canvas.xview)
        vbar.config(command=self._canvas.yview)
        hbar.pack(side=tk.BOTTOM, fill=tk.X)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.pack(fill=tk.BOTH, expand=True)

        # Pan via left-button drag; zoom via scroll wheel
        self._canvas.bind("<ButtonPress-1>",
                          lambda e: self._canvas.scan_mark(e.x, e.y))
        self._canvas.bind("<B1-Motion>",
                          lambda e: self._canvas.scan_dragto(e.x, e.y, gain=1))
        self._canvas.bind("<MouseWheel>", self._on_wheel)

    # ── rendering ────────────────────────────────────────────────────────────

    def _render(self):
        w = max(1, round(self._img.width  * self._zoom))
        h = max(1, round(self._img.height * self._zoom))
        resample = Image.NEAREST if self._zoom >= 2.0 else Image.LANCZOS
        self._photo = ImageTk.PhotoImage(self._img.resize((w, h), resample))
        c = self._canvas
        c.delete("all")
        c.create_image(0, 0, image=self._photo, anchor=tk.NW)
        c.configure(scrollregion=(0, 0, w, h))
        self._zoom_lbl.config(text=f"{self._zoom * 100:.0f}%")

    # ── zoom helpers ─────────────────────────────────────────────────────────

    def _set_zoom(self, zoom: float):
        self._zoom = max(self._MIN_ZOOM, min(self._MAX_ZOOM, zoom))
        self._render()

    def _zoom_in(self):     self._set_zoom(self._zoom * self._ZOOM_STEP)
    def _zoom_out(self):    self._set_zoom(self._zoom / self._ZOOM_STEP)
    def _zoom_actual(self): self._set_zoom(1.0)

    def _zoom_fit(self):
        cw = self._canvas.winfo_width()  or 800
        ch = self._canvas.winfo_height() or 560
        iw, ih = self._img.size
        self._set_zoom(min(cw / iw, ch / ih))

    def _on_wheel(self, event):
        """Zoom toward the cursor position."""
        # Canvas coordinate under the mouse before zoom
        mx = self._canvas.canvasx(event.x)
        my = self._canvas.canvasy(event.y)

        old_zoom = self._zoom
        if event.delta > 0:
            new_zoom = min(self._MAX_ZOOM, self._zoom * self._ZOOM_STEP)
        else:
            new_zoom = max(self._MIN_ZOOM, self._zoom / self._ZOOM_STEP)
        if new_zoom == old_zoom:
            return

        self._zoom = new_zoom
        self._render()

        # Scroll so the image pixel under the mouse stays under the mouse
        ratio = new_zoom / old_zoom
        iw = self._img.width  * new_zoom
        ih = self._img.height * new_zoom
        self._canvas.xview_moveto(max(0.0, (mx * ratio - event.x) / iw))
        self._canvas.yview_moveto(max(0.0, (my * ratio - event.y) / ih))
