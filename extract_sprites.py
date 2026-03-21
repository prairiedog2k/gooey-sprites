#!/usr/bin/env python3
"""
extract_sprites.py

Extracts individual sprites from a sprite-sheet GIF.

Structure assumed:
  - Full-width white horizontal lines divide the sheet into animation rows.
  - Within each row, full-height white vertical lines divide it into cells.
  - Each cell may contain a white text label near the top; sprite pixels are
    coloured (non-white, non-background) so the label is automatically excluded.
  - Background is a solid color not present in any sprite.

Modes
-----
  # List all detected animation cells
  python extract_sprites.py sheet.gif

  # Extract every animation (folders named unknown-001, unknown-002, …)
  python extract_sprites.py sheet.gif --all -o ./out

  # Manually stitch two frames that were incorrectly split
  python extract_sprites.py --stitch ./out/unknown-001 0 1

Dependencies:
  pip install pillow numpy
"""

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image


# ── Colour helpers ─────────────────────────────────────────────────────────────

def detect_bg_color(img: Image.Image) -> tuple[int, int, int]:
    from collections import Counter
    rgba = img.convert("RGBA")
    w, h = rgba.size
    corners = [rgba.getpixel((0,0)), rgba.getpixel((w-1,0)),
               rgba.getpixel((0,h-1)), rgba.getpixel((w-1,h-1))]
    return Counter(c[:3] for c in corners).most_common(1)[0][0]


def sprite_fg_mask(arr: np.ndarray, bg: tuple, tol: int) -> np.ndarray:
    """True for genuine sprite pixels (not background, not white border)."""
    diff = np.abs(arr[:,:,:3].astype(np.int32) - np.array(bg, dtype=np.int32))
    is_bg     = diff.max(axis=2) <= tol
    is_border = (arr[:,:,0] > 200) & (arr[:,:,1] > 200) & (arr[:,:,2] > 200)
    return ~is_bg & ~is_border


# ── Separator detection ────────────────────────────────────────────────────────

def _merge_runs(indices: list[int]) -> list[tuple[int,int]]:
    if not indices:
        return []
    runs, cur = [], [indices[0]]
    for v in indices[1:]:
        if v == cur[-1] + 1:
            cur.append(v)
        else:
            runs.append((cur[0], cur[-1]))
            cur = [v]
    runs.append((cur[0], cur[-1]))
    return runs


def find_horizontal_separators(arr: np.ndarray,
                                threshold: int = 200,
                                min_coverage: float = 0.4) -> list[tuple[int,int]]:
    bright = (arr[:,:,0]>threshold) & (arr[:,:,1]>threshold) & (arr[:,:,2]>threshold)
    rows = [int(y) for y in np.where(bright.mean(axis=1) >= min_coverage)[0]]
    return _merge_runs(rows)


def find_vertical_separators_in_band(arr: np.ndarray, y0: int, y1: int,
                                      threshold: int = 200,
                                      min_coverage: float = 0.4) -> list[tuple[int,int]]:
    band  = arr[y0:y1]
    bright = (band[:,:,0]>threshold) & (band[:,:,1]>threshold) & (band[:,:,2]>threshold)
    cols  = [int(x) for x in np.where(bright.mean(axis=0) >= min_coverage)[0]]
    return _merge_runs(cols)


def separators_to_gaps(runs: list[tuple[int,int]], total: int) -> list[tuple[int,int]]:
    gaps, prev = [], 0
    for s0, s1 in runs:
        if s0 > prev:
            gaps.append((prev, s0))
        prev = s1 + 1
    if prev < total:
        gaps.append((prev, total))
    return gaps


# ── Sprite segmentation ────────────────────────────────────────────────────────

def find_sprite_y_start(arr: np.ndarray,
                         x0: int, x1: int, y0: int, y1: int,
                         bg: tuple, tol: int) -> int:
    for y in range(y0, y1):
        if sprite_fg_mask(arr[y:y+1, x0:x1], bg, tol).any():
            return y
    return y1


