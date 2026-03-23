# What To Do When a Task Is Completed

1. **Verify imports compile** — run:
   ```
   python -c "import sprite_gui; import compose_window; import frame_edit_window; print('OK')"
   ```

2. **Check for cross-file consistency** — edits to shared interfaces (e.g. `FrameEditWindow.__init__` signature, `on_save` callback shape, `_managed_anims` list) must be reflected in all callers:
   - `sprite_gui.py` calls `FrameEditWindow` and `ComposeWindow`
   - `compose_window.py` calls `FrameEditWindow`
   - Both callers manage `_managed_anims` / `_flagged_anims` lists

3. **No automated tests or linting** — manual review only.

4. **No build step** — Python source runs directly.
