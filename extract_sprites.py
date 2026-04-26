#!/usr/bin/env python3
"""
extract_sprites.py

Extracts individual sprites from a sprite-sheet image (GIF, PNG, etc.).

Structure supported:
  - Sheets where full-width white/bright horizontal lines divide animation rows,
    and full-height white vertical lines divide cells within each row (classic
    labelled arcade sprite sheets).
  - Sheets where rows and cells are separated only by strips of the background
    colour with no white grid lines at all (e.g. Dhalsim.png-style sheets with
    a solid dark background).
  - Each cell may contain a white text label near the top; sprite pixels are
    coloured (non-white, non-background) so the label is automatically excluded.
  - Background is detected from the image corners.

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
    """Strict segmentation mask: True for non-background AND non-near-white pixels.

    The near-white exclusion (R,G,B > 200) prevents embedded text labels and
    white grid lines from inflating the detected sprite bounding boxes.  It is
    intentionally used only for *finding* blob extents; see `_sprite_paint_mask`
    for the relaxed version used when compositing pixels inside those bounds.
    """
    diff      = np.abs(arr[:,:,:3].astype(np.int32) - np.array(bg, dtype=np.int32))
    is_bg     = diff.max(axis=2) <= tol
    is_border = (arr[:,:,0] > 200) & (arr[:,:,1] > 200) & (arr[:,:,2] > 200)
    return ~is_bg & ~is_border


def _sprite_paint_mask(arr: np.ndarray, bg: tuple, tol: int) -> np.ndarray:
    """Relaxed compositing mask: True for any non-background pixel.

    Used inside already-established blob bounding boxes so that near-white
    sprite pixels (highlights, light shading) are preserved, while text labels
    outside the box were already excluded during segmentation.
    """
    diff  = np.abs(arr[:,:,:3].astype(np.int32) - np.array(bg, dtype=np.int32))
    is_bg = diff.max(axis=2) <= tol
    return ~is_bg


# ── Sprite-zone detection ─────────────────────────────────────────────────────

def find_sprite_zone_width(arr: np.ndarray, bg: tuple, tol: int,
                           min_banner_coverage: float = 0.5,
                           min_banner_columns: int = 20) -> int:
    """Return the x-width of the sprite zone, masking out any right-side banner.

    Scans columns left-to-right.  The first run of ``min_banner_columns``
    consecutive columns where more than ``min_banner_coverage`` of their pixels
    are non-background is treated as the start of an annotation block (e.g. a
    solid-colour title card or credit banner).  All columns from that point
    onward are excluded from further processing.

    Returns the full image width when no such block is found, so sheets without
    banners are unaffected.
    """
    h, w = arr.shape[:2]
    diff = np.abs(arr[:, :, :3].astype(np.int32) - np.array(bg, dtype=np.int32))
    nonbg_frac = (diff.max(axis=2) > tol).mean(axis=0)   # shape (w,)
    is_banner = nonbg_frac > min_banner_coverage
    run = 0
    for x in range(w):
        if is_banner[x]:
            run += 1
            if run >= min_banner_columns:
                return x - min_banner_columns + 1
        else:
            run = 0
    return w


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
                                bg: tuple = None,
                                tol: int = 20,
                                threshold: int = 200,
                                min_coverage: float = 0.4,
                                min_sep_height: int = 3) -> list[tuple[int,int]]:
    """Find rows that are separator lines.

    Detects two kinds of separators:
    - Bright/white rows (classic white-grid sprite sheets), and
    - Rows that are ≥95 % background colour (non-white-grid sheets like Dhalsim).

    min_sep_height: minimum consecutive separator-row count for a run to be
    treated as a true boundary.  Short all-background gaps (e.g. 1-2 rows of
    empty space between a steam wisp and the sprite body below) are ignored,
    preventing them from splitting a single animation into separate cells.
    """
    bright = (arr[:,:,0]>threshold) & (arr[:,:,1]>threshold) & (arr[:,:,2]>threshold)
    is_sep = bright.mean(axis=1) >= min_coverage

    if bg is not None:
        diff = np.abs(arr[:,:,:3].astype(np.int32) - np.array(bg, dtype=np.int32))
        is_bg_px = diff.max(axis=2) <= tol
        # A row must be 100 % background to count as a separator — even one
        # sprite pixel disqualifies it. A % threshold is too loose on wide
        # sheets where sparse top-of-head rows look >95 % background.
        is_sep = is_sep | is_bg_px.all(axis=1)

    rows = [int(y) for y in np.where(is_sep)[0]]
    runs = _merge_runs(rows)
    if min_sep_height > 1:
        runs = [(s0, s1) for s0, s1 in runs if s1 - s0 + 1 >= min_sep_height]
    return runs


def find_vertical_separators_in_band(arr: np.ndarray, y0: int, y1: int,
                                      threshold: int = 200,
                                      min_coverage: float = 0.4) -> list[tuple[int,int]]:
    """Find columns that are explicit white/bright separator lines within a band.

    Only bright/white columns are treated as dividers — background-coloured gaps
    between sprites within a row are intentionally NOT treated as separators so
    that all sprites in a row stay in the same cell (= one animation).
    """
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
        if _sprite_paint_mask(arr[y:y+1, x0:x1], bg, tol).any():
            return y
    return y1


def segment_sprites(arr: np.ndarray,
                    x0: int, x1: int, y0: int, y1: int,
                    bg: tuple, tol: int,
                    min_w: int = 10, min_h: int = 10) -> list[tuple[int,int,int,int]]:
    """Return (sx0,sy0,sx1,sy1) blobs in full-image coordinates, left-to-right.

    Column spans are discovered with the strict mask (excludes near-white) so
    text labels don't create false blobs.  Row extents within each span are then
    measured with the relaxed paint mask so near-white sprite pixels (white
    headbands, highlight shading, etc.) are included in the bounding box.
    """
    region     = arr[y0:y1, x0:x1]
    strict     = sprite_fg_mask(region, bg, tol)
    relaxed    = _sprite_paint_mask(region, bg, tol)
    col_has_fg = strict.any(axis=0)
    sprites, in_sprite, sx = [], False, 0
    for cx, fg in enumerate(col_has_fg):
        if fg and not in_sprite:
            sx, in_sprite = cx, True
        elif not fg and in_sprite:
            strip = relaxed[:, sx:cx]
            rows  = np.where(strip.any(axis=1))[0]
            w, h  = cx - sx, (rows[-1] - rows[0] + 1) if rows.size else 0
            if rows.size and w >= min_w and h >= min_h:
                sprites.append((x0+sx, y0+rows[0], x0+cx, y0+rows[-1]+1))
            in_sprite = False
    if in_sprite:
        strip = relaxed[:, sx:]
        rows  = np.where(strip.any(axis=1))[0]
        w, h  = len(col_has_fg) - sx, (rows[-1] - rows[0] + 1) if rows.size else 0
        if rows.size and w >= min_w and h >= min_h:
            sprites.append((x0+sx, y0+rows[0], x0+len(col_has_fg), y0+rows[-1]+1))
    return sprites


# ── Blob type aliases ──────────────────────────────────────────────────────────

Blob  = tuple[int, int, int, int]   # (sx0, sy0, sx1, sy1) in sheet coordinates
Frame = list[Blob]                   # one or more blobs that form one output frame


# ── Touching-sprite splitter ────────────────────────────────────────────────────

def _max_run_arr(arr: np.ndarray, threshold: float) -> int:
    """Return the length of the longest contiguous run where arr[i] >= threshold."""
    best = run = 0
    for v in arr:
        if v >= threshold:
            run += 1
            if run > best:
                best = run
        else:
            run = 0
    return best


def _trim_blob_y(blob: Blob, arr: np.ndarray,
                 bg: tuple, tol: int) -> "Blob | None":
    """Narrow a blob's y-extent to only rows that contain non-background pixels."""
    sx0, sy0, sx1, sy1 = blob
    if sx0 >= sx1:
        return None
    region = arr[sy0:sy1, sx0:sx1]
    diff   = np.abs(region[:, :, :3].astype(np.int32) - np.array(bg, dtype=np.int32))
    rows   = np.where((diff.max(axis=2) > tol).any(axis=1))[0]
    if rows.size == 0:
        return None
    return (sx0, sy0 + int(rows[0]), sx1, sy0 + int(rows[-1]) + 1)


