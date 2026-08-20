# Implementation Plan: Video Whiteboard

## Overview

Add a collaborative real-time whiteboard overlay to the existing Video Activity player. The whiteboard enables all connected viewers to draw annotations (freehand, lines, shapes, text, stickers) on a transparent canvas above the video, with strokes synchronized via the existing WebSocket infrastructure and scoped to the session lifecycle.

The implementation extends three backend components (StrokeRegistry, WebSocketHub, SessionRegistry lifecycle hooks) and adds a frontend canvas overlay with drawing tools, HUD, sticker picker, hit-testing, and coordinate normalization.

## Prerequisites

- Video Activity feature must be implemented (Activity Frontend with hls.js player, WebSocketHub, SessionRegistry)
- `hypothesis` available for Python property-based tests
- `stickers/` directory with zip files for sticker content (can be empty for initial dev)

## Tasks

- [ ] 1. Backend: StrokeRegistry and WebSocketHub extension
  - [ ] 1.1 Create `bot/video/stroke_registry.py` with `StrokeData` and `StrokeRegistry`
    - Add `StrokeData` dataclass with fields: id, type, author, color, width, points, text, text_bg, sticker_category, sticker_filename
    - Add `StrokeRegistry` class with MAX_STROKES=500, methods: add(), remove(), clear(), get_all(), __len__()
    - add() returns False when at capacity; remove() returns False when ID not found
    - get_all() returns list of dicts in insertion order (using dataclasses.asdict)
    - _Requirements: 10.2, 10.6, 10.11, 11.1, 11.5, 12.3_

  - [ ] 1.2 Extend `bot/video/ws_hub.py` with stroke registry management
    - Add `_stroke_registries: dict[int, StrokeRegistry]` to WebSocketHub.__init__
    - Add `get_stroke_registry(guild_id)`, `clear_stroke_registry(guild_id)`, `init_stroke_registry(guild_id)` methods
    - Add `_VALID_STROKE_TYPES` set constant
    - Add `_send_error(ws, message)` helper method
    - _Requirements: 10.2, 10.6, 12.1, 12.4_

  - [ ] 1.3 Add `stroke_add` handler to WebSocketHub
    - Implement `_handle_stroke_add(guild_id, sender, data)` with full field validation
    - Validate required fields: id, stroke_type, points, color, width, author
    - Validate stroke_type against _VALID_STROKE_TYPES enum set
    - Validate non-empty points array
    - Validate sticker-specific fields (sticker_category, sticker_filename) for type "sticker"
    - Store in registry, broadcast to other viewers (exclude sender)
    - Send error if registry at capacity (500 strokes)
    - Wire into existing `_handle_message` dispatch
    - _Requirements: 10.1, 10.2, 10.11, 10.12_

  - [ ] 1.4 Add `stroke_remove` and `whiteboard_reset` handlers to WebSocketHub
    - Implement `_handle_stroke_remove(guild_id, sender, data)` — validate ID, remove from registry, broadcast
    - Silently ignore removal of non-existent IDs (idempotent)
    - Implement `_handle_whiteboard_reset(guild_id, sender, data)` — clear registry, broadcast
    - Wire both into `_handle_message` dispatch
    - _Requirements: 10.4, 10.6, 10.8, 10.9_

  - [ ] 1.5 Update late-joiner state message to include strokes
    - In the `handle_ws` connection handler, add `strokes` field to the initial `state` message
    - Call `get_stroke_registry(guild_id).get_all()` for the strokes array
    - Empty registry sends empty array
    - _Requirements: 10.10, 11.1, 11.2, 11.4_

  - [ ] 1.6 Add session lifecycle hooks for whiteboard state
    - In `ActivityStreamer.play()`: call `ws_hub.init_stroke_registry(guild_id)` when starting a new session (state IDLE)
    - In `ActivityStreamer.stop()`: call `ws_hub.clear_stroke_registry(guild_id)` and broadcast `whiteboard_clear`
    - In `ActivityStreamer.skip()` / `_auto_advance()`: make NO changes to stroke registry (strokes persist across videos)
    - _Requirements: 12.1, 12.2, 12.4_

