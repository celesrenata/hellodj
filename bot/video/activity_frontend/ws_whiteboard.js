/**
 * WebSocket whiteboard sync — integrates WhiteboardOverlay with the
 * existing WebSocket connection in the Activity frontend.
 *
 * Exports `initWhiteboardSync(wsSend, overlay)` which:
 * 1. Returns a `handleMessage(data)` function for dispatching incoming
 *    whiteboard-specific WebSocket messages (stroke_add, stroke_remove,
 *    whiteboard_reset, whiteboard_clear, state strokes, error).
 * 2. Provides `sendStrokeAdd(stroke)`, `sendStrokeRemove(strokeId)`,
 *    `sendWhiteboardReset()` functions to broadcast local actions.
 * 3. On initial `state` message with `strokes` array: renders all strokes
 *    and rebuilds undo history before enabling drawing.
 *
 * @module ws_whiteboard
 * @requirements 10.1, 10.3, 10.4, 10.5, 10.7, 10.8, 10.10, 11.3
 */

import { restoreUndoHistory } from './undo_restore.js';

/**
 * Initialize whiteboard WebSocket synchronization.
 *
 * @param {(msg: object) => void} wsSend - Function that JSON-serializes and sends
 *   a message over the WebSocket (handles readyState checks internally).
 * @param {import('./whiteboard.js').WhiteboardOverlay} overlay - The WhiteboardOverlay instance.
 * @returns {{
 *   handleMessage: (data: object) => void,
 *   sendStrokeAdd: (stroke: object) => void,
 *   sendStrokeRemove: (strokeId: string) => void,
 *   sendWhiteboardReset: () => void
 * }}
 */
