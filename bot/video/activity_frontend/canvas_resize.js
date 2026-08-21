/**
 * Canvas resize handling — automatic canvas resizing via ResizeObserver.
 *
 * Watches the canvas's parent element for size changes and delegates
 * all resize logic (dimensions, renderer, redraw) to WhiteboardOverlay.resize().
 *
 * @module canvas_resize
 * @see WhiteboardOverlay#resize
 * @requirement 13.3
 */

/**
 * Initialize automatic canvas resize handling.
 *
 * Creates a ResizeObserver on the canvas's parent element. On each observed
 * resize, calls overlay.resize() which updates canvas dimensions, recalculates
 * stroke pixel positions from normalized coords, and redraws.
 *
 * Performs an initial resize call immediately to set correct dimensions.
 *
 * @param {HTMLCanvasElement} canvas - The whiteboard canvas element
 * @param {import('./whiteboard.js').WhiteboardOverlay} overlay - The overlay instance with a resize() method
 * @returns {{ disconnect: () => void }} Cleanup handle to stop observing
 */
export function initCanvasResize(canvas, overlay) {
  const parent = canvas.parentElement;

  const observer = new ResizeObserver((entries) => {
    for (const entry of entries) {
      const { width, height } = entry.contentRect;
      // Skip redraw if dimensions are 0 (element hidden or collapsed)
      if (width === 0 || height === 0) return;
      overlay.resize();
    }
  });

  observer.observe(parent);

  // Perform initial resize to set up correct dimensions
  overlay.resize();

  return {
    disconnect() {
      observer.disconnect();
    },
  };
}
