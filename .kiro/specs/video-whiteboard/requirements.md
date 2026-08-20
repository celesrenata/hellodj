# Requirements Document

## Introduction

This feature adds a collaborative whiteboard drawing overlay on top of the existing video Activity player in the HelloDJ Discord bot. The whiteboard enables all connected viewers in a voice channel session to draw, annotate, and highlight content on the video in real-time. Drawing state is synchronized across all participants via the existing WebSocket infrastructure and persists for the duration of the video session.

The whiteboard HUD is accessed from the existing on-hover/tap player options menu and renders as a transparent canvas layer above the video element. All drawing operations are broadcast to other viewers and rendered collaboratively.

The whiteboard also supports placing pre-made sticker images from categorized sticker packs. Sticker assets are stored as zip files in the `stickers/` project directory, with each zip representing a category. The Activity_Backend serves extracted sticker images and exposes a discovery API so the frontend can build a sticker picker UI.

## Glossary

- **Whiteboard_Overlay**: A transparent HTML5 Canvas element positioned above the video player that captures drawing input and renders strokes from all participants
- **Drawing_Tool**: A user-selectable instrument (pen, line, shape, text, eraser) that determines how pointer input is interpreted and rendered on the Whiteboard_Overlay
- **Stroke**: A single atomic drawing operation (freehand path, line, shape, or text placement) created by one viewer, identified by a unique stroke ID
- **Stroke_Registry**: The in-memory collection of all active strokes for a guild session, maintained server-side in the WebSocketHub for late-joiner sync
- **Color_Picker**: A UI component allowing the viewer to select a drawing color from a palette or custom color input
- **Whiteboard_HUD**: The toolbar UI containing drawing tools, color picker, undo button, and reset button, shown when whiteboard mode is active
- **Activity_Frontend**: The existing HTML/JS application (index.html, app.js, style.css) served at `/activity/` that contains the hls.js video player
- **Activity_Backend**: The existing Python aiohttp server that serves the Activity_Frontend static files, handles the WebSocket endpoint, and provides API endpoints for the activity
- **WebSocketHub**: The existing per-guild WebSocket connection manager that synchronizes playback state between all connected Activity clients
- **Session**: A per-guild Activity lifecycle from launch through playback to teardown; whiteboard state is scoped to and cleared with the session
- **Sticker**: A pre-made image asset that a viewer can place on the Whiteboard_Overlay at a chosen position and size, synchronized as a Stroke with type "sticker"
- **Sticker_Category**: A collection of related sticker images derived from a single zip file in the `stickers/` directory; the category name is extracted from the zip filename
- **Sticker_Picker**: A UI panel shown when the sticker tool is selected, displaying available Sticker_Categories and their thumbnail contents for selection
- **Sticker_Catalog**: The server-side index of all available sticker categories and their image filenames, built by scanning and extracting the `stickers/` zip files

## Requirements

### Requirement 1: Whiteboard Mode Activation

**User Story:** As a viewer, I want to toggle a whiteboard drawing overlay on the video player so that I can annotate and draw over the video content.

#### Acceptance Criteria

1. THE Activity_Frontend SHALL display a whiteboard toggle button in the existing player controls overlay
2. WHEN a viewer clicks the whiteboard toggle button, THE Activity_Frontend SHALL activate whiteboard mode by showing the Whiteboard_HUD toolbar and enabling drawing input on the Whiteboard_Overlay
3. WHILE whiteboard mode is active, THE Whiteboard_Overlay SHALL be positioned as a transparent layer covering the entire video viewport, above the video element but below the player controls overlay
4. WHEN a viewer clicks the whiteboard toggle button while whiteboard mode is active, THE Activity_Frontend SHALL deactivate whiteboard mode by hiding the Whiteboard_HUD and disabling drawing input
5. WHILE whiteboard mode is inactive, THE Whiteboard_Overlay SHALL still render all existing strokes (read-only display) but SHALL NOT capture pointer input for drawing
6. THE whiteboard toggle button SHALL visually indicate whether whiteboard mode is currently active or inactive
7. WHEN the Activity iframe first loads, THE Activity_Frontend SHALL initialize whiteboard mode as inactive with the Whiteboard_HUD hidden and drawing input disabled

