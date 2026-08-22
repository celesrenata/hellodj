# Whiteboard Enhancement Plan

inclusion: manual

## Bugs to Fix

### 1. Sticker picker clicks blocked (CRITICAL)
**Root cause:** When whiteboard is active, canvas is at `z-index: 35` with `pointer-events: auto`. Sticker picker is at `z-index: 25`. Canvas intercepts all clicks over the picker.

**Fix:** In `controls_passthrough.js`, add awareness of the sticker picker panel. When pointer is over `#sticker-picker`, disable canvas pointer-events (same pattern as bottom-controls). Alternatively, raise sticker-picker to `z-index: 40` in CSS.

**Simplest fix:** Just add `z-index: 40` to `.sticker-picker` in style.css. Since it already has `pointer-events: auto`, it just needs to be above the canvas.

## New Features

### 2. Sticker placement without drag (click-to-place)
Currently stickers require drag-to-size (like shapes). Better UX: click to place at default size, or drag to resize.

**Approach:** If pointerdown→pointerup without significant movement (<5px), place sticker at a default size (e.g., 15% of canvas width, preserving aspect ratio) centered on click position.

### 3. Rotation for stickers and shapes
Add rotation handle after placement, or a rotation input in the HUD when sticker/shape tool is active.

**Stroke schema change:** Add optional `rotation` field (radians) to stroke objects.
**Renderer change:** Apply `ctx.rotate(stroke.rotation)` around center point before drawing.
**UI:** 
- Option A: Rotation slider in HUD toolbar (0-360°)
- Option B: Rotation handle on preview (drag to rotate before finalizing)
- Option C: Both

**Sync:** The `rotation` field gets included in `stroke_add` WS message.

### 4. New tools (MS Paint style)

| Tool | Description | Priority |
|------|-------------|----------|
| Fill bucket | Flood-fill a closed region with color | Medium |
| Spray can | Random dots within radius on pointermove | Low |
| Select/Move | Click existing strokes to reposition | High |
| Stamp (click-to-place shape) | Single-click places a shape at fixed size | Medium |

### 5. Fill option for shapes
Currently shapes are outline-only. Add a "filled" toggle in the HUD when shape tool is active.

**Stroke schema change:** Add optional `filled: boolean` field.
**Renderer change:** Use `ctx.fill()` in addition to `ctx.stroke()` when filled=true.

## Implementation Order

1. **Fix sticker picker z-index** (CSS one-liner, deploy immediately)
2. **Click-to-place stickers** (StickerTool change, small)
3. **Rotation field + renderer support** (schema + renderer + HUD slider)
4. **Fill toggle for shapes** (schema + renderer + HUD toggle)
5. **Select/Move tool** (new tool class, complex — involves hit-testing and stroke repositioning)

## Files to modify

- `bot/video/activity_frontend/style.css` — z-index fix
- `bot/video/activity_frontend/sticker_tool.js` — click-to-place, rotation
- `bot/video/activity_frontend/shape_tool.js` — rotation, fill
- `bot/video/activity_frontend/renderer.js` — rotation transform, fill rendering
- `bot/video/activity_frontend/whiteboard.js` — HUD additions (rotation slider, fill toggle)
- `bot/video/activity_frontend/ws_whiteboard.js` — include rotation/filled in messages
- `bot/video/stroke_registry.py` — accept rotation/filled fields
- `bot/video/activity_frontend/index.html` — HUD markup additions