- [ ] 2. Backend: Sticker catalog
  - [ ] 2.1 Create `bot/video/sticker_catalog.py` with `StickerCatalog` class
    - Implement `StickerCatalog` with __init__(stickers_dir), load(), _load_zip(), _slugify()
    - Support .png, .gif, .webp image extensions
    - Skip corrupt/empty zips gracefully with logging
    - Extract zip contents into in-memory cache (category_slug → {filename: bytes})
    - Derive category display name from zip filename (strip timestamp suffixes)
    - _Requirements: 15.10, 15.11, 15.12_

  - [ ] 2.2 Add HTTP endpoints for sticker catalog and image serving
    - Implement `handle_sticker_catalog(request)` → GET /activity/stickers/catalog → JSON response
    - Implement `handle_sticker_image(request)` → GET /activity/stickers/{category}/{filename} → image bytes
    - Return 404 for missing category/filename
    - Set Cache-Control header (24h) on image responses
    - Infer content-type from file extension
    - Register routes in activity.py application setup
    - Initialize StickerCatalog at server startup (call .load())
    - _Requirements: 15.10, 15.11_

- [ ] 3. Checkpoint — Backend tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 4. Frontend: Canvas overlay and mode activation
  - [ ] 4.1 Add whiteboard canvas element and HUD DOM to `index.html`
    - Add `<canvas id="whiteboard-canvas">` element (z-index: 20)
    - Add `<div class="whiteboard-hud">` with tool buttons, color palette, undo, reset (z-index: 25)
    - Add `<div class="sticker-picker">` panel (hidden by default)
    - Add whiteboard toggle button to existing player controls overlay
    - Set initial state: HUD hidden, canvas pointer-events none
    - _Requirements: 1.1, 1.3, 1.7_

  - [ ] 4.2 Create `bot/video/activity/whiteboard.js` — WhiteboardOverlay class
    - Implement mode toggle: activate() shows HUD + enables pointer-events on canvas; deactivate() hides HUD + disables pointer-events
    - Maintain strokes Map (id → Stroke), currentTool, currentColor, localAuthorId, undoStack
    - Implement addStroke(), removeStroke(), clearAll(), redraw(), resize()
    - Wire toggle button click to activate/deactivate
    - Set initial mode to inactive with HUD hidden
    - Visual indicator on toggle button for active/inactive state
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 1.6, 1.7_

  - [ ] 4.3 Add whiteboard CSS styles to `style.css`
    - Style canvas overlay: position absolute, top 0, left 0, 100% width/height, z-index 20
    - Style HUD toolbar: positioned at top, z-index 25, flexbox layout
    - Style tool buttons, color swatches, dividers
    - Style sticker picker panel
    - Pointer-events toggling based on mode active/inactive
    - _Requirements: 1.3, 14.1_

- [ ] 5. Frontend: Coordinate normalization and stroke renderer
  - [ ] 5.1 Create `bot/video/activity/coords.js` — normalization module
    - Implement normalize(pixelX, pixelY, width, height) → [nx, ny] clamped to [0, 1] with 4 decimal places
    - Implement denormalize(normX, normY, width, height) → [px, py]
    - Implement normalizeWidth(cssPixels, viewportWidth) → normalized float
    - Implement denormalizeWidth(normalizedWidth, viewportWidth) → CSS pixels
    - _Requirements: 13.1, 13.2, 13.4, 13.5_

  - [ ] 5.2 Create `bot/video/activity/renderer.js` — StrokeRenderer class
    - Implement renderStroke(stroke) dispatcher by type
    - Implement renderFreehand() with quadratic Bézier interpolation (midpoint algorithm)
    - Implement renderLine() and renderArrow() (line + arrowhead)
    - Implement renderRect() (outline only, not filled)
    - Implement renderEllipse() (outline only)
    - Implement renderText() with optional background (50% opacity black, 4px padding)
    - Implement renderSticker() with aspect-ratio-preserving letterbox fit within bounding box
    - Add image cache (Map<url, HTMLImageElement>) with onload → redraw trigger
    - _Requirements: 2.4, 2.6, 4.6, 5.6, 5.7, 13.2, 15.7, 15.8_