### Requirement 2: Freehand Drawing Tool

**User Story:** As a viewer, I want to draw freehand lines on the whiteboard so that I can sketch annotations and highlight areas of the video.

#### Acceptance Criteria

1. THE Whiteboard_HUD SHALL include a freehand pen tool as the default selected Drawing_Tool
2. WHILE the pen tool is selected, WHEN the viewer presses a pointer down on the Whiteboard_Overlay, THE Activity_Frontend SHALL begin capturing a freehand Stroke by recording pointer coordinates normalized to the 0.0–1.0 range (relative to overlay width and height) on each pointer-move event until the pointer is released or leaves the Whiteboard_Overlay bounds
3. WHEN the viewer releases the pointer or the pointer leaves the Whiteboard_Overlay bounds, THE Activity_Frontend SHALL finalize the freehand Stroke and broadcast it to all connected viewers via WebSocket as a message containing the ordered array of normalized coordinates, the stroke color, and the stroke width
4. THE freehand Stroke SHALL render as an anti-aliased path using quadratic Bézier curve interpolation between consecutive captured points so that corners are rounded rather than jagged
5. THE freehand Stroke SHALL use the currently selected color and a fixed stroke width of 3 CSS pixels
6. WHEN a viewer receives a freehand Stroke message via WebSocket, THE Activity_Frontend SHALL render that Stroke on the receiver's Whiteboard_Overlay using the same path interpolation, color, and width specified in the message

### Requirement 3: Straight Line Drawing Tool

**User Story:** As a viewer, I want to draw straight lines on the whiteboard so that I can create precise annotations and pointers.

#### Acceptance Criteria

1. THE Whiteboard_HUD SHALL include a straight line Drawing_Tool selectable from the toolbar
2. WHILE the line tool is selected, WHEN the viewer presses a pointer down on the Whiteboard_Overlay, THE Activity_Frontend SHALL record the start point of the line in normalized coordinates (0.0–1.0)
3. WHILE the viewer drags with the pointer held down, THE Activity_Frontend SHALL render a single preview line from the start point to the current pointer position, replacing the previous preview on each pointer move
4. WHEN the viewer releases the pointer, THE Activity_Frontend SHALL finalize the line Stroke from the start point to the release point and broadcast it to all connected viewers via WebSocket
5. THE line Stroke SHALL use the currently selected color and a fixed stroke width of 3 CSS pixels
6. IF the pointer leaves the Whiteboard_Overlay bounds during a drag, THEN THE Activity_Frontend SHALL finalize the line Stroke using the last pointer position within the overlay bounds as the endpoint
7. IF the viewer releases the pointer at the same position as the start point (zero-length line), THEN THE Activity_Frontend SHALL discard the interaction without creating a Stroke

### Requirement 4: Shape Drawing Tool

**User Story:** As a viewer, I want to draw shapes on the whiteboard so that I can highlight and frame areas of interest in the video.

#### Acceptance Criteria

1. THE Whiteboard_HUD SHALL include a shape Drawing_Tool with a sub-menu offering rectangle, ellipse, and arrow shape types, with the currently selected shape type visually highlighted
2. WHILE a shape type is selected, WHEN the viewer presses a pointer down on the Whiteboard_Overlay, THE Activity_Frontend SHALL record the start point of the shape bounding box in normalized coordinates (0.0–1.0)
3. WHILE the viewer drags with the pointer held down, THE Activity_Frontend SHALL render a live preview of the selected shape type within the bounding box defined by the start point and current pointer position
4. WHEN the viewer releases the pointer and the bounding box exceeds 5 CSS pixels in both width and height, THE Activity_Frontend SHALL finalize the shape Stroke and broadcast it to all connected viewers via WebSocket
5. IF the viewer releases the pointer and the bounding box is 5 CSS pixels or smaller in either dimension, THEN THE Activity_Frontend SHALL discard the interaction without creating a Stroke
6. THE shape Stroke SHALL render as an outlined (not filled) shape using the currently selected color and a stroke width of 3 CSS pixels
7. THE arrow shape SHALL render as a line segment from start point to end point with an arrowhead at the endpoint sized proportionally to the stroke width
8. IF the pointer leaves the Whiteboard_Overlay boundary during a shape drag, THEN THE Activity_Frontend SHALL clamp the shape bounding box to the overlay edges and finalize the Stroke at the clamped position

