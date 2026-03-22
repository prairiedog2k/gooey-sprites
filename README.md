# Sprite Sheet Extractor

A desktop GUI application for extracting individual sprites from a sprite sheet image. Supports GIF, PNG, and JPEG input formats. Extracted sprites are saved as individual PNG files organized into named animation folders.

## Features

- Extract sprites from GIF, PNG, or JPEG sprite sheets
- Automatic blob detection with configurable gap and tolerance settings
- Auto-split multi-sprite frames into individual frames
- Replace palette colors with transparency and re-detect blobs
- Animation composer to assemble sprites into new animated GIFs
- Zoomable sprite sheet viewer
- Project files (`.ssproj`) preserve all settings and paths
- Recent projects list (last 4 opened)
- Undo support for most operations

## Requirements

- **Python 3.11+**
- **Pillow** — image loading, manipulation, and export
- **NumPy** — blob detection via alpha-channel column scanning
- **tkinter** — GUI (included with standard Python on Windows and macOS; on Linux install `python3-tk`)

## Installation

```bash
pip install pillow numpy
```

On Linux, also install tkinter if not already present:

```bash
# Debian/Ubuntu
sudo apt install python3-tk

# Fedora
sudo dnf install python3-tkinter
```

## Running

```bash
# Open the GUI
python gooey_sprites.py

# Open with a sprite sheet pre-loaded
python gooey_sprites.py path/to/sheet.gif
python gooey_sprites.py path/to/sheet.png
python gooey_sprites.py path/to/sheet.jpg

# Open a saved project
python gooey_sprites.py myproject.ssproj

# Specify an output folder
python gooey_sprites.py path/to/sheet.gif --output ./sprites
```

## Project Files

Projects are saved as `.ssproj` files (JSON). They store:

- Path to the sprite sheet (relative to the project file)
- A copy of the sprite sheet inside the project folder
- Output folder path
- Extraction settings (gap, tolerance, minimum pixel count)
- Animation folder names and flagged animations

Projects can be moved as a folder — paths are stored relative to the `.ssproj` file.

## Source Files

| File | Purpose |
|---|---|
| `gooey_sprites.py` | Entry point; argument parsing; launches the main window |
| `sprite_gui.py` | Main application window (`SpriteGUI`) |
| `extract_sprites.py` | Blob detection and sprite extraction logic |
| `compose_window.py` | Animation composer window |
| `frame_edit_window.py` | Per-frame crop/transform editor |
| `sheet_viewer.py` | Zoomable sprite sheet viewer window |
| `project.py` | `.ssproj` read/write helpers |
| `frame_ops.py` | Frame delete, duplicate, and reorder operations |
| `image_helpers.py` | Thumbnail generation and checker-background rendering |
| `dialogs.py` | Shared input dialog |
| `constants.py` | Colors, sizes, and other shared constants |