- [ ] 6. Frontend: Drawing tools
  - [ ] 6.1 Create `bot/video/activity/tools.js` — ToolManager and DrawingTool interface
    - Implement ToolManager class with tools Map and activeTool state
    - Define DrawingTool interface: name, cursor, onPointerDown, onPointerMove, onPointerUp, onCancel, renderPreview
    - Implement selectTool(name) with cursor update on canvas
    - _Requirements: 2.1_

  - [ ] 6.2 Implement PenTool
    - Capture normalized points on pointermove
    - Finalize stroke on pointerup (type: "freehand", N≥2 points)
    - Finalize on pointer leaving canvas bounds
    - Use current color, fixed width 3px (normalized)
    - Render live preview during draw
    - _Requirements: 2.1, 2.2, 2.3, 2.5_

  - [ ] 6.3 Implement LineTool
    - Record start point on pointerdown (normalized)
    - Render preview line on pointermove
    - Finalize 2-point line stroke on pointerup
    - Finalize at last valid position if pointer leaves canvas
    - Discard zero-length lines (start == end)
    - Use current color, fixed width 3px (normalized)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [ ] 6.4 Implement ShapeTool (rect, ellipse, arrow sub-types)
    - Sub-menu for shape type selection (rect, ellipse, arrow)
    - Record start point on pointerdown
    - Render preview of selected shape within bounding box on pointermove
    - Finalize if bounding box > 5px in both dimensions
    - Discard if bounding box ≤ 5px in either dimension
    - Clamp to overlay edges if pointer leaves bounds
    - Use current color, fixed width 3px, outline only (not filled)
    - Arrow renders with proportional arrowhead at endpoint
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8_

  - [ ] 6.5 Implement TextTool
    - On click: show text input at clicked position (normalized coords)
    - Max length 200 characters
    - On Enter or blur: finalize text stroke with content + text_bg toggle state
    - Reject empty/whitespace-only input (no stroke created)
    - On Escape: cancel without creating stroke
    - Use current color, font size 16px
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_

  - [ ] 6.6 Implement EraserTool
    - On click: hit-test against all strokes (topmost first)
    - 5px tolerance from path centerline (normalized to viewport)
    - Remove hit stroke locally + send stroke_remove via WebSocket
    - Show distinct eraser cursor
    - No action if no stroke within tolerance
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

  - [ ] 6.7 Implement StickerTool
    - On activation: show Sticker_Picker panel
    - On deactivation: hide Sticker_Picker panel
    - Require sticker selection before placement (no-op if none selected)
    - Same drag pattern as ShapeTool (pointerdown → pointermove → pointerup)
    - Preview selected sticker image within bounding box during drag
    - Finalize if bbox > 5px in both dims
    - Cap bounding box at 50% overlay width and 50% overlay height
    - Produce stroke with type "sticker", sticker_category, sticker_filename
    - _Requirements: 15.1, 15.4, 15.5, 15.6, 15.9_

- [ ] 7. Frontend: HUD interactions, color picker, undo, reset
  - [ ] 7.1 Implement Color_Picker in Whiteboard HUD
    - 8 preset color swatches (white, red, orange, yellow, green, cyan, blue, purple)
    - Custom color input via native browser color picker
    - Visual highlight on active color swatch
    - Persist selected color in localStorage (hex string)
    - Default to #FFFFFF if no stored color or stored value invalid
    - Apply selected color to all subsequent strokes
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [ ] 7.2 Implement undo button logic
    - Remove most recent stroke by THIS viewer (not other viewers)
    - Send stroke_remove WebSocket message with that stroke's ID
    - Visually disable button when no strokes to undo (reduced opacity, non-interactive)
    - Support sequential undo back to beginning of viewer's history
    - Wire Ctrl+Z / Cmd+Z keyboard shortcut
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.6_

  - [ ] 7.3 Implement reset button logic
    - Show confirmation prompt before proceeding
    - On confirm: clear all strokes locally, send whiteboard_reset via WebSocket
    - Clear undo history for all viewers
    - On cancel: no action
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.6, 9.7_

  - [ ] 7.4 Implement text background toggle
    - Checkbox in HUD for text background on/off
    - When enabled: text strokes render with 50% opacity black background + 4px padding
    - State passed to text strokes as text_bg field
    - _Requirements: 5.7_

