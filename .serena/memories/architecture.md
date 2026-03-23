# Architecture

## File Structure
- `gooey_sprites.py` — entry point, argparse, launches `SpriteGUI`
- `sprite_gui.py` — main window (`SpriteGUI` class); extraction, animation list, frame viewer, palette, undo, project save/load
- `frame_edit_window.py` — per-frame editor (`FrameEditWindow`); rotate/flip/warp/erase/draw/select tools, hitboxes
- `compose_window.py` — animation composer (`ComposeWindow`); timeline, source browser, preview, save
- `extract_sprites.py` — sprite extraction engine; blob detection, segmentation, auto-split, false-positive detection
- `image_helpers.py` — shared image utilities: `_CItem`, `_make_thumb`, `_apply_transform`, `_compose_thumb`, `_thumb_scale`
- `constants.py` — shared UI constants: colors (`BG`, `FG`, `ACCENT`, etc.), size constants, `PROJECT_EXT`, `PROJECT_VERSION`
- `project.py` — project file read/write: `_read_project`, `_write_project`, `_resolve_project_paths`
- `frame_ops.py` — frame-level operations: `_cmd_delete_frames`, `_cmd_duplicate_frame`, `_cmd_reorder_frames`
- `dialogs.py` — shared dialog widgets: `_InputDialog`, `_Tooltip`
- `sheet_viewer.py` — zoomable sprite sheet viewer panel

## Key Data Structures
- Animation folder: a directory containing `*.png` files + `frames.json`
- `frames.json`: `{"frames": [{"index": int, "file": str, "blobs": [...], "hitboxes": [...]}]}`
- `_CItem` (image_helpers): timeline item — `anim_dir`, `png`, `rotate`, transform state
- `SpriteGUI._managed_anims`: list of animation folder names owned by this project
- `SpriteGUI._flagged_anims`: set of animation folder names flagged as false positives

## Window Relationships
- `SpriteGUI` (main) spawns `FrameEditWindow` (modal Toplevel with grab_set)
- `SpriteGUI` spawns `ComposeWindow` (non-modal Toplevel)
- `ComposeWindow` spawns `FrameEditWindow` for per-slot editing
- All windows share the same `tk.Tk` root from `gooey_sprites.py`
