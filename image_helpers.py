"""Thumbnail helpers, per-frame compose data, and image transform utilities."""

import math as _math
from pathlib import Path

from PIL import Image, ImageDraw, ImageTk

from constants import CHECKER, MIN_SCALE, MAX_SCALE


def _make_thumb(png_path: Path, scale: float) -> ImageTk.PhotoImage:
    """Load a PNG and scale it by `scale`, composited over a checker background."""
    img = Image.open(png_path).convert("RGBA")
    w = max(1, round(img.width  * scale))
    h = max(1, round(img.height * scale))
    img = img.resize((w, h), Image.NEAREST)

    checker = Image.new("RGBA", (w, h))
    bsize   = max(8, h // 8)
    draw    = ImageDraw.Draw(checker)
    for row in range(0, h, bsize):
        for col in range(0, w, bsize):
            color = CHECKER[(row // bsize + col // bsize) % 2]
            draw.rectangle([col, row, col + bsize - 1, row + bsize - 1], fill=color)
    return ImageTk.PhotoImage(Image.alpha_composite(checker, img))


def _thumb_scale(pngs: list, target_h: int) -> float:
    """Return a uniform scale so the tallest frame reaches target_h px."""
    if not pngs:
        return MIN_SCALE
    max_h = max(Image.open(p).height for p in pngs)
    return max(MIN_SCALE, min(MAX_SCALE, target_h / max(max_h, 1)))


class _CItem:
    """One frame slot in the compose timeline."""
    __slots__ = ("anim_dir", "png", "rotate", "skew_x")

    def __init__(self, anim_dir: Path, png: Path,
                 rotate: int = 0, skew_x: float = 0.0):
        self.anim_dir = anim_dir   # source animation folder
        self.png      = png        # source PNG path
        self.rotate   = rotate     # CW degrees: 0, 90, 180, 270
        self.skew_x   = skew_x     # horizontal shear angle in degrees

    def copy(self) -> "_CItem":
        return _CItem(self.anim_dir, self.png, self.rotate, self.skew_x)


def _apply_transform(img: Image.Image, rotate: int,
                     skew_x: float) -> Image.Image:
    """Return a new RGBA image with CW rotation and horizontal skew applied."""
    out = img.convert("RGBA")
    if rotate:
        out = out.rotate(-rotate, expand=True, resample=Image.BICUBIC)
    if skew_x:
        k     = _math.tan(_math.radians(skew_x))
        w, h  = out.size
        extra = int(abs(k) * h) + 1
        new_w = w + extra
        ox    = extra if k < 0 else 0
        out   = out.transform(
            (new_w, h), Image.AFFINE,
            (1, -k, ox, 0, 1, 0),
            resample=Image.BILINEAR,
            fillcolor=(0, 0, 0, 0))
    return out


def _compose_thumb(item: _CItem, scale: float) -> ImageTk.PhotoImage:
    """Load, transform, and render a checker-background thumbnail."""
    img = Image.open(item.png).convert("RGBA")
    if item.rotate or item.skew_x:
        img = _apply_transform(img, item.rotate, item.skew_x)
    w = max(1, round(img.width  * scale))
    h = max(1, round(img.height * scale))
    img = img.resize((w, h), Image.NEAREST)

    checker = Image.new("RGBA", (w, h))
    bsize   = max(8, h // 8)
    draw    = ImageDraw.Draw(checker)
    for row in range(0, h, bsize):
        for col in range(0, w, bsize):
            color = CHECKER[(row // bsize + col // bsize) % 2]
            draw.rectangle([col, row, col + bsize - 1, row + bsize - 1],
                           fill=color)
    return ImageTk.PhotoImage(Image.alpha_composite(checker, img))
