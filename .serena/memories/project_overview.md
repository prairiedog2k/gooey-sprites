# Project Overview

**gooey-sprites** is a desktop GUI application for extracting sprites from sprite sheet images (GIF/PNG/JPEG) and composing them into animations.

## Purpose
- Extract individual sprite frames from sprite sheets using blob detection
- Organize frames into named animation folders with metadata (`frames.json`)
- Edit individual frames (rotate, flip, warp, erase, draw, select/transform regions)
- Compose new animations from existing frames in a timeline editor
- Save/load project state via `.ssproj` files (JSON)

## Entry Point
```
python gooey_sprites.py
python gooey_sprites.py path/to/sheet.gif
python gooey_sprites.py myproject.ssproj
```

## Project File Format
`.ssproj` files are JSON storing: gif path, output dir, gap/tolerance/min_pixels settings, list of managed animation folder names, flagged animations.
