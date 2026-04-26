# Slicer Dialog — Implementation Plan

Adds a **Slicer** window to gooey-sprites for extracting named body-part sprites
from existing animation frames. The output is ready-to-use PNG files sized to the
per-part canvas guidelines in `fat-sumo/SPRITES.md`.

---

## Overview

The workflow is:

1. Open the Slicer from `Frames → Slice…` in the main window (or right-click a frame).
2. Select a source frame from any loaded animation.
3. Draw a **lasso** (freehand polygon) or **box** region around a body part.
4. Assign a **part name** (from a structured catalogue or free-entry) and a **target canvas size**.
5. The selection is cropped, composited on a transparent canvas of the target size, and saved to
   `<output_dir>/<part-group>/<part-name>.png`.

Multiple slices can be extracted from a single frame in one session before closing.

---

## Part Catalogue

Drawn directly from `fat-sumo/SPRITES.md`. Each entry carries a **group** (subfolder),
**name** (filename stem), and **canvas** (target pixel size).

```python
PART_CATALOGUE = [
    # group              name                canvas (w, h)
    ("sumo_body",       "idle",              (128, 128)),
    ("sumo_body",       "pitch_up_1",        (128, 128)),
    ("sumo_body",       "pitch_up_2",        (128, 128)),
    ("sumo_body",       "pitch_up_3",        (128, 128)),
    ("sumo_body",       "pitch_down_1",      (128, 128)),
    ("sumo_body",       "pitch_down_2",      (128, 128)),
    ("sumo_body",       "pitch_down_3",      (128, 128)),
    # slim/heavy variants use e.g. "idle_slim", "idle_heavy"

    ("sumo_arm",        "arm_idle",          (96, 32)),
    ("sumo_arm",        "arm_extend_1",      (96, 32)),
    ("sumo_arm",        "arm_extend_2",      (96, 32)),
    ("sumo_arm",        "arm_extend_3",      (96, 32)),
    ("sumo_arm",        "arm_extend_4",      (96, 32)),
    ("sumo_arm",        "arm_grab",          (96, 32)),
    ("sumo_arm",        "arm_retract_1",     (96, 32)),
    ("sumo_arm",        "arm_retract_2",     (96, 32)),
    ("sumo_arm",        "arm_retract_3",     (96, 32)),

    ("sumo_face",       "face_neutral",      (48, 48)),
    ("sumo_face",       "face_strain",       (48, 48)),
    ("sumo_face",       "face_happy",        (48, 48)),
    ("sumo_face",       "face_scared",       (48, 48)),
    ("sumo_face",       "face_chewing",      (48, 48)),

    ("trampoline",      "mat_default",       (256, 32)),
    ("trampoline",      "mat_ok",            (256, 32)),
    ("trampoline",      "mat_good",          (256, 32)),
    ("trampoline",      "mat_perfect",       (256, 32)),
    ("trampoline",      "mat_used_miss",     (256, 32)),
    ("trampoline",      "mat_used_ok",       (256, 32)),
    ("trampoline",      "mat_used_good",     (256, 32)),
    ("trampoline",      "mat_used_perfect",  (256, 32)),
    ("trampoline",      "mat_bounce_1",      (256, 32)),
    ("trampoline",      "mat_bounce_2",      (256, 32)),
    ("trampoline",      "mat_bounce_3",      (256, 32)),
    ("trampoline",      "frame_legs",        (192, 48)),
    ("trampoline",      "star_logo",         (48, 48)),

    ("food_onigiri",    "idle",              (32, 32)),
    ("food_onigiri",    "eaten_1",           (48, 48)),
    ("food_onigiri",    "eaten_2",           (48, 48)),
    ("food_onigiri",    "eaten_3",           (48, 48)),
    ("food_mandarin",   "idle",              (32, 32)),
    ("food_mandarin",   "eaten_1",           (48, 48)),
    ("food_mandarin",   "eaten_2",           (48, 48)),
    ("food_mandarin",   "eaten_3",           (48, 48)),
    ("food_watermelon", "idle",              (32, 32)),
    ("food_watermelon", "eaten_1",           (48, 48)),
    ("food_watermelon", "eaten_2",           (48, 48)),
    ("food_watermelon", "eaten_3",           (48, 48)),
    ("food_mochi",      "idle",              (32, 32)),
    ("food_mochi",      "eaten_1",           (48, 48)),
    ("food_mochi",      "eaten_2",           (48, 48)),
    ("food_mochi",      "eaten_3",           (48, 48)),

    ("obstacles",       "spike_1",           (64, 64)),
    ("obstacles",       "spike_2",           (64, 64)),
    ("obstacles",       "spike_3",           (64, 64)),
    ("obstacles",       "spike_4",           (64, 64)),
    ("obstacles",       "spike_5",           (64, 64)),
    ("obstacles",       "spike_6",           (64, 64)),
    ("obstacles",       "hex_1",             (64, 64)),
    ("obstacles",       "hex_2",             (64, 64)),
    ("obstacles",       "hex_3",             (64, 64)),
    ("obstacles",       "hex_4",             (64, 64)),
    ("obstacles",       "hex_5",             (64, 64)),
    ("obstacles",       "hex_6",             (64, 64)),
    ("obstacles",       "elevation_arrow",   (64, 64)),

    ("effects",         "flash_1",           (128, 128)),
    ("effects",         "flash_2",           (128, 128)),
    ("effects",         "flash_3",           (128, 128)),
    ("effects",         "flash_4",           (128, 128)),
    ("effects",         "bonk_1",            (64, 64)),
    ("effects",         "bonk_2",            (64, 64)),
    ("effects",         "bonk_3",            (64, 64)),
    ("effects",         "bonk_4",            (64, 64)),
    ("effects",         "squish_1",          (64, 64)),
    ("effects",         "squish_2",          (64, 64)),
    ("effects",         "squish_3",          (64, 64)),
    ("effects",         "squish_4",          (64, 64)),
    ("effects",         "airjump_1",         (96, 96)),
    ("effects",         "airjump_2",         (96, 96)),
    ("effects",         "airjump_3",         (96, 96)),
    ("effects",         "airjump_4",         (96, 96)),
    ("effects",         "wind_1",            (96, 96)),
    ("effects",         "wind_2",            (96, 96)),
    ("effects",         "wind_3",            (96, 96)),
]
```