def _find_best_valley(profile: np.ndarray, min_w: int,
                      valley_ratio: float, max_pinch_px: int,
                      min_sustain_ratio: float = 0.20) -> "int | None":
    """Return the column index of the deepest qualifying valley, or None.

    A qualifying valley must satisfy all of:
    - Absolute: column pixel count <= max_pinch_px
    - Relative: count / min(left_peak, right_peak) < valley_ratio
    - Sustain: each side must have a contiguous run of columns at >= 75% of its
      peak whose length is >= min_sustain_ratio × the half-width of that side.
      This prevents false splits on single sprites with narrow waists, which tend
      to have short sustained-density runs even though their valley/peak ratio
      can be low.
    """
    n = len(profile)
    best_x, best_score = None, float('inf')
    for x in range(min_w, n - min_w):
        v = int(profile[x])
        if v > max_pinch_px:
            continue
        left_part  = profile[:x]
        right_part = profile[x + 1:]
        lp = int(left_part.max())
        rp = int(right_part.max())
        if lp < 3 or rp < 3:
            continue
        ratio = v / min(lp, rp)
        if ratio >= valley_ratio:
            continue
        l_run = _max_run_arr(left_part,  lp * 0.75)
        r_run = _max_run_arr(right_part, rp * 0.75)
        if l_run / x < min_sustain_ratio or r_run / (n - x - 1) < min_sustain_ratio:
            continue
        if ratio < best_score:
            best_score, best_x = ratio, x
    return best_x