### Requirement 5: Text Tool

**User Story:** As a viewer, I want to place text annotations on the whiteboard so that I can label and describe areas of the video.

#### Acceptance Criteria

1. THE Whiteboard_HUD SHALL include a text Drawing_Tool
2. WHEN the text tool is selected and the viewer clicks on the Whiteboard_Overlay, THE Activity_Frontend SHALL display a text input field at the clicked position (using normalized 0.0–1.0 coordinates) with a maximum length of 200 characters
3. WHEN the viewer submits text (by pressing Enter or clicking outside the input), THE Activity_Frontend SHALL finalize a text Stroke at the clicked position with the entered content, the currently selected background toggle state, and broadcast it to all connected viewers via WebSocket
4. IF the viewer submits an empty or whitespace-only text input, THEN THE Activity_Frontend SHALL cancel the text placement without creating a Stroke
5. IF the viewer presses Escape while the text input field is active, THEN THE Activity_Frontend SHALL cancel the text placement without creating a Stroke
6. THE text Stroke SHALL render using the currently selected color with a font size of 16 CSS pixels
7. WHEN the text background toggle is enabled, THE text Stroke SHALL render with a black background at 50% opacity and 4px padding behind the text; WHEN the toggle is disabled, THE text Stroke SHALL render with no background

### Requirement 6: Color Selection

**User Story:** As a viewer, I want to choose drawing colors so that I can differentiate my annotations and create visual emphasis.

#### Acceptance Criteria

1. THE Whiteboard_HUD SHALL include a Color_Picker component displaying a palette of 8 preset colors (white, red, orange, yellow, green, cyan, blue, purple)
2. THE Color_Picker SHALL include a custom color input allowing the viewer to select an arbitrary color via the browser's native color picker dialog, which returns a hex color string
3. WHEN a viewer selects a color (preset or custom), THE Activity_Frontend SHALL apply that color as the stroke color to all subsequent Strokes drawn by that viewer until a new color is selected
4. THE Color_Picker SHALL visually indicate the currently selected color by rendering a visible border or highlight on the active preset swatch, or by displaying the custom color value when a non-preset color is active
5. THE selected color SHALL persist in localStorage as a hex string so it survives page reloads within the same session; IF no stored color exists or the stored value is not a valid 7-character hex color string (e.g. `#RRGGBB`), THEN THE Activity_Frontend SHALL default to white (`#FFFFFF`)

### Requirement 7: Eraser Tool

**User Story:** As a viewer, I want to erase specific strokes from the whiteboard so that I can remove mistakes or outdated annotations.

#### Acceptance Criteria

1. THE Whiteboard_HUD SHALL include an eraser Drawing_Tool
2. WHEN the eraser tool is selected and the viewer clicks on a Stroke on the Whiteboard_Overlay, THE Activity_Frontend SHALL remove that entire Stroke and broadcast the removal to all connected viewers via a `stroke_remove` WebSocket message containing the stroke ID
3. THE eraser SHALL use hit-testing against rendered Stroke paths with a tolerance of 5 CSS pixels from the path centerline to determine which Stroke the viewer clicked on
4. IF the viewer clicks on a point where multiple Strokes overlap within hit-testing tolerance, THEN THE Activity_Frontend SHALL remove only the topmost (most recently added) Stroke
5. IF the viewer clicks on an area with no Strokes within hit-testing tolerance, THEN THE Activity_Frontend SHALL take no action
6. WHILE the eraser tool is selected, THE Activity_Frontend SHALL display a distinct eraser cursor (visually differentiated from the default pointer and drawing crosshair)

