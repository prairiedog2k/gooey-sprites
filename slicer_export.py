"""
slicer_export.py — export extracted slice parts as a self-contained pack.

Public API
----------
export_pack(tree_data, group_hierarchy, source_png, slice_dir, pack_name, target_dir)
    Write {target_dir}/{pack_name}/ with pack.json, the source frame copy, and
    all extracted part PNGs.  Parts with no extracted PNG are silently skipped.
    Returns {"copied": int, "skipped": int}.

pack.json v2 format
-------------------
{
  "name":         "pack-name",
  "version":      2,
  "source_frame": "frame.png",
  "frame_size":   [W, H],
  "groups": {
    "<group>": {
      "hierarchy": { "<part>": "<parent>", ... },   // optional
      "parts": [
        { "name": "<part>", "file": "<group>/<part>.png",
          "canvas": [W, H], "origin": [x, y] },     // origin omitted if no ghost
        ...
      ]
    }
  }
}

Coordinate convention for flopsy importers
-------------------------------------------
  world_x =  origin_x + canvas_w/2 - frame_w/2
  world_y = -(origin_y + canvas_h/2 - frame_h/2)   # Y-flipped (flopsy is Y-up)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from PIL import Image


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


def export_pack(
    tree_data:       dict[str, dict[str, dict]],
    group_hierarchy: dict[str, dict[str, str]],
    source_png:      Path,
    slice_dir:       Path,
    pack_name:       str,
    target_dir:      Path,
) -> dict[str, int]:
    """Copy extracted parts + source frame into a portable pack folder.

    Parameters
    ----------
    tree_data:
        In-memory tree: {group: {part: {"w", "h", "ghost", "visible"}}}.
    group_hierarchy:
        Per-group bone hierarchy: {group: {part: parent_name}}.
    source_png:
        The source frame that was sliced.
    slice_dir:
        Root directory where extracted PNGs live (group/part.png under here).
    pack_name:
        Folder name for the output pack; also written to pack.json["name"].
    target_dir:
        Parent directory.  Pack is written to {target_dir}/{pack_name}/.

    Returns
    -------
    dict with keys "copied" (int) and "skipped" (int).
    """
    out_dir = target_dir / pack_name
    out_dir.mkdir(parents=True, exist_ok=True)

    copied  = 0
    skipped = 0

    # ── source frame + dimensions ─────────────────────────────────────────────
    frame_size = [0, 0]
    if source_png.exists():
        try:
            with Image.open(source_png) as im:
                frame_size = list(im.size)
            shutil.copy2(source_png, out_dir / source_png.name)
        except OSError:
            pass  # non-fatal

    # ── groups ────────────────────────────────────────────────────────────────
    groups_json: dict[str, dict] = {}

    for group, parts in tree_data.items():
        parts_list: list[dict] = []

        for part_name, nd in parts.items():
            src = slice_dir / group / f"{part_name}.png"
            if not src.exists():
                skipped += 1
                continue
            dest = out_dir / group / f"{part_name}.png"
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dest)
            except OSError:
                skipped += 1
                continue

            try:
                with Image.open(src) as _im:
                    canvas_w, canvas_h = _im.size
            except OSError:
                canvas_w, canvas_h = nd["w"], nd["h"]

            entry: dict = {
                "name":   part_name,
                "file":   f"{group}/{part_name}.png",
                "canvas": [canvas_w, canvas_h],
            }
            origin = _ghost_origin(nd.get("ghost"))
            if origin is not None:
                entry["origin"] = origin
            if nd.get("angle") is not None:
                entry["angle"] = round(nd["angle"], 4)
            parts_list.append(entry)
            copied += 1

        if parts_list:
            group_entry: dict = {"parts": parts_list}
            hier = group_hierarchy.get(group)
            if hier:
                group_entry["hierarchy"] = hier
            groups_json[group] = group_entry

    # ── descriptor ───────────────────────────────────────────────────────────
    pack = {
        "name":         pack_name,
        "version":      3,
        "source_frame": source_png.name,
        "frame_size":   frame_size,
        "groups":       groups_json,
    }
    (out_dir / "pack.json").write_text(
        json.dumps(pack, indent=2), encoding="utf-8")

    return {"copied": copied, "skipped": skipped}
