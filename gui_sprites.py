#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui_sprites.py — GUI front-end for extract_sprites.py

Usage:
  python gui_sprites.py
  python gui_sprites.py path/to/sheet.gif
  python gui_sprites.py path/to/sheet.gif --output ./sprites
  python gui_sprites.py myproject.ssproj

Project files (.ssproj) are JSON and store the GIF path, output folder,
and extraction settings so a session can be resumed in one click.
"""

import argparse
from pathlib import Path

import tkinter as tk

from constants import PROJECT_EXT
from sprite_gui import SpriteGUI


def main():
    ap = argparse.ArgumentParser(description="Sprite Sheet Extractor GUI")
    ap.add_argument("file",   nargs="?", default="",
                    help=f"Sprite-sheet GIF or project file ({PROJECT_EXT}).")
    ap.add_argument("--output", "-o", default="",
                    help="Output folder (ignored when loading a project file).")
    args = ap.parse_args()

    root = tk.Tk()
    root.geometry("1200x720")
    app = SpriteGUI(root)

    if args.file:
        p = Path(args.file)
        if p.suffix.lower() == PROJECT_EXT:
            root.after(100, lambda: app._load_project_file(p))
        else:
            app.v_gif.set(str(p))
            if args.output:
                app.v_out.set(args.output)
            root.after(100, app._maybe_autoload)

    root.mainloop()


if __name__ == "__main__":
    main()
