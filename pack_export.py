"""
pack_export.py — export an animation tree as a self-contained animation pack.

Public API
----------
export_pack(root_branch, pack_name, target_dir)
    Write {target_dir}/{pack_name}/ with pack.json and renamed PNG copies.
    Returns a list of missing/unreadable PNG paths (empty on full success).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from director import DBranch


def export_pack(root_branch: DBranch, pack_name: str,
                target_dir: Path) -> list[Path]:
    """Export the animation tree rooted at *root_branch* as a pack folder.

    Frame files are named ``{branch_path}_{pos:03d}.png`` where *branch_path*
    is the underscore-joined chain of branch names with the root normalised to
    ``"root"`` (e.g. ``root_001.png``, ``root_lean_up_001.png``).  Positions
    are 1-based and zero-padded to 3 digits.

    Parameters
    ----------
    root_branch:
        The root ``DBranch`` of the animation tree.
    pack_name:
        Identifier used as the output folder name and the ``name`` field in
        ``pack.json``.  Must match ``[a-z0-9_]+``.
    target_dir:
        Parent directory.  The pack is written to ``{target_dir}/{pack_name}/``.

    Returns
    -------
    list[Path]
        Paths of PNGs that could not be read or copied.  Empty on full success.
    """
    out_dir = target_dir / pack_name
    out_dir.mkdir(parents=True, exist_ok=True)

    missing: list[Path] = []

    def _branch_path(branch: DBranch) -> str:
        """Underscore-joined chain from root to *branch*, root renamed to 'root'."""
        parts: list[str] = []
        b: DBranch | None = branch
        while b is not None:
            parts.append("root" if b.parent is None else b.name)
            b = b.parent
        parts.reverse()
        return "_".join(parts)

    def _find_spawn_file(parent: DBranch, spawn_uid: str | None) -> str | None:
        """Return the export filename for the spawn frame, or None."""
        if spawn_uid is None:
            return None
        for pos, f in enumerate(parent.frames, start=1):
            if f.uid == spawn_uid:
                bp = _branch_path(parent)
                return f"{bp}_{pos:03d}.png"
        return None

    def _walk(branch: DBranch) -> dict:
        bp = _branch_path(branch)
        frames_json: list[dict] = []

        for pos, frame in enumerate(branch.frames, start=1):
            dest_name = f"{bp}_{pos:03d}.png"
            dest_path = out_dir / dest_name

            src = frame.png
            if not src.exists():
                missing.append(src)
            else:
                try:
                    shutil.copy2(src, dest_path)
                except OSError:
                    missing.append(src)

            entry: dict = {
                "file": dest_name,
                "hold": frame.hold if frame.keyframe else 1,
            }
            frames_json.append(entry)

        node: dict = {"frames": frames_json}

        if branch.branches:
            branches_json: dict = {}
            for child in branch.branches.values():
                spawn_file = _find_spawn_file(branch, child.spawn_uid)
                child_node: dict = {"reversible": child.reversible}
                if spawn_file is not None:
                    child_node["spawn_file"] = spawn_file
                child_node.update(_walk(child))
                branches_json[child.name] = child_node
            node["branches"] = branches_json

        return node

    root_node = _walk(root_branch)

    pack = {
        "name": pack_name,
        "version": 1,
        "root": root_node,
    }

    (out_dir / "pack.json").write_text(
        json.dumps(pack, indent=2), encoding="utf-8")

    return missing