### Requirement 8: Undo Operation

**User Story:** As a viewer, I want to undo my last drawing action so that I can quickly correct mistakes without using the eraser.

#### Acceptance Criteria

1. THE Whiteboard_HUD SHALL include an undo button
2. WHEN a viewer clicks the undo button, THE Activity_Frontend SHALL remove the most recent Stroke drawn by THAT viewer (not other viewers' strokes) and broadcast the removal to all connected viewers via a `stroke_remove` WebSocket message
3. IF the viewer has no remaining Strokes to undo, THEN THE undo button SHALL appear visually disabled (reduced opacity, non-interactive) and take no action when clicked
4. THE undo operation SHALL support undoing multiple strokes sequentially (one per click) back to the beginning of the viewer's drawing history for the current session
5. WHEN a viewer disconnects and reconnects to the same Activity session, THE Activity_Frontend SHALL restore that viewer's undo history so previously drawn strokes remain undoable
6. WHEN a viewer presses Ctrl+Z (or Cmd+Z on macOS), THE Activity_Frontend SHALL perform the same undo action as clicking the undo button

### Requirement 9: Reset (Clear All) Operation

**User Story:** As a viewer, I want to clear the entire whiteboard so that we can start fresh when annotations become cluttered.

#### Acceptance Criteria

1. THE Whiteboard_HUD SHALL include a reset button
2. WHEN a viewer clicks the reset button, THE Activity_Frontend SHALL display a confirmation prompt before proceeding
3. WHEN the viewer confirms the reset, THE Activity_Frontend SHALL remove ALL Strokes from all viewers and broadcast a `whiteboard_reset` WebSocket message to all connected viewers
4. WHEN the reset is broadcast, ALL connected viewers' Whiteboard_Overlays SHALL be cleared immediately
5. WHEN the WebSocketHub receives a `whiteboard_reset` message, THE Stroke_Registry on the server SHALL be emptied for that guild
6. WHEN a reset is performed, THE undo history for ALL viewers SHALL be cleared (no strokes remain undoable after reset)
7. IF the viewer dismisses or cancels the confirmation prompt, THEN THE Activity_Frontend SHALL take no action and leave the whiteboard unchanged

### Requirement 10: Real-Time Stroke Synchronization via WebSocket

**User Story:** As a viewer, I want to see other participants' drawings appear in real-time so that the whiteboard is truly collaborative.

#### Acceptance Criteria

1. WHEN a viewer finishes drawing a Stroke (pointer-up or touch-end event), THE Activity_Frontend SHALL send a `stroke_add` WebSocket message containing the full stroke data (type, points array, color, stroke width, unique ID, author)
2. WHEN the WebSocketHub receives a `stroke_add` message, THE WebSocketHub SHALL store the Stroke in the Stroke_Registry for the guild and broadcast the message to all other connected viewers
3. WHEN a viewer receives a `stroke_add` message, THE Activity_Frontend SHALL render the Stroke on the Whiteboard_Overlay within 100 milliseconds of message receipt
4. WHEN a viewer erases a Stroke, THE Activity_Frontend SHALL send a `stroke_remove` WebSocket message containing the stroke ID
5. WHEN a viewer undoes their most recent Stroke, THE Activity_Frontend SHALL send a `stroke_remove` WebSocket message containing the stroke ID of their last authored stroke
6. WHEN the WebSocketHub receives a `stroke_remove` message, THE WebSocketHub SHALL remove the Stroke from the Stroke_Registry and broadcast the removal to all other connected viewers
7. WHEN a viewer receives a `stroke_remove` message, THE Activity_Frontend SHALL remove the corresponding Stroke from the Whiteboard_Overlay
8. WHEN a reset is triggered, THE Activity_Frontend SHALL send a `whiteboard_reset` WebSocket message
9. WHEN the WebSocketHub receives a `whiteboard_reset` message, THE WebSocketHub SHALL clear the Stroke_Registry for the guild and broadcast the reset to all other connected viewers
10. WHEN a new viewer connects to the WebSocket mid-session, THE WebSocketHub SHALL send all existing Strokes from the Stroke_Registry so the late joiner sees the current whiteboard state
11. IF the Stroke_Registry for a guild reaches 500 stored strokes, THEN THE WebSocketHub SHALL reject new `stroke_add` messages and notify the sender that the whiteboard is full
12. IF the WebSocketHub receives a `stroke_add` or `stroke_remove` message with missing or invalid fields (missing stroke ID, empty points array, or unrecognized type), THEN THE WebSocketHub SHALL discard the message and send an error notification to the sender without broadcasting

### Requirement 11: Late-Joiner Stroke Synchronization

**User Story:** As a viewer joining a session in progress, I want to see all existing whiteboard drawings so that I have the same view as other participants.

#### Acceptance Criteria

1. WHEN a new viewer connects via WebSocket, THE WebSocketHub SHALL include the current Stroke_Registry contents as a `strokes` array field in the initial `state` message sent to the late joiner, ordered by the sequence in which strokes were originally added
2. IF the Stroke_Registry is empty when a new viewer connects, THEN THE WebSocketHub SHALL include an empty `strokes` array in the initial state message
3. WHEN the Activity_Frontend receives stroke data in the initial state message, THE Activity_Frontend SHALL render all existing Strokes on the Whiteboard_Overlay in the received array order before enabling drawing input or processing subsequent WebSocket messages
4. THE late-joiner stroke payload SHALL contain the same data fields as individual `stroke_add` messages (type, points, color, stroke width, ID, author) for each entry in the `strokes` array
5. THE Stroke_Registry SHALL hold a maximum of 500 strokes per guild session; WHEN a `stroke_add` would exceed this limit, THE WebSocketHub SHALL reject the stroke and respond with an error message indicating the stroke limit has been reached

### Requirement 12: Session-Scoped Whiteboard State

**User Story:** As a system operator, I want the whiteboard state cleared automatically when a video session ends so that state does not leak between sessions.

#### Acceptance Criteria

1. WHEN a Session ends (stop command, queue empty, or grace period expiry), THE WebSocketHub SHALL remove all entries from the Stroke_Registry for that guild and broadcast a `whiteboard_clear` event to any remaining connected clients
2. WHEN a new video begins playing (skip or auto-advance), THE Stroke_Registry SHALL be preserved (whiteboard state carries across videos within the same session)
3. THE Stroke_Registry SHALL be stored in-memory only, requiring no persistent storage beyond the session lifetime
4. WHEN a new Session begins (state transitions from IDLE to RESOLVING), THE WebSocketHub SHALL initialize an empty Stroke_Registry for that guild

### Requirement 13: Coordinate Normalization

**User Story:** As a viewer on a different screen size, I want whiteboard drawings to appear at the correct positions regardless of my viewport dimensions.

#### Acceptance Criteria

1. THE Activity_Frontend SHALL normalize all stroke x and y coordinates to a 0.0–1.0 range by dividing each coordinate by the corresponding Whiteboard_Overlay dimension (x by width, y by height) before sending via WebSocket, using at least 4 decimal places of precision
2. WHEN rendering received Strokes, THE Activity_Frontend SHALL scale normalized coordinates back to absolute pixel positions by multiplying x by the current Whiteboard_Overlay width and y by the current Whiteboard_Overlay height
3. WHEN the browser viewport is resized, THE Activity_Frontend SHALL re-render all Strokes by recalculating pixel positions from the stored normalized coordinates, preserving the original point count and relative positions of every stroke
4. IF received stroke coordinates contain values outside the 0.0–1.0 range, THEN THE Activity_Frontend SHALL clamp them to the 0.0–1.0 bounds before rendering
5. THE Activity_Frontend SHALL normalize stroke width relative to the Whiteboard_Overlay width so that line thickness scales proportionally across different viewport sizes

### Requirement 14: Drawing Interaction with Video Controls

**User Story:** As a viewer, I want drawing mode to coexist with video playback controls so that I can still pause, seek, and adjust volume while the whiteboard is active.

#### Acceptance Criteria

1. WHILE whiteboard mode is active, THE player controls overlay SHALL appear when the viewer hovers over or taps the controls area at the bottom of the player, following the same auto-hide timeout as when whiteboard mode is inactive
2. WHILE the pointer or touch point is within the player controls overlay region, THE Whiteboard_Overlay SHALL NOT capture drawing input, allowing all pointer events to pass through to the controls beneath
3. WHILE whiteboard mode is active, THE video player SHALL continue audio and video playback without interruption (activating or deactivating the whiteboard SHALL NOT pause, seek, or otherwise alter playback state)
4. THE whiteboard toggle button SHALL remain visible and respond to click or tap to enable or disable whiteboard mode regardless of whether whiteboard mode is currently active or inactive
5. WHEN a viewer taps within the controls overlay region on a touch device while whiteboard mode is active, THE Whiteboard_Overlay SHALL treat the tap as a controls interaction rather than a drawing stroke

### Requirement 15: Sticker Tool

**User Story:** As a viewer, I want to place pre-made sticker images on the whiteboard so that I can add expressive visual elements to the video overlay without drawing freehand.

#### Acceptance Criteria

1. THE Whiteboard_HUD SHALL include a sticker Drawing_Tool selectable from the toolbar
2. WHEN the sticker tool is selected, THE Activity_Frontend SHALL display a Sticker_Picker UI showing all available Sticker_Categories as navigable groups, with category names extracted from the zip filenames in the `stickers/` directory (e.g. "Stickers - Christmas 2022", "Stickers - Ghosts", "Stickers - Playlist", "Stickers - Self-Love colour-changing", "Stickers - Social Media Summer", "Stickers - Summer Popsicles", "Stickers Pastel Study")
3. WHEN the Sticker_Picker displays a Sticker_Category, THE Activity_Frontend SHALL render thumbnail previews (maximum 64×64 CSS pixels, preserving aspect ratio) of all PNG, GIF, and WebP image files contained in that category's zip file
4. WHEN a viewer selects a sticker from the Sticker_Picker and clicks on the Whiteboard_Overlay, THE Activity_Frontend SHALL begin a placement interaction where the click sets the sticker position and the viewer drags to define the bounding box size (same drag-to-expand mechanism as the shape Drawing_Tool)
5. WHEN the viewer releases the pointer and the bounding box exceeds 5 CSS pixels in both width and height, THE Activity_Frontend SHALL finalize the sticker Stroke at the defined position and size, capping the bounding box at 50% of the overlay width and 50% of the overlay height, and broadcast it to all connected viewers via WebSocket as a `stroke_add` message with type "sticker"
6. IF the viewer releases the pointer and the bounding box is 5 CSS pixels or smaller in either dimension, THEN THE Activity_Frontend SHALL discard the placement without creating a Stroke
7. THE sticker Stroke SHALL render the selected sticker image on the Whiteboard_Overlay at the specified normalized position and size, scaling the image to fit within the bounding box while preserving its original aspect ratio (letterboxed, not stretched)
8. WHEN a viewer receives a sticker Stroke via WebSocket, THE Activity_Frontend SHALL render that sticker image on the Whiteboard_Overlay at the position and size specified in the message
9. THE sticker Stroke SHALL be erasable and undoable using the same mechanisms as all other Stroke types (eraser hit-testing against the sticker bounding box, undo removes the most recent sticker by that author)
10. THE Activity_Backend SHALL provide an HTTP endpoint to serve individual sticker image files extracted from the zip archives, extracting all zip contents into memory or a cache directory at server startup
11. THE Activity_Backend SHALL provide an HTTP endpoint that returns the Sticker_Catalog (a JSON structure listing all Sticker_Categories with their names and contained image filenames) so the Activity_Frontend can populate the Sticker_Picker
12. IF a zip file in the `stickers/` directory is corrupt or contains no supported image files (PNG, GIF, WebP), THEN THE Activity_Backend SHALL omit that category from the Sticker_Catalog and log a warning, without affecting other categories