def _split_blob_recursive(blob: Blob, arr: np.ndarray, bg: tuple, tol: int,
                          min_w: int, valley_ratio: float,
                          max_pinch_px: int) -> "list[Blob]":
    sx0, sy0, sx1, sy1 = blob
    if sx1 - sx0 < 2 * min_w:
        return [blob]

    region  = arr[sy0:sy1, sx0:sx1]
    diff    = np.abs(region[:, :, :3].astype(np.int32) - np.array(bg, dtype=np.int32))
    profile = (diff.max(axis=2) > tol).sum(axis=0).astype(np.int32)

    vx = _find_best_valley(profile, min_w, valley_ratio, max_pinch_px)
    if vx is None:
        return [blob]

    # Split just before and just after the valley minimum column.
    # The valley pixel is dropped; stitch_frames is prevented from re-merging
    # these sub-blobs by the forced_splits mechanism in split_touching_blobs.
    left_blob  = _trim_blob_y((sx0,          sy0, sx0 + vx,     sy1), arr, bg, tol)
    right_blob = _trim_blob_y((sx0 + vx + 1, sy0, sx1,          sy1), arr, bg, tol)

    if left_blob is None or right_blob is None:
        return [blob]
    if left_blob[2] - left_blob[0] < min_w or right_blob[2] - right_blob[0] < min_w:
        return [blob]

    return (_split_blob_recursive(left_blob,  arr, bg, tol, min_w, valley_ratio, max_pinch_px) +
            _split_blob_recursive(right_blob, arr, bg, tol, min_w, valley_ratio, max_pinch_px))


def split_touching_blobs(
    blobs:        "list[Blob]",
    arr:          np.ndarray,
    bg:           tuple,
    tol:          int,
    min_w:        int   = 10,
    valley_ratio: float = 0.35,
    max_pinch_px: int   = 6,
) -> "tuple[list[Blob], set[int]]":
    """Split blobs containing multiple touching sprites at column pinch-points.

    A pinch point is a column where the non-background pixel count drops to
    <= max_pinch_px AND is less than valley_ratio × the lower surrounding peak.
    Blobs are split recursively until no more pinch points are found.

    Returns ``(new_blobs, forced_splits)`` where ``forced_splits`` is a set of
    boundary indices *i* meaning ``new_blobs[i]`` and ``new_blobs[i+1]`` must
    not be stitched into the same frame (they are separate sprites that were
    touching pixel-to-pixel on the sheet).
    """
    result: list[Blob] = []
    forced: set[int]   = set()
    for blob in blobs:
        sub  = _split_blob_recursive(blob, arr, bg, tol, min_w, valley_ratio, max_pinch_px)
        base = len(result)
        for j in range(len(sub) - 1):
            forced.add(base + j)
        result.extend(sub)
    return result, forced


# ── Compositing ────────────────────────────────────────────────────────────────


def _max_opaque_pixels(images: list) -> int:
    """Return the highest non-transparent pixel count across all frames."""
    best = 0
    for img in images:
        count = int((np.array(img)[:, :, 3] > 0).sum())
        if count > best:
            best = count
    return best


# Palette-complexity thresholds used by flag_false_positives().
# Animations with ≤ _FP_ABS_COLORS bucketed colors are almost certainly false.
# Animations below _FP_REL_FACTOR × median score are relatively flagged.
_FP_ABS_COLORS  = 10
_FP_REL_FACTOR  = 0.18