- [ ] 8. Frontend: Hit-testing
  - [ ] 8.1 Create `bot/video/activity/hittest.js` — hit-testing module
    - Implement hitTest(clickX, clickY, strokes, tolerancePx, viewportWidth, viewportHeight)
    - Iterate strokes in reverse insertion order (topmost first)
    - For freehand: minimum distance to any line segment between consecutive points
    - For line/arrow: distance to the line segment
    - For rect: distance to any of 4 edge segments
    - For ellipse: distance to ellipse perimeter (sampled approximation)
    - For text: bounding box hit test
    - For sticker: bounding box hit test (same logic as rect, uses 2-point bounding box)
    - Implement distToSegment() helper
    - Normalize tolerance to viewport (tolX = tolerancePx / viewportWidth, tolY = tolerancePx / viewportHeight)
    - Return topmost hit stroke or null
    - _Requirements: 7.2, 7.3, 7.4, 7.5, 15.9_

- [ ] 9. Frontend: Sticker Picker UI
  - [ ] 9.1 Create `bot/video/activity/sticker_picker.js` — StickerPicker class
    - Implement show(): fetch catalog from /activity/stickers/catalog (cache after first fetch)
    - Implement hide(): hide picker panel
    - Render category tabs/accordion with clickable navigation
    - Render thumbnail grid for selected category (max 64×64px, preserve aspect ratio)
    - Image src: /activity/stickers/{category_slug}/{filename}
    - On thumbnail click: set selectedSticker and call onSelect callback to StickerTool
    - Handle fetch failure: show "Stickers unavailable" message, retry on next activation
    - _Requirements: 15.2, 15.3_

- [ ] 10. Frontend: WebSocket sync integration
  - [ ] 10.1 Integrate whiteboard with existing WebSocket client
    - Handle incoming `stroke_add` messages: parse and render stroke on canvas
    - Handle incoming `stroke_remove` messages: remove stroke from map, redraw
    - Handle incoming `whiteboard_reset` messages: clear all strokes, redraw
    - Handle incoming `whiteboard_clear` messages (from session end): clear all strokes, deactivate mode
    - Handle `state` message `strokes` array on connection: render all strokes before enabling drawing
    - Send stroke_add on tool finalization (pointerup)
    - Send stroke_remove on eraser/undo
    - Send whiteboard_reset on confirmed reset
    - Handle errors from server (display toast or log)
    - _Requirements: 10.1, 10.3, 10.4, 10.5, 10.7, 10.8, 10.10, 11.3_

  - [ ] 10.2 Implement controls region event passthrough
    - While whiteboard mode is active: detect pointer position within controls overlay region
    - Set canvas pointer-events to none for that region (allow events to reach controls)
    - Controls appear on hover/tap with same auto-hide timeout as normal mode
    - Touch devices: taps in controls region treated as controls interaction, not drawing
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5_

  - [ ] 10.3 Implement canvas resize handling
    - Listen to ResizeObserver on canvas container
    - On resize: update canvas dimensions, recalculate all stroke pixel positions from normalized coords, redraw
    - Skip redraw if dimensions are 0
    - _Requirements: 13.3_

  - [ ] 10.4 Implement undo history restore on reconnect
    - On WebSocket reconnect: rebuild undoStack from strokes authored by localAuthorId in the state message
    - Maintain insertion order in undoStack
    - _Requirements: 8.5_

