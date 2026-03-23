# Code Style and Conventions

## General
- Python 3.11+, no type stubs or mypy enforcement
- Type hints used on function signatures (e.g. `def foo(x: int) -> str`)
- `Path` from `pathlib` used throughout for file paths
- No docstrings on most methods; inline comments for non-obvious logic
- Private helpers prefixed with `_` (functions and methods)
- Constants are ALL_CAPS in `constants.py`

## Naming
- Classes: PascalCase (`SpriteGUI`, `FrameEditWindow`, `ComposeWindow`)
- Private helper functions: `_snake_case` with leading underscore
- Instance variables: `self._snake_case`
- tkinter `StringVar`/`IntVar`/`DoubleVar`: `self.v_name` or `self._var_name`
- Button/widget refs: `self._foo_btn`, `self._foo_lbl`, `self._foo_canvas`

## tkinter Patterns
- Dark theme: all windows use `BG`/`BG_PANEL`/`BG_CARD` backgrounds, `FG`/`FG_DIM` text
- Layout: mix of `pack` and `grid` within frames
- Modal windows: `Toplevel` + `grab_set()` + `wait_window()`
- Canvas-based rendering with `_render()` methods redrawing on state change
- `PhotoImage` refs kept alive on instance to prevent GC

## Error Handling
- User-facing errors via `messagebox.showerror()`
- Silent `except Exception: pass` for non-critical rendering paths

## Image Convention
- All images stored/processed as RGBA PIL Images
- Checkerboard background for transparency display
- Thumbnails computed via `_make_thumb()` / `_compose_thumb()` from image_helpers
