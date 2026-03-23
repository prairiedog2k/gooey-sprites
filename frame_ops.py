"""Low-level frame manipulation helpers (no GUI dependencies)."""

import json as _json
import shutil as _shutil
from pathlib import Path


def _cmd_delete_frames(out_dir: Path, indices: set[int]):
    """Remove frames at `indices`, renumber the rest, update frames.json."""
    meta_path = out_dir / "frames.json"
    meta = _json.loads(meta_path.read_text(encoding="utf-8"))

    # Delete PNG files for removed frames
    for f in meta["frames"]:
        if f["index"] in indices:
            p = out_dir / f["file"]
            if p.exists():
                p.unlink()

    kept = [f for f in meta["frames"] if f["index"] not in indices]

    # Two-pass rename to avoid collisions: first to .tmp names, then to final names
    for new_idx, f in enumerate(kept):
        old_path = out_dir / f["file"]
        tmp_path = out_dir / f"{new_idx:03d}.__tmp__.png"
        if old_path.exists():
            old_path.rename(tmp_path)
        f["_tmp"] = tmp_path.name

    for new_idx, f in enumerate(kept):
        tmp_path = out_dir / f["_tmp"]
        final    = out_dir / f"{new_idx:03d}.png"
        if tmp_path.exists():
            tmp_path.rename(final)
        del f["_tmp"]
        f["index"] = new_idx
        f["file"]  = f"{new_idx:03d}.png"

    meta["frames"] = kept
    meta_path.write_text(_json.dumps(meta, indent=2), encoding="utf-8")


def _cmd_duplicate_frame(out_dir: Path, src_idx: int):
    """Append a copy of frame src_idx at the end of the sequence."""
    meta_path = out_dir / "frames.json"
    meta = _json.loads(meta_path.read_text(encoding="utf-8"))

    src_frame = next(f for f in meta["frames"] if f["index"] == src_idx)
    new_idx   = max(f["index"] for f in meta["frames"]) + 1

    _shutil.copy2(out_dir / src_frame["file"], out_dir / f"{new_idx:03d}.png")

    new_entry = {
        "index": new_idx,
        "file":  f"{new_idx:03d}.png",
        "blobs": src_frame["blobs"],
    }
    if src_frame.get("hitboxes"):
        new_entry["hitboxes"] = list(src_frame["hitboxes"])
    meta["frames"].append(new_entry)
    meta_path.write_text(_json.dumps(meta, indent=2), encoding="utf-8")


def _cmd_reorder_frames(out_dir: Path, src_idx: int, dst_idx: int):
    """Move frame at src_idx so it is inserted before dst_idx, renaming files."""
    if src_idx == dst_idx or src_idx + 1 == dst_idx:
        return
    meta_path = out_dir / "frames.json"
    meta   = _json.loads(meta_path.read_text(encoding="utf-8"))
    frames = sorted(meta["frames"], key=lambda f: f["index"])

    item = frames.pop(src_idx)
    # Adjust insertion point: removing src shifts later positions left by one
    insert_at = dst_idx - 1 if dst_idx > src_idx else dst_idx
    frames.insert(insert_at, item)

    # Two-pass rename to avoid collisions
    for new_idx, f in enumerate(frames):
        old_path = out_dir / f["file"]
        tmp_path = out_dir / f"{new_idx:03d}.__tmp__.png"
        if old_path.exists():
            old_path.rename(tmp_path)
        f["_tmp"] = tmp_path.name

    for new_idx, f in enumerate(frames):
        tmp_path = out_dir / f["_tmp"]
        final    = out_dir / f"{new_idx:03d}.png"
        if tmp_path.exists():
            tmp_path.rename(final)
        del f["_tmp"]
        f["index"] = new_idx
        f["file"]  = f"{new_idx:03d}.png"

    meta["frames"] = frames
    meta_path.write_text(_json.dumps(meta, indent=2), encoding="utf-8")