def _palette_score(images: list) -> int:
    """Count distinct non-transparent RGB colors (quantized to 32-step buckets).

    Quantising prevents anti-aliasing / compression noise from inflating the
    score while still cleanly separating 1-3-colour backgrounds from real
    sprites (which typically have dozens of distinct hues and shades).
    """
    packed: set[int] = set()
    for img in images:
        arr = np.array(img)
        mask = arr[:, :, 3] > 0
        if not mask.any():
            continue
        # Right-shift 5 → 3-bit channels (8 steps per channel = 512 max buckets).
        px   = arr[mask, :3].astype(np.uint32) >> 5
        keys = (px[:, 0] << 6) | (px[:, 1] << 3) | px[:, 2]
        packed.update(keys.tolist())
    return len(packed)


def flag_false_positives(scores: list[int]) -> list[bool]:
    """Return True for each animation whose palette score looks like a false positive.

    Two conditions, either of which triggers a flag:
    - Absolute: score ≤ _FP_ABS_COLORS (background / text cells almost always
      have ≤ 10 distinct bucketed colours).
    - Relative: score < _FP_REL_FACTOR × median score of all animations (catches
      suspiciously simple animations even on sheets with simpler art styles).
    """
    if not scores:
        return []
    valid = [s for s in scores if s > 0]
    if not valid:
        return [True] * len(scores)
    median_score = sorted(valid)[len(valid) // 2]
    rel_threshold = median_score * _FP_REL_FACTOR
    threshold = max(_FP_ABS_COLORS, rel_threshold)
    return [s <= threshold for s in scores]


def _compose_frame(blobs: Frame, arr: np.ndarray,
                   bg: tuple, tol: int,
                   canvas_top: int = None,
                   canvas_bottom: int = None) -> Image.Image:
    """Render blobs onto a transparent canvas at their correct relative positions.

    canvas_top / canvas_bottom pin the vertical extent so all frames in an
    animation share the same height and no frame is clipped at the top.
    """
    left   = min(b[0] for b in blobs)
    top    = canvas_top    if canvas_top    is not None else min(b[1] for b in blobs)
    right  = max(b[2] for b in blobs)
    bottom = canvas_bottom if canvas_bottom is not None else max(b[3] for b in blobs)
    canvas = np.zeros((bottom - top, right - left, 4), dtype=np.uint8)
    for sx0, sy0, sx1, sy1 in blobs:
        piece      = arr[sy0:sy1, sx0:sx1].copy()
        fg         = _sprite_paint_mask(piece, bg, tol)
        piece[~fg] = [0, 0, 0, 0]
        dx, dy     = sx0 - left, sy0 - top
        dst        = canvas[dy:dy+(sy1-sy0), dx:dx+(sx1-sx0)]
        dst[fg]    = piece[fg]
    return Image.fromarray(canvas, "RGBA")


def stitch_frames(boxes: list[Blob],
                  arr: np.ndarray,
                  bg: tuple, tol: int,
                  max_intra_gap: int = 4,
                  forced_splits: "set[int] | None" = None,
                  ) -> tuple[list[Image.Image], list[Frame]]:
    """
    Group raw blobs into frames: consecutive blobs whose horizontal gap on the
    sheet is <= max_intra_gap are considered parts of the same frame.

    forced_splits: set of boundary indices i where boxes[i] and boxes[i+1] must
    NOT be stitched regardless of their pixel gap (they are separate sprites that
    were touching on the sheet and have been split by split_touching_blobs).

    Returns (images, frames) where frames[i] is the list of blobs for image i.
    The blob lists are the metadata needed for manual re-stitching later.
    """
    if not boxes:
        return [], []
    if forced_splits is None:
        forced_splits = set()
    groups: list[Frame] = [[boxes[0]]]
    for i, blob in enumerate(boxes[1:], 1):
        gap = blob[0] - groups[-1][-1][2]
        if gap <= max_intra_gap and (i - 1) not in forced_splits:
            groups[-1].append(blob)
        else:
            groups.append([blob])

    # Use the global y extent across every frame so all frames share the same
    # height — prevents top/bottom clipping on frames that are shorter than the
    # tallest frame in the animation.
    global_top    = min(b[1] for g in groups for b in g)
    global_bottom = max(b[3] for g in groups for b in g)

    images = [_compose_frame(g, arr, bg, tol, global_top, global_bottom)
              for g in groups]
    return images, groups


def apply_auto_split(
    sprites: list,
    frames:  list,
    arr,
    bg:  tuple,
    tol: int,
) -> "tuple[list, list]":
    """Return new (sprites, frames) with every multi-blob frame expanded into
    individual single-blob frames, each rendered from the original sheet array.

    Pass the result to ``save_animation`` instead of the original lists.
    """
    new_sprites: list = []
    new_frames:  list = []
    for sprite, frame in zip(sprites, frames):
        if len(frame) <= 1:
            new_sprites.append(sprite)
            new_frames.append(frame)
        else:
            for blob in frame:
                new_sprites.append(_compose_frame([blob], arr, bg, tol))
                new_frames.append([blob])
    return new_sprites, new_frames


# ── Metadata I/O ───────────────────────────────────────────────────────────────

FRAMES_JSON = "frames.json"


def save_metadata(out_dir: Path, gif_path: str,
                  bg: tuple, tol: int, frames: list[Frame]) -> None:
    """Save frames.json alongside the extracted PNGs."""
    anim_name = out_dir.name
    data = {
        "gif": str(Path(gif_path).resolve()),
        "bg":  list(bg),
        "tol": tol,
        "frames": [
            {
                "index": i,
                "file":  f"{anim_name}-{i:03d}.png",
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

def _stitch_from_pngs(out_dir: Path, frame_metas: list[dict]) -> Image.Image:
    """Compose a merged image by placing frame PNGs side-by-side (left to right)."""
    imgs = [Image.open(out_dir / f["file"]).convert("RGBA") for f in frame_metas]
    total_w = sum(im.width for im in imgs)
    max_h   = max(im.height for im in imgs)
    canvas  = Image.new("RGBA", (total_w, max_h), (0, 0, 0, 0))
    x = 0
    for im in imgs:
        canvas.paste(im, (x, 0))
        x += im.width
    return canvas


def cmd_stitch(out_dir: Path, frame_indices: list[int]) -> None:
    """
    Merge the specified frame indices into a single frame.
    When the original sprite sheet is available and blobs use sheet coordinates,
    _compose_frame re-renders from the sheet.  Otherwise falls back to placing
    the frame PNGs side-by-side and storing PNG-local blob coords.
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

    sorted_indices = sorted(frame_indices)
    frames_to_merge = [meta["frames"][i] for i in sorted_indices]

    gif_path = meta.get("gif", "")
    use_sheet = (
        bool(gif_path)
        and Path(gif_path).exists()
        and not any(b.get("png_local") for f in frames_to_merge for b in f["blobs"])
    )

    if use_sheet:
        bg  = tuple(meta["bg"])
        tol = meta["tol"]
        arr = np.array(Image.open(gif_path).convert("RGBA"))

        all_blobs: Frame = []
        for idx in sorted_indices:
            all_blobs.extend(
                (b["x0"], b["y0"], b["x1"], b["y1"])
                for b in meta["frames"][idx]["blobs"]
            )

        stitched   = _compose_frame(all_blobs, arr, bg, tol)
        new_blobs  = [{"x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3]}
                      for b in all_blobs]
    else:
        stitched   = _stitch_from_pngs(out_dir, frames_to_merge)
        # Build PNG-local blob entries: one blob per source frame, placed side-by-side
        new_blobs  = []
        x = 0
        for f in frames_to_merge:
            img = Image.open(out_dir / f["file"])
            new_blobs.append({"x0": x, "y0": 0,
                               "x1": x + img.width, "y1": img.height,
                               "png_local": True})
            x += img.width

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
    meta["frames"][keep_idx]["blobs"] = new_blobs

    # Drop removed frames from the list
    meta["frames"] = [f for i, f in enumerate(meta["frames"]) if i not in remove]

    # Renumber: rename PNGs and update metadata
    anim_name = out_dir.name
    for new_idx, frame in enumerate(meta["frames"]):
        old_path = out_dir / frame["file"]
        new_file = f"{anim_name}-{new_idx:03d}.png"
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
    anim_name = out_dir.name
    if shift > 0:
        for old_f in reversed(meta["frames"][frame_idx + 1:]):
            old_path = out_dir / old_f["file"]
            new_path = out_dir / f"{anim_name}-{old_f['index'] + shift:03d}.png"
            if old_path.exists():
                old_path.rename(new_path)

    # ── remove original frame file ────────────────────────────────────────────
    (out_dir / frame_meta["file"]).unlink(missing_ok=True)

    # ── save new pieces ───────────────────────────────────────────────────────
    for j, (img, _) in enumerate(rendered):
        path = out_dir / f"{anim_name}-{frame_idx + j:03d}.png"
        img.save(path)
        print(f"  {path}  ({img.width}x{img.height})")

    # ── rebuild metadata ──────────────────────────────────────────────────────
    new_frames: list[dict] = []
    for i, f in enumerate(meta["frames"]):
        if i == frame_idx:
            for j, (_, fb) in enumerate(rendered):
                new_frames.append({
                    "index": frame_idx + j,
                    "file":  f"{anim_name}-{frame_idx + j:03d}.png",
                    "blobs": [{"x0": int(b[0]), "y0": int(b[1]),
                               "x1": int(b[2]), "y1": int(b[3])}
                              for b in fb],
                })
        else:
            new_idx = i if i < frame_idx else i + shift
            new_frames.append({**f, "index": new_idx, "file": f"{anim_name}-{new_idx:03d}.png"})

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
        self.sprite_w  = find_sprite_zone_width(self.arr, self.bg, self.tol)
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

    def extract_all(self, max_intra_gap: int = 4,
                    min_pixels: int = 100,
                    ) -> list[tuple[Cell, list[Image.Image], list[Frame], int]]:
        """Extract every cell, dropping animations with no real sprite content.

        Returns a list of (cell, images, frames, palette_score).  Callers can
        pass the scores to flag_false_positives() to identify likely non-sprite
        cells.

        min_pixels: minimum non-transparent pixel count that at least one frame
        must contain.  Cells with only a solid background or label text produce
        very sparse frames and are filtered out by this threshold.
        """
        results = []
        for cell in self.cells():
            images, frames = self._extract_cell(cell, max_intra_gap)
            if images and _max_opaque_pixels(images) >= min_pixels:
                score = _palette_score(images)
                results.append((cell, images, frames, score))
        return results

    # ── internals ──────────────────────────────────────────────────────────────

    def _extract_cell(self, cell: Cell,
                      max_intra_gap: int) -> tuple[list[Image.Image], list[Frame]]:
        boxes = segment_sprites(
            self.arr, cell.x0, cell.x1, cell.sprite_y0, cell.y1,
            self.bg, self.tol,
        )
        boxes, forced_splits = split_touching_blobs(boxes, self.arr, self.bg, self.tol)
        return stitch_frames(boxes, self.arr, self.bg, self.tol, max_intra_gap, forced_splits)

    def _build_cells(self) -> list[Cell]:
        # Restrict all detection to the sprite zone (excludes right-side banners).
        sprite_arr = self.arr[:, :self.sprite_w]
        h_seps = find_horizontal_separators(sprite_arr, bg=self.bg, tol=self.tol,
                                            min_sep_height=3)
        h_gaps = separators_to_gaps(h_seps, self.img.height)
        cells: list[Cell] = []
        for row_y0, row_y1 in h_gaps:
            if row_y1 - row_y0 < 8:
                continue
            v_seps = find_vertical_separators_in_band(sprite_arr, row_y0, row_y1)
            v_gaps = separators_to_gaps(v_seps, self.sprite_w)
            for col_x0, col_x1 in v_gaps:
                if col_x1 - col_x0 < 8:
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
    anim_name = out_dir.name
    for i, sprite in enumerate(sprites):
        sprite.save(out_dir / f"{anim_name}-{i:03d}.png")
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
    sprite_w_note = (f"  |  Sprite zone: 0–{sheet.sprite_w - 1}"
                     if sheet.sprite_w < sheet.img.width else "")
    print(f"Background: RGB{sheet.bg}  |  Image: {sheet.img.width}×{sheet.img.height}{sprite_w_note}")

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
        scores  = [score for *_, score in results]
        flagged = flag_false_positives(scores)
        for n, ((_, sprites, frames, score), is_fp) in enumerate(
                zip(results, flagged), 1):
            folder  = f"unknown-{n:03d}"
            out_dir = out_root / folder
            save_animation(out_dir, sprites, frames, args.gif, sheet.bg, sheet.tol)
            fp_tag  = "  [possible false positive]" if is_fp else ""
            print(f"  {folder}  ->  {out_dir}  ({len(sprites)} frames, "
                  f"{score} palette buckets){fp_tag}")
        print(f"\nDone — {len(results)} animations extracted to '{out_root}'.")
        return

    parser.error("Without --all, specify an animation name (not supported without OCR).")


if __name__ == "__main__":
    main()
