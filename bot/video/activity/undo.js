/**
 * Undo module — manages undo button logic and Ctrl+Z / Cmd+Z keyboard shortcut.
 *
 * Responsibilities:
 * - Pop the most recent stroke from this viewer's undo stack
 * - Remove the stroke locally via WhiteboardOverlay.removeStroke()
 * - Broadcast removal via sendStrokeRemove callback
 * - Visually disable the button when nothing to undo
 * - Listen for Ctrl+Z / Cmd+Z when whiteboard mode is active
 */

/**
 * Initialize undo functionality.
 *
 * @param {HTMLButtonElement} button - The undo button element (#btn-undo)
 * @param {import('./whiteboard.js').WhiteboardOverlay} overlay - The whiteboard overlay instance
 * @param {(strokeId: string) => void} sendRemove - Callback to send stroke_remove WebSocket message
 */
export function initUndo(button, overlay, sendRemove) {
  /**
   * Update button visual state based on undo stack contents.
   * Adds 'disabled' class (opacity 0.4, pointer-events: none) when empty.
   */
  function updateButtonState() {
    if (overlay.undoStack.length === 0) {
      button.classList.add('disabled');
    } else {
      button.classList.remove('disabled');
    }
  }

  /**
   * Perform a single undo operation:
   * - Pop last stroke ID from this viewer's undo stack
   * - Remove it from the overlay (stroke map + redraw)
   * - Send stroke_remove over WebSocket
   * - Update button state
   */
  function performUndo() {
    if (overlay.undoStack.length === 0) return;

    const strokeId = overlay.undoStack[overlay.undoStack.length - 1];
    overlay.removeStroke(strokeId);
    sendRemove(strokeId);
    updateButtonState();
  }

  // Wire button click
  button.addEventListener('click', () => {
    performUndo();
  });

  // Wire Ctrl+Z / Cmd+Z keyboard shortcut (active only when whiteboard mode is active)
  document.addEventListener('keydown', (e) => {
    if (overlay.mode !== 'active') return;

    const isUndo = (e.ctrlKey || e.metaKey) && e.key === 'z' && !e.shiftKey;
    if (isUndo) {
      e.preventDefault();
      performUndo();
    }
  });

  // Set initial button state
  updateButtonState();

  // Return updateButtonState so external code can trigger a refresh
  // (e.g. after addStroke or after receiving state from server)
  return { updateButtonState };
}
