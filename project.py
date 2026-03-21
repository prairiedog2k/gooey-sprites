"""Project file (.ssproj) read/write helpers."""

import json
from pathlib import Path

from constants import PROJECT_VERSION


def _read_project(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version", 1) != PROJECT_VERSION:
        raise ValueError(f"Unsupported project version: {data.get('version')}")
    return data


def _write_project(path: Path, gif: str, output: str, gap: int, tol: int,
                   animations: list[str] | None = None,
                   min_pixels: int = 100,
                   flagged_animations: list[str] | None = None):
    # Store paths relative to the project file so the folder can be moved.
    proj_dir = path.parent

    def _rel(p: str) -> str:
        try:
            return str(Path(p).relative_to(proj_dir))
        except ValueError:
            return p  # keep absolute if on a different drive

    data = {
        "version":    PROJECT_VERSION,
        "gif":        _rel(gif)    if gif    else "",
        "output":     _rel(output) if output else "",
        "gap":        gap,
        "tol":        tol,
        "min_pixels":          min_pixels,
        "animations":          animations or [],
        "flagged_animations":  flagged_animations or [],
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _resolve_project_paths(data: dict, proj_dir: Path) -> tuple[str, str]:
    """Return (gif_abs, output_abs) resolving relative paths against proj_dir."""
    def _abs(p: str) -> str:
        if not p:
            return ""
        pp = Path(p)
        return str((proj_dir / pp).resolve()) if not pp.is_absolute() else p
    return _abs(data.get("gif", "")), _abs(data.get("output", ""))