def segment_sprites(arr: np.ndarray,
                    x0: int, x1: int, y0: int, y1: int,
                    bg: tuple, tol: int,
                    min_w: int = 10, min_h: int = 10) -> list[tuple[int,int,int,int]]:
    """Return (sx0,sy0,sx1,sy1) blobs in full-image coordinates, left-to-right."""
    mask       = sprite_fg_mask(arr[y0:y1, x0:x1], bg, tol)
    col_has_fg = mask.any(axis=0)
    sprites, in_sprite, sx = [], False, 0
    for cx, fg in enumerate(col_has_fg):
        if fg and not in_sprite:
            sx, in_sprite = cx, True
        elif not fg and in_sprite:
            strip = mask[:, sx:cx]
            rows  = np.where(strip.any(axis=1))[0]
            w, h  = cx - sx, (rows[-1] - rows[0] + 1) if rows.size else 0
            if rows.size and w >= min_w and h >= min_h:
                sprites.append((x0+sx, y0+rows[0], x0+cx, y0+rows[-1]+1))
            in_sprite = False
    if in_sprite:
        strip = mask[:, sx:]
        rows  = np.where(strip.any(axis=1))[0]
        w, h  = len(col_has_fg) - sx, (rows[-1] - rows[0] + 1) if rows.size else 0
        if rows.size and w >= min_w and h >= min_h:
            sprites.append((x0+sx, y0+rows[0], x0+len(col_has_fg), y0+rows[-1]+1))
    return sprites


# ── Compositing ────────────────────────────────────────────────────────────────

Blob  = tuple[int, int, int, int]   # (sx0, sy0, sx1, sy1) in sheet coordinates
Frame = list[Blob]                   # one or more blobs that form one output frame


def _compose_frame(blobs: Frame, arr: np.ndarray,
                   bg: tuple, tol: int) -> Image.Image:
    """Render a list of blobs onto a transparent canvas at their correct relative positions."""
    left   = min(b[0] for b in blobs)
    top    = min(b[1] for b in blobs)
    right  = max(b[2] for b in blobs)
    bottom = max(b[3] for b in blobs)
    canvas = np.zeros((bottom - top, right - left, 4), dtype=np.uint8)
    for sx0, sy0, sx1, sy1 in blobs:
        piece      = arr[sy0:sy1, sx0:sx1].copy()
        fg         = sprite_fg_mask(piece, bg, tol)
        piece[~fg] = [0, 0, 0, 0]
        dx, dy     = sx0 - left, sy0 - top
        dst        = canvas[dy:dy+(sy1-sy0), dx:dx+(sx1-sx0)]
        dst[fg]    = piece[fg]
    return Image.fromarray(canvas, "RGBA")


def stitch_frames(boxes: list[Blob],
                  arr: np.ndarray,
                  bg: tuple, tol: int,
                  max_intra_gap: int = 4) -> tuple[list[Image.Image], list[Frame]]:
    """
    Group raw blobs into frames: consecutive blobs whose horizontal gap on the
    sheet is <= max_intra_gap are considered parts of the same frame.

    Returns (images, frames) where frames[i] is the list of blobs for image i.
    The blob lists are the metadata needed for manual re-stitching later.
    """
    if not boxes:
        return [], []
    groups: list[Frame] = [[boxes[0]]]
    for blob in boxes[1:]:
        gap = blob[0] - groups[-1][-1][2]
        if gap <= max_intra_gap:
            groups[-1].append(blob)
        else:
            groups.append([blob])
    images = [_compose_frame(g, arr, bg, tol) for g in groups]
    return images, groups


# ── Metadata I/O ───────────────────────────────────────────────────────────────

FRAMES_JSON = "frames.json"


def save_metadata(out_dir: Path, gif_path: str,
                  bg: tuple, tol: int, frames: list[Frame]) -> None:
    """Save frames.json alongside the extracted PNGs."""
    data = {
        "gif": str(Path(gif_path).resolve()),
        "bg":  list(bg),
        "tol": tol,
        "frames": [
            {
                "index": i,
                "file":  f"{i:03d}.png",
                "blobs": [{"x0": int(b[0]), "y0": int(b[1]),
                            "x1": int(b[2]), "y1": int(b[3])}
                           for b in frame],
            }
            for i, frame in enumerate(frames)
        ],
    }
    (out_dir / FRAMES_JSON).write_text(json.dumps(data, indent=2))