The catalogue lives in `slicer_catalogue.py` so it can be edited independently.
Free-entry (custom group + name + canvas) is also supported.

---

## New Files

| File | Purpose |
|---|---|
| `slicer_catalogue.py` | `PART_CATALOGUE` list; no imports from the rest of the app |
| `slicer_window.py` | `SlicerWindow` class — the full dialog |

One entry point in `sprite_gui.py`:
- Menu: `Frames → Slice…`
- Right-click context menu on a frame thumbnail: `Slice…`

Both call `SlicerWindow(parent, source_png, output_dir)`.

---

## Layout

```
┌──────────────────────────────────────────────────────────────────┐
│  Source: [anim name / frame name]          [← Prev]  [Next →]   │
├──────────────────┬───────────────────────────────────────────────┤
│  PART PANEL      │  CANVAS (zoomable, scrollable)                │
│                  │                                               │
│  Group filter:   │   [source frame, full resolution]             │
│  [dropdown]      │                                               │
│  Part list:      │   lasso outline / box drawn here              │
│  [scrollable     │                                               │
│   listbox with   │                                               │
│   group headers] │                                               │
│                  │                                               │
│  Output size:    │                                               │
│  W [___] H [___] │                                               │
│  (editable;      │                                               │
│   catalogue hint)│                                               │
│                  ├───────────────────────────────────────────────┤
│ ┌──────────────┐ │  PREVIEW (output size, centred crop)          │
│ │  LASSO  [L]  │ │                                               │
│ └──────────────┘ │   [result at 2× zoom]                         │
│  (press L or B)  │                                               │
│  [Extract]       │                                               │
│  [Clear]         │                                               │
│                  │                                               │
│  ── Extracted ── │                                               │
│  [scrollable     │                                               │
│   list of done   │                                               │
│   parts]         │                                               │
└──────────────────┴───────────────────────────────────────────────┘
```

---

## Tool Mode Indicator

The active tool is made unmistakably obvious through three simultaneous signals:

1. **Mode badge** — a large pill-shaped label sits in the top-left corner of the
   canvas at all times. It reads either **`✦ LASSO`** or **`☐ BOX`**, drawn
   directly on the canvas (not a widget, so it floats over the image).
   - Lasso badge: `YELLOW` background, dark text.
   - Box badge: `ACCENT` background, dark text.
   - Font size and badge dimensions are **fixed in screen pixels** — the badge
     does not scale with zoom.

2. **Cursor** — lasso mode uses a crosshair cursor (`"crosshair"`); box mode uses
   the default arrow cursor (`"arrow"`). The cursor changes the instant the mode
   switches, before any drawing begins.

3. **Tool button** in the side panel — the active button is rendered with a thick
   `2 px` coloured border and bold text; inactive button is flat and dim.
   - Lasso button border: `YELLOW`.
   - Box button border: `ACCENT`.

---

## Canvas Interaction

### Tool toggle
- **`L` key** → switch to Lasso mode.
- **`B` key** → switch to Box mode.
- Pressing the key while a selection is in progress **cancels** the current
  selection first, then switches mode (no partial lasso is left on screen).
- Clicking either tool button in the side panel has the same effect.

### Zoom
- **Ctrl + scroll wheel** zooms the source image in the canvas.
- Zoom range: 1× – 32× (integer steps preferred: 1, 2, 4, 8, 16, 32).
- A zoom level label (`2×`, `8×`, …) is shown in the canvas header row.
- **Only the source image pixels scale with zoom.** All selection drawing is
  done in fixed screen-pixel units regardless of zoom level:
  - Outline stroke width: always **1 px** screen.
  - Lasso vertex handles: always **5 px** radius screen circles.
  - Box corner handles: always **5 px** screen squares.
  - Dash pattern: always `(4, 3)` screen pixels.
  - Mode badge: fixed size (see Tool Mode Indicator).
  This ensures the selection remains easy to draw and see at any zoom level.

