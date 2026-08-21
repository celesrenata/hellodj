/**
 * Reset button logic — clears the entire whiteboard after user confirmation.
 *
 * On click: prompts the user with a confirm dialog.
 * On confirm: clears all strokes locally (via overlay.clearAll()) and
 *   sends whiteboard_reset via WebSocket (via sendReset callback).
 * On cancel: no action.
 *
 * Requirements: 9.1, 9.2, 9.3, 9.4, 9.6, 9.7
 */

/**
 * Initialize the reset button behavior.
 *
 * @param {HTMLButtonElement} button - The reset button element (#btn-reset)
 * @param {import('./whiteboard.js').WhiteboardOverlay} overlay - The WhiteboardOverlay instance
 * @param {() => void} sendReset - Callback that sends whiteboard_reset via WebSocket
 */
export function initReset(button, overlay, sendReset) {
  button.addEventListener('click', () => {
    const confirmed = confirm('Clear entire whiteboard? This removes all drawings.');
    if (!confirmed) return;

    // Clear all strokes locally (also clears undo history for this viewer)
    overlay.clearAll();

    // Broadcast reset to all other viewers via WebSocket
    sendReset();
  });
}
