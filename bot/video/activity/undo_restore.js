/**
 * Undo history restore — rebuilds a viewer's undoStack from
 * the strokes array received in the WebSocket `state` message
 * after a reconnect.
 *
 * @module undo_restore
 */

/**
 * Rebuild the viewer's undo stack from the initial state strokes.
 *
 * Clears the current undoStack, then iterates through strokes in
 * insertion order and pushes the ID of each stroke authored by the
 * local viewer. This restores the ability to undo previously drawn
 * strokes after a WebSocket reconnect.
 *
 * @param {import('./whiteboard.js').WhiteboardOverlay} overlay - The whiteboard overlay instance
 * @param {Array<{id: string, author: string}>} strokes - Strokes array from the state message (insertion order)
 */
export function restoreUndoHistory(overlay, strokes) {
  overlay.undoStack.length = 0;

  for (const stroke of strokes) {
    if (stroke.author === overlay.localAuthorId) {
      overlay.undoStack.push(stroke.id);
    }
  }
}