### Pixel grid
- Drawn **behind** the source image (bottom layer, before image compositing).
- Each cell represents **one source pixel** scaled by the current zoom.
- Only rendered when zoom ≥ **4×** (at lower zoom the grid would be
  sub-pixel and meaningless).
- Grid line colour: `#2a2a3e` (just slightly lighter than `BG`, very subtle).
- Grid lines are always **1 px** screen width regardless of zoom.
- At zoom ≥ 16×, every **8th** grid line is drawn slightly brighter (`BG_CARD`)
  to form an 8-pixel major grid — matching common sprite-art tile sizes.

### Lasso tool
- **Click** to place polygon vertices.
- **Double-click** (or click near first point within **8 screen px**) to close.
- Closed polygon: dashed `YELLOW` outline, 25 % opacity `YELLOW` fill.
- Open polygon: real-time rubber-band segment from last vertex to cursor.
- All geometry stored in **image-space coordinates** (pre-zoom); display coords
  are derived on each redraw.

### Box tool
- **Click-drag** to draw a rectangle.
- Live rubber-band rect: dashed `ACCENT` outline, 25 % opacity `ACCENT` fill.
- Geometry stored in **image-space coordinates**.

### Both tools
- **Escape** or **Clear** button cancels the current selection and clears the overlay.
- **Scroll wheel** (without Ctrl) scrolls the canvas vertically.
- **Shift-drag** pans the canvas.

---

## Extraction Logic (`_do_extract`)

1. Selection geometry is already stored in image-space coordinates — no conversion needed.
2. Compute the **tight bounding box** of the selection in image pixels.
3. Mask the source image with the lasso polygon (pixels outside the polygon are made transparent). Box tool skips masking — straight crop.
4. Crop to the bounding box.
5. **Fit into output canvas**: the output canvas size is whatever is in the W/H fields
   at the time of extraction (pre-filled from the catalogue selection, freely editable).
   Scale the crop down uniformly if it exceeds the output canvas in either dimension;
   never scale up. Centre the result on a transparent canvas of exactly that size.
6. Determine output path: `<output_dir>/<group>/<name>.png`.
   - If the file already exists, prompt: **Overwrite / Save as…/ Cancel**.
7. Save as RGBA PNG.
8. Append `(group, name, output_path)` to the extracted list in the part panel.
9. Clear the current selection. Advance part-list highlight to the next uncompleted part in the same group.

---

## Part List Behaviour

- Groups are shown as non-selectable headers (bold, `ACCENT` colour).
- Each part row shows: checkbox (✓ = already extracted to disk), name, canvas hint size.
- Selecting a row pre-fills the **Output size** W/H fields with the catalogue hint.
  The fields remain fully editable — the hint is a starting point, not a constraint.
  Free-entry rows also enable the name field for editing.
- Completed parts (output file exists) shown in `FG_DIM`; still selectable for re-extraction.
- **Group filter dropdown** at the top hides/shows groups — useful for focusing on one character part at a time.

---

## Source Navigation

- The dialog opens on the frame that was right-clicked / selected.
- `← Prev` / `Next →` buttons cycle through all frames of the source animation (root frames only; branch frames are excluded from the cycle).
- The current frame label shows `<anim_name> / <filename>`.

---

## Output Folder

Defaults to `<output_dir>/sliced/`. Shown as an editable path field in the header with a `Browse…` button. The chosen path is persisted in `~/.gooey-sprites/prefs.json` under key `"last_slice_dir"`.

The resulting directory tree:

```
sliced/
├── sumo_body/
│   ├── idle.png
│   ├── pitch_up_1.png
│   └── …
├── sumo_arm/
├── sumo_face/
├── trampoline/
├── food_onigiri/
├── food_mandarin/
├── food_watermelon/
├── food_mochi/
├── obstacles/
└── effects/
```

---

## Integration Points in `sprite_gui.py`

- [ ] Add `Frames → Slice…` menu item.
- [ ] Add `Slice…` to the right-click context menu on frame thumbnails.
- [ ] Both call `SlicerWindow(self.root, selected_png, self._output_dir)`.

---

## Implementation Order

1. `slicer_catalogue.py` — static data only, no dependencies.
2. `slicer_window.py` — `SlicerWindow`:
   - a. Window shell, layout skeleton, zoom/pan, zoom label.
   - b. Pixel grid rendering (behind image; threshold at 4×; major grid at 16×).
   - c. Source image compositing over grid; screen-pixel invariant for all overlays.
   - d. Part panel: catalogue listbox, group filter, output size W/H fields.
   - e. Tool mode indicator: badge, cursor swap, button styling; `L`/`B` keys.
   - f. Box tool: image-space geometry, rubber-band display, preview panel.
   - g. Lasso tool: image-space geometry, vertex handles, close detection, fill.
   - h. Extraction logic: bounding box, lasso mask, fit-to-output-canvas, file save.
   - i. Extracted list, overwrite prompt, prefs persistence.
3. `sprite_gui.py` — wire menu and right-click entry points.
