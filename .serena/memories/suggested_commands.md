# Suggested Commands

## Run the Application
```
python gooey_sprites.py
python gooey_sprites.py path/to/sheet.gif --output ./sprites
python gooey_sprites.py myproject.ssproj
```

## Quick Syntax Check (no test suite exists)
```
python -c "import sprite_gui; import compose_window; import frame_edit_window; print('OK')"
```

## Install Dependencies
```
pip install pillow numpy
```

## Platform Notes (Windows)
- Shell: bash (Git Bash / WSL) or PowerShell
- Python command: `python` (not `python3`)
- Paths use forward slashes in code (`pathlib.Path` handles both)
- tkinter is bundled with standard Python on Windows — no separate install needed

## No Test Suite / Linter
- No pytest, unittest, flake8, black, or mypy configured
- Manual testing by running the app
- Verify imports compile after edits with the syntax check command above