- [ ] 11. Checkpoint — Frontend integration complete
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 12. Property-based tests (backend — Python/Hypothesis)
  - [ ]* 12.1 Write property test for coordinate normalization and clamping (Property 1)
    - **Property 1: Coordinate normalization and clamping**
    - Generate random (px, py, w, h) with w,h > 0 → verify normalize() output in [0.0, 1.0]
    - Include negative coordinates and coordinates exceeding viewport
    - **Validates: Requirements 2.2, 3.2, 4.2, 13.1, 13.4**

  - [ ]* 12.2 Write property test for coordinate normalization round-trip (Property 2)
    - **Property 2: Coordinate normalization round-trip**
    - Generate random in-bounds (px, py, w, h) → normalize → denormalize → within 1px of original
    - **Validates: Requirements 13.2, 13.3**

  - [ ]* 12.3 Write property test for stroke width normalization round-trip (Property 3)
    - **Property 3: Stroke width normalization round-trip**
    - Generate random (pixels, width) with both > 0 → normalizeWidth → denormalizeWidth → within 0.1px
    - **Validates: Requirements 13.5**

  - [ ]* 12.4 Write property test for stroke message completeness (Property 4)
    - **Property 4: Stroke message completeness**
    - Generate random valid stroke data → serialize to WebSocket message → verify all required fields present and valid
    - **Validates: Requirements 2.3, 3.4, 10.1, 11.4**

  - [ ]* 12.5 Write property test for degenerate stroke rejection (Property 5)
    - **Property 5: Degenerate stroke rejection**
    - Generate zero-length lines (start == end) → verify no stroke produced
    - Generate shapes with bbox ≤ 5px in either dimension → verify no stroke produced
    - **Validates: Requirements 3.7, 4.4, 4.5**

  - [ ]* 12.6 Write property test for text input validation (Property 6)
    - **Property 6: Text input validation**
    - Generate whitespace-only strings → verify rejection
    - Generate non-whitespace strings (1–200 chars) → verify acceptance with content preserved
    - **Validates: Requirements 5.3, 5.4**

  - [ ]* 12.7 Write property test for color persistence round-trip (Property 8)
    - **Property 8: Color persistence round-trip**
    - Generate valid 7-char hex strings (#[0-9A-Fa-f]{6}) → store → retrieve → verify same
    - Generate invalid strings → retrieve → verify returns #FFFFFF
    - **Validates: Requirements 6.5**

  - [ ]* 12.8 Write property test for undo removes author's most recent stroke (Property 10)
    - **Property 10: Undo removes author's most recent stroke**
    - Generate random stroke sequences with multiple authors → undo for specific author → verify correct stroke removed
    - **Validates: Requirements 8.2, 8.4, 8.5, 10.5**

  - [ ]* 12.9 Write property test for reset clears entire registry (Property 11)
    - **Property 11: Reset clears entire registry**
    - Generate registries with 0–500 strokes → reset → verify size 0
    - **Validates: Requirements 9.3, 9.5, 9.6**

  - [ ]* 12.10 Write property test for StrokeRegistry add/remove invariants (Property 12)
    - **Property 12: Stroke_Registry add/remove invariants**
    - Generate random add/remove sequences → verify: add when N<500 increases to N+1; add at 500 rejected; remove valid ID decreases by 1; remove invalid ID unchanged
    - **Validates: Requirements 10.2, 10.6, 10.11, 11.5**

  - [ ]* 12.11 Write property test for late-joiner receives complete ordered state (Property 13)
    - **Property 13: Late-joiner receives complete ordered stroke state**
    - Generate registries with N strokes → get_all() → verify N entries in insertion order with all fields
    - **Validates: Requirements 10.10, 11.1, 11.2, 11.4**

  - [ ]* 12.12 Write property test for invalid message rejection (Property 14)
    - **Property 14: Invalid message rejection**
    - Generate stroke_add messages with randomly removed required fields → verify rejection without registry modification
    - Generate messages with invalid stroke_type → verify rejection
    - Generate messages with empty points array → verify rejection
    - **Validates: Requirements 10.12**

  - [ ]* 12.13 Write property test for session lifecycle (Property 15)
    - **Property 15: Session lifecycle initializes and clears stroke state**
    - Simulate session start → verify empty registry
    - Simulate session end → verify registry cleared to 0
    - **Validates: Requirements 12.1, 12.4**

  - [ ]* 12.14 Write property test for video skip preserves strokes (Property 16)
    - **Property 16: Video skip preserves whiteboard strokes**
    - Generate registries with N strokes → simulate skip → verify registry size still N with identical contents
    - **Validates: Requirements 12.2**

  - [ ]* 12.15 Write property test for Bézier interpolation midpoints (Property 18)
    - **Property 18: Bézier interpolation produces smooth midpoints**
    - Generate random point sequences (N≥3) → verify each intermediate endpoint is midpoint of consecutive input points
    - **Validates: Requirements 2.4, 2.6**

  - [ ]* 12.16 Write property test for sticker bounding box size cap (Property 19)
    - **Property 19: Sticker bounding box size cap**
    - Generate random bounding boxes of arbitrary size → apply cap → verify neither dimension exceeds 50% of overlay
    - **Validates: Requirements 15.5**

  - [ ]* 12.17 Write property test for sticker catalog discovery (Property 20)
    - **Property 20: Sticker catalog discovery**
    - Generate random sets of zip files (valid, corrupt, empty-of-images) in temp directory → load catalog → verify valid zips present, invalid excluded
    - **Validates: Requirements 15.11, 15.12**

  - [ ]* 12.18 Write property test for sticker stroke message completeness (Property 21)
    - **Property 21: Sticker stroke message completeness**
    - Generate sticker stroke messages → verify sticker_category and sticker_filename present
    - Generate sticker messages missing those fields → verify rejection
    - **Validates: Requirements 15.5, 15.9, 15.10**

  - [ ]* 12.19 Write property test for hit-testing correctness (Property 9)
    - **Property 9: Hit-testing correctness**
    - Generate random strokes + click points → verify returns topmost stroke within 5px tolerance or null
    - **Validates: Requirements 7.2, 7.3, 7.4**

  - [ ]* 12.20 Write property test for color selection applies to subsequent strokes (Property 7)
    - **Property 7: Color selection applies to subsequent strokes**
    - Generate random valid hex colors → set color → simulate draws → verify all strokes have that color
    - **Validates: Requirements 6.3**

  - [ ]* 12.21 Write property test for controls region event passthrough (Property 17)
    - **Property 17: Controls region event passthrough**
    - Generate random pointer positions + controls rect → verify points inside controls rect are passed through (not captured by canvas)
    - **Validates: Requirements 14.2, 14.5**

- [ ] 13. Integration tests
  - [ ]* 13.1 Write WebSocket round-trip integration tests
    - Client sends stroke_add → second client receives broadcast
    - Client sends sticker stroke_add → second client receives broadcast with sticker fields
    - Client sends stroke_remove → verify removal in registry + broadcast to others
    - Client sends whiteboard_reset → verify registry cleared + broadcast to others
    - _Requirements: 10.1, 10.2, 10.6, 10.9_

  - [ ]* 13.2 Write late-joiner integration test
    - Add strokes to registry → connect new client → verify state message includes all strokes (including sticker strokes)
    - Connect to empty session → verify empty strokes array
    - _Requirements: 11.1, 11.2, 11.3_

  - [ ]* 13.3 Write session lifecycle integration tests
    - Start session → verify empty registry initialized
    - Stop session → verify registry cleared and clients notified with whiteboard_clear
    - Skip video → verify strokes preserved
    - _Requirements: 12.1, 12.2, 12.4_

  - [ ]* 13.4 Write capacity integration test
    - Add 500 strokes → verify 501st rejected with error message to sender
    - Verify no broadcast for rejected stroke
    - _Requirements: 10.11, 11.5_

  - [ ]* 13.5 Write sticker catalog endpoint integration tests
    - Start server with test zip files → GET /activity/stickers/catalog → verify JSON structure with correct categories
    - GET /activity/stickers/{category}/{filename} → verify correct image bytes and content-type
    - GET /activity/stickers/invalid/missing.png → verify 404 response
    - _Requirements: 15.10, 15.11, 15.12_

- [ ] 14. Final checkpoint — All tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties (21 properties from design)
- Unit tests validate specific examples and edge cases
- Backend is Python (aiohttp), frontend is vanilla JavaScript (no build step)
- Frontend files live in `bot/video/activity/` alongside the existing Activity player
- Sticker zip files go in `stickers/` at project root

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "5.1"] },
    { "id": 1, "tasks": ["1.2", "2.1", "5.2"] },
    { "id": 2, "tasks": ["1.3", "1.4", "2.2", "4.1"] },
    { "id": 3, "tasks": ["1.5", "1.6", "4.2", "4.3"] },
    { "id": 4, "tasks": ["6.1", "8.1", "9.1"] },
    { "id": 5, "tasks": ["6.2", "6.3", "6.4", "6.5", "6.6", "6.7"] },
    { "id": 6, "tasks": ["7.1", "7.2", "7.3", "7.4"] },
    { "id": 7, "tasks": ["10.1", "10.2", "10.3", "10.4"] },
    { "id": 8, "tasks": ["12.1", "12.2", "12.3", "12.4", "12.5", "12.6", "12.7", "12.8", "12.9", "12.10", "12.11", "12.12", "12.13", "12.14", "12.15", "12.16", "12.17", "12.18", "12.19", "12.20", "12.21"] },
    { "id": 9, "tasks": ["13.1", "13.2", "13.3", "13.4", "13.5"] }
  ]
}
```