export function initWhiteboardSync(wsSend, overlay) {
  /**
   * Send a stroke_add message to the server.
   * Called on tool finalization (pointerup) after the stroke is added locally.
   *
   * @param {object} stroke - Finalized stroke data (id, type, points, color, width, etc.)
   */
  function sendStrokeAdd(stroke) {
    wsSend({
      type: 'stroke_add',
      id: stroke.id,
      stroke_type: stroke.type,
      points: stroke.points,
      color: stroke.color,
      width: stroke.width,
      opacity: stroke.opacity,
      author: overlay.localAuthorId,
      // Optional fields for text strokes
      ...(stroke.text != null && { text: stroke.text }),
      ...(stroke.text_bg != null && { text_bg: stroke.text_bg }),
      // Optional fields for sticker strokes
      ...(stroke.sticker_category != null && { sticker_category: stroke.sticker_category }),
      ...(stroke.sticker_filename != null && { sticker_filename: stroke.sticker_filename }),
      // Optional animated field for rotating shapes
      ...(stroke.animated && { animated: true }),
    });
  }

  /**
   * Send a stroke_remove message to the server.
   * Called on eraser hit or undo operation.
   *
   * @param {string} strokeId - ID of the stroke to remove.
   */
  function sendStrokeRemove(strokeId) {
    wsSend({
      type: 'stroke_remove',
      id: strokeId,
    });
  }

  /**
   * Send a whiteboard_reset message to the server.
   * Called after the user confirms the reset action.
   */
  function sendWhiteboardReset() {
    wsSend({
      type: 'whiteboard_reset',
    });
  }

  /**
   * Handle an incoming WebSocket message related to the whiteboard.
   * Called from the existing app.js message handler for whiteboard message types.
   *
   * Handles:
   * - `stroke_add`: parse and render stroke on canvas
   * - `stroke_remove`: remove stroke from map, redraw
   * - `whiteboard_reset`: clear all strokes, redraw
   * - `whiteboard_clear` (session end): clear all strokes, deactivate mode
   * - `state` (with strokes array): render initial strokes, restore undo history
   * - `error`: log the error message
   *
   * @param {object} data - Parsed WebSocket message data with a `type` field.
   * @returns {boolean} True if the message was handled, false otherwise.
   */
  function handleMessage(data) {
    switch (data.type) {
      case 'stroke_add':
        _handleStrokeAdd(data);
        return true;

      case 'stroke_remove':
        _handleStrokeRemove(data);
        return true;

      case 'whiteboard_reset':
        _handleWhiteboardReset();
        return true;

      case 'whiteboard_clear':
        _handleWhiteboardClear();
        return true;

      case 'state':
        _handleState(data);
        return false;  // Allow main handler to also process playback state

      case 'error':
        _handleError(data);
        return true;

      default:
        return false;
    }
  }

  /**
   * Handle incoming stroke_add: add the stroke to the overlay and redraw.
   * The stroke arrives from another viewer (the server excluded the sender).
   * @param {object} data
   */
  function _handleStrokeAdd(data) {
    const stroke = {
      id: data.id,
      type: data.stroke_type,
      points: data.points,
      color: data.color,
      width: data.width,
      opacity: data.opacity,
      author: data.author,
    };

    // Include optional text fields
    if (data.text != null) stroke.text = data.text;
    if (data.text_bg != null) stroke.text_bg = data.text_bg;

    // Include optional sticker fields
    if (data.sticker_category != null) stroke.sticker_category = data.sticker_category;
    if (data.sticker_filename != null) stroke.sticker_filename = data.sticker_filename;

    // Include optional animated field
    if (data.animated) stroke.animated = true;

    overlay.addStroke(stroke);
  }

  /**
   * Handle incoming stroke_remove: remove the stroke from the overlay.
   * @param {object} data
   */
  function _handleStrokeRemove(data) {
    if (data.id) {
      overlay.removeStroke(data.id);
    }
  }

  /**
   * Handle incoming whiteboard_reset: clear all strokes and redraw.
   * This occurs when another viewer resets the whiteboard.
   */
  function _handleWhiteboardReset() {
    overlay.clearAll();
  }

  /**
   * Handle incoming whiteboard_clear (from session end):
   * clear all strokes and deactivate whiteboard mode.
   */
  function _handleWhiteboardClear() {
    overlay.clearAll();
    overlay.deactivate();
  }

  /**
   * Handle the initial `state` message containing a `strokes` array.
   * Renders all existing strokes and restores the undo history before
   * enabling drawing input.
   *
   * Called both on initial connection and on WebSocket reconnect.
   * @param {object} data
   */
  function _handleState(data) {
    const strokes = data.strokes;
    if (!Array.isArray(strokes)) return;

    // Clear existing local state before applying server state
    overlay.strokes.clear();
    overlay.undoStack.length = 0;

    // Render all strokes from the server in insertion order
    for (const s of strokes) {
      const stroke = {
        id: s.id,
        type: s.type,
        points: s.points,
        color: s.color,
        width: s.width,
        opacity: s.opacity,
        author: s.author,
      };

      // Include optional text fields
      if (s.text != null) stroke.text = s.text;
      if (s.text_bg != null) stroke.text_bg = s.text_bg;

      // Include optional sticker fields
      if (s.sticker_category != null) stroke.sticker_category = s.sticker_category;
      if (s.sticker_filename != null) stroke.sticker_filename = s.sticker_filename;

      // Include optional animated field
      if (s.animated) stroke.animated = true;

      overlay.strokes.set(stroke.id, stroke);
    }

    // Rebuild undo history for the local author
    restoreUndoHistory(overlay, strokes);

    // Update animation state and redraw
    overlay.renderer.updateAnimationState(overlay.strokes);
    overlay.redraw();
  }

  /**
   * Handle error messages from the server (display in console).
   * Could be extended to show a toast notification in the future.
   * @param {object} data
   */
  function _handleError(data) {
    if (data.message) {
      console.warn('[Whiteboard] Server error:', data.message);
    }
  }

  return {
    handleMessage,
    sendStrokeAdd,
    sendStrokeRemove,
    sendWhiteboardReset,
  };
}