def load_metadata(out_dir: Path) -> dict:
    p = out_dir / FRAMES_JSON
    if not p.exists():
        sys.exit(f"No {FRAMES_JSON} found in '{out_dir}'. Run extraction first.")
    return json.loads(p.read_text())


# ── Manual stitch command ──────────────────────────────────────────────────────

def cmd_stitch(out_dir: Path, frame_indices: list[int]) -> None:
    """
    Merge the specified frame indices into a single frame, using the stored
    sheet coordinates to compose them at their correct relative positions.
    The result replaces the lowest-indexed frame; others are removed.
    Remaining frames are renumbered sequentially.
    """
    meta   = load_metadata(out_dir)
    n      = len(meta["frames"])
    bad    = [i for i in frame_indices if i < 0 or i >= n]
    if bad:
        sys.exit(f"Frame indices {bad} out of range (0–{n-1}).")
    if len(frame_indices) < 2:
        sys.exit("Specify at least two frame indices to stitch.")

    gif_path = meta["gif"]
    bg       = tuple(meta["bg"])
    tol      = meta["tol"]

    arr = np.array(Image.open(gif_path).convert("RGBA"))

    # Collect all blobs from the frames to merge
    all_blobs: Frame = []
    for idx in sorted(frame_indices):
        all_blobs.extend(
            (b["x0"], b["y0"], b["x1"], b["y1"])
            for b in meta["frames"][idx]["blobs"]
        )

    stitched = _compose_frame(all_blobs, arr, bg, tol)

    keep_idx  = min(frame_indices)
    keep_file = out_dir / meta["frames"][keep_idx]["file"]
    stitched.save(keep_file)
    print(f"  Stitched -> {keep_file}  ({stitched.width}×{stitched.height})")

    # Remove the other frames from disk and metadata
    remove = set(frame_indices) - {keep_idx}
    for idx in remove:
        p = out_dir / meta["frames"][idx]["file"]
        p.unlink(missing_ok=True)

    # Update blobs for the kept frame
    meta["frames"][keep_idx]["blobs"] = [
        {"x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3]} for b in all_blobs
    ]

    # Drop removed frames from the list
    meta["frames"] = [f for i, f in enumerate(meta["frames"]) if i not in remove]

    # Renumber: rename PNGs and update metadata
    for new_idx, frame in enumerate(meta["frames"]):
        old_path = out_dir / frame["file"]
        new_file = f"{new_idx:03d}.png"
        new_path = out_dir / new_file
        if old_path != new_path and old_path.exists():
            old_path.rename(new_path)
        frame["index"] = new_idx
        frame["file"]  = new_file

    (out_dir / FRAMES_JSON).write_text(json.dumps(meta, indent=2))
    print(f"  frames.json updated — {len(meta['frames'])} frames remain.")


# ── Manual split command ───────────────────────────────────────────────────────

def cmd_split(out_dir: Path, frame_idx: int,
              split_x: Optional[int] = None) -> None:
    """
    Split one frame into two or more frames.

    No split_x → un-stitch: each stored blob becomes its own frame.
                 Requires the frame to have been auto-stitched from ≥ 2 blobs.

    split_x    → pixel-x split: cut the frame at that x position (frame-local
                 coordinates, 0 = left edge of the frame canvas).  Two synthetic
                 blobs are created from the sheet coordinates so future stitching
                 still works.
    """
    meta = load_metadata(out_dir)
    n = len(meta["frames"])
    if frame_idx < 0 or frame_idx >= n:
        sys.exit(f"Frame index {frame_idx} out of range (0-{n-1}).")

    frame_meta = meta["frames"][frame_idx]
    blobs: list[Blob] = [
        (b["x0"], b["y0"], b["x1"], b["y1"]) for b in frame_meta["blobs"]
    ]

    gif_path = meta["gif"]
    bg       = tuple(meta["bg"])
    tol      = meta["tol"]
    arr      = np.array(Image.open(gif_path).convert("RGBA"))

    # ── decide how to split ───────────────────────────────────────────────────
    if split_x is None:
        # Un-stitch: explode each stored blob into its own frame
        if len(blobs) == 1:
            sys.exit(
                f"Frame {frame_idx} has only one blob — nothing to un-stitch.\n"
                f"To cut at a pixel position supply an x coordinate:\n"
                f"  --split {out_dir} {frame_idx} <x>"
            )
        new_blob_groups: list[Frame] = [[b] for b in blobs]
        print(f"Un-stitching frame {frame_idx} into {len(new_blob_groups)} blobs.")
    else:
        # Pixel-x split — compute sheet-space split column
        frame_left  = min(b[0] for b in blobs)
        frame_right = max(b[2] for b in blobs)
        frame_top   = min(b[1] for b in blobs)
        frame_bot   = max(b[3] for b in blobs)
        frame_w     = frame_right - frame_left

        if not (0 < split_x < frame_w):
            sys.exit(
                f"x={split_x} is outside the frame (width={frame_w}). "
                f"Choose a value between 1 and {frame_w - 1}."
            )
        sheet_x = frame_left + split_x

        # Partition existing blobs: blobs whose centre is left of split → left group,
        # blobs straddling the split are themselves split at sheet_x.
        left_blobs: Frame  = []
        right_blobs: Frame = []
        for bx0, by0, bx1, by1 in blobs:
            if bx1 <= sheet_x:
                left_blobs.append((bx0, by0, bx1, by1))
            elif bx0 >= sheet_x:
                right_blobs.append((bx0, by0, bx1, by1))
            else:                                          # blob straddles the cut
                left_blobs.append((bx0,    by0, sheet_x, by1))
                right_blobs.append((sheet_x, by0, bx1,   by1))

        if not left_blobs:
            left_blobs = [(frame_left, frame_top, sheet_x, frame_bot)]
        if not right_blobs:
            right_blobs = [(sheet_x, frame_top, frame_right, frame_bot)]

        new_blob_groups = [left_blobs, right_blobs]
        print(f"Splitting frame {frame_idx} at local x={split_x} "
              f"(sheet x={sheet_x}).")

    # ── render new frames and discard empty ones ──────────────────────────────
    rendered: list[tuple[Image.Image, Frame]] = []
    for bg_group in new_blob_groups:
        img = _compose_frame(bg_group, arr, bg, tol)
        if np.array(img)[:, :, 3].any():          # skip fully-transparent pieces
            rendered.append((img, bg_group))

    if not rendered:
        sys.exit("All pieces are empty — check your x value.")

    n_new  = len(rendered)
    shift  = n_new - 1          # frames after the split shift right by this much

    # ── rename after-frames in reverse to avoid collisions ───────────────────
    if shift > 0:
        for old_f in reversed(meta["frames"][frame_idx + 1:]):
            old_path = out_dir / old_f["file"]
            new_path = out_dir / f"{old_f['index'] + shift:03d}.png"
            if old_path.exists():
                old_path.rename(new_path)

    # ── remove original frame file ────────────────────────────────────────────
    (out_dir / frame_meta["file"]).unlink(missing_ok=True)

    # ── save new pieces ───────────────────────────────────────────────────────
    for j, (img, _) in enumerate(rendered):
        path = out_dir / f"{frame_idx + j:03d}.png"
        img.save(path)
        print(f"  {path}  ({img.width}x{img.height})")

    # ── rebuild metadata ──────────────────────────────────────────────────────
    new_frames: list[dict] = []
    for i, f in enumerate(meta["frames"]):
        if i == frame_idx:
            for j, (_, fb) in enumerate(rendered):
                new_frames.append({
                    "index": frame_idx + j,
                    "file":  f"{frame_idx + j:03d}.png",
                    "blobs": [{"x0": int(b[0]), "y0": int(b[1]),
                               "x1": int(b[2]), "y1": int(b[3])}
                              for b in fb],
                })
        else:
            new_idx = i if i < frame_idx else i + shift
            new_frames.append({**f, "index": new_idx, "file": f"{new_idx:03d}.png"})

    meta["frames"] = new_frames
    (out_dir / FRAMES_JSON).write_text(json.dumps(meta, indent=2))
    print(f"  frames.json updated — {len(new_frames)} frames total.")


# ── Grid builder ───────────────────────────────────────────────────────────────

@dataclass
class Cell:
    name: str
    x0: int
    y0: int
    x1: int
    y1: int
    sprite_y0: int = 0


class SpriteSheet:
    def __init__(self, gif_path: str, tol: int = 20):
        self.path = gif_path
        self.tol  = tol
        img       = Image.open(gif_path)
        self.img       = img.convert("RGBA")
        self.arr       = np.array(self.img)
        self.bg        = detect_bg_color(self.img)
        self._cells: Optional[list[Cell]] = None

    def cells(self) -> list[Cell]:
        if self._cells is None:
            self._cells = self._build_cells()
        return self._cells

    def animation_names(self) -> list[str]:
        return [c.name for c in self.cells() if c.name]

    def extract(self, name: str,
                max_intra_gap: int = 4) -> tuple[list[Image.Image], list[Frame]]:
        cell = self._find_cell(name)
        if cell is None:
            avail = ", ".join(self.animation_names())
            raise KeyError(f"Animation '{name}' not found.\nAvailable: {avail}")
        return self._extract_cell(cell, max_intra_gap)

    def extract_all(self, max_intra_gap: int = 4) -> list[tuple[Cell, list[Image.Image], list[Frame]]]:
        results = []
        for cell in self.cells():
            images, frames = self._extract_cell(cell, max_intra_gap)
            if images:
                results.append((cell, images, frames))
        return results

    # ── internals ──────────────────────────────────────────────────────────────

    def _extract_cell(self, cell: Cell,
                      max_intra_gap: int) -> tuple[list[Image.Image], list[Frame]]:
        boxes = segment_sprites(
            self.arr, cell.x0, cell.x1, cell.sprite_y0, cell.y1,
            self.bg, self.tol,
        )
        return stitch_frames(boxes, self.arr, self.bg, self.tol, max_intra_gap)

    def _build_cells(self) -> list[Cell]:
        h_seps = find_horizontal_separators(self.arr)
        h_gaps = separators_to_gaps(h_seps, self.img.height)
        cells: list[Cell] = []
        for row_y0, row_y1 in h_gaps:
            if row_y1 - row_y0 < 20:
                continue
            v_seps = find_vertical_separators_in_band(self.arr, row_y0, row_y1)
            v_gaps = separators_to_gaps(v_seps, self.img.width)
            for col_x0, col_x1 in v_gaps:
                if col_x1 - col_x0 < 20:
                    continue
                sprite_y0 = find_sprite_y_start(self.arr, col_x0, col_x1,
                                                row_y0, row_y1, self.bg, self.tol)
                cells.append(Cell(name="", x0=col_x0, y0=row_y0,
                                  x1=col_x1, y1=row_y1, sprite_y0=sprite_y0))
        return cells

    def _find_cell(self, name: str) -> Optional[Cell]:
        lower = name.lower()
        named = [c for c in self.cells() if c.name]
        for c in named:
            if c.name.lower() == lower:
                return c
        for c in named:
            if lower in c.name.lower():
                return c
        return None


# ── Saving helpers ─────────────────────────────────────────────────────────────

def save_animation(out_dir: Path, sprites: list[Image.Image],
                   frames: list[Frame], gif_path: str,
                   bg: tuple, tol: int) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, sprite in enumerate(sprites):
        sprite.save(out_dir / f"{i:03d}.png")
    save_metadata(out_dir, gif_path, bg, tol, frames)


# ── CLI ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract sprites from a sprite-sheet GIF, or stitch split frames.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  List detected animations:
    python extract_sprites.py sheet.gif

  Extract one animation:
    python extract_sprites.py sheet.gif "Idle" -o ./sprites

  Extract every animation:
    python extract_sprites.py sheet.gif --all -o ./sprites

  Stitch frames 0 and 1 that were incorrectly split:
    python extract_sprites.py --stitch ./sprites/Hundred 0 1

  Split frame 0 back into individual blobs:
    python extract_sprites.py --split ./sprites/Hundred 0

  Split frame 0 at pixel x=70:
    python extract_sprites.py --split ./sprites/Hundred 0 70
""",
    )

    # ── stitch mode ────────────────────────────────────────────────────────────
    parser.add_argument("--stitch", nargs="+", metavar="ARG",
                        help="FOLDER IDX IDX … — merge frames in FOLDER using frames.json")

    # ── split mode ─────────────────────────────────────────────────────────────
    parser.add_argument("--split", nargs="+", metavar="ARG",
                        help="FOLDER IDX [X] — split a frame by blobs (no X) or at pixel x=X")

    # ── extract mode ───────────────────────────────────────────────────────────
    parser.add_argument("gif",       nargs="?", help="Path to the sprite-sheet GIF.")
    parser.add_argument("animation", nargs="?", help="Animation name to extract.")
    parser.add_argument("--all",    action="store_true",
                        help="Extract every detected animation.")
    parser.add_argument("--output", "-o", default=".",
                        help="Output root directory (default: current dir).")
    parser.add_argument("--tol",    type=int, default=20,
                        help="Background colour tolerance 0-255 (default: 20).")
    parser.add_argument("--gap",    type=int, default=4,
                        help="Max pixel gap between blobs treated as one frame (default: 4).")

    args = parser.parse_args()

    # ── stitch mode ───────────────────────────────────────────────────────────
    if args.stitch:
        stitch_args = args.stitch
        if len(stitch_args) < 3:
            sys.exit("Usage: --stitch FOLDER IDX IDX [IDX …]")
        folder  = Path(stitch_args[0])
        try:
            indices = [int(x) for x in stitch_args[1:]]
        except ValueError:
            sys.exit("Frame indices must be integers.")
        cmd_stitch(folder, indices)
        return

    # ── split mode ────────────────────────────────────────────────────────────
    if args.split:
        split_args = args.split
        if len(split_args) < 2:
            sys.exit("Usage: --split FOLDER IDX [X]")
        folder = Path(split_args[0])
        try:
            idx = int(split_args[1])
            x   = int(split_args[2]) if len(split_args) > 2 else None
        except ValueError:
            sys.exit("IDX and X must be integers.")
        cmd_split(folder, idx, x)
        return

    # ── extract mode ──────────────────────────────────────────────────────────
    if not args.gif:
        parser.print_help()
        sys.exit(1)

    sheet = SpriteSheet(args.gif, tol=args.tol)
    print(f"Background: RGB{sheet.bg}  |  Image: {sheet.img.width}×{sheet.img.height}")

    # List mode
    if not args.all and not args.animation:
        cells = sheet.cells()
        print(f"\n{len(cells)} cells detected:\n")
        for i, c in enumerate(cells, 1):
            print(f"  {i:3d}.  [{c.x0:4d},{c.y0:4d}]-[{c.x1:4d},{c.y1:4d}]")
        return

    out_root = Path(args.output)

    # Extract all
    if args.all:
        results = sheet.extract_all(max_intra_gap=args.gap)
        for n, (_, sprites, frames) in enumerate(results, 1):
            folder  = f"unknown-{n:03d}"
            out_dir = out_root / folder
            save_animation(out_dir, sprites, frames, args.gif, sheet.bg, sheet.tol)
            print(f"  {folder}  ->  {out_dir}  ({len(sprites)} frames)")
        print(f"\nDone — {len(results)} animations extracted to '{out_root}'.")
        return

    parser.error("Without --all, specify an animation name (not supported without OCR).")


if __name__ == "__main__":
    main()
