/**
 * ControlsPassthrough — ensures video controls remain accessible while whiteboard mode is active.
 *
 * When whiteboard mode is active, the canvas (z-index 20) has pointer-events: auto,
 * which would normally capture all pointer input including the bottom controls region.
 * This module detects when the pointer is within the controls overlay area and temporarily
 * disables canvas pointer-events so events pass through to the controls beneath.
 *
 * Behavior:
 * - Pointer in controls region → canvas pointer-events: none (controls receive events)
 * - Pointer in drawing region → canvas pointer-events: auto (canvas receives events)
 * - Touch taps in controls region → treated as controls interaction, not drawing
 * - Controls appear on hover/tap with the same auto-hide timeout as normal mode
 *
 * @module controls_passthrough
 */

/**
 * @typedef {object} ControlsPassthroughOptions
 * @property {HTMLCanvasElement} canvas - The whiteboard canvas element
 * @property {HTMLElement} controlsOverlay - The .controls-overlay element
 * @property {HTMLElement} bottomControls - The .bottom-controls element
 * @property {function} showControls - Callback to trigger controls visibility (show + reset auto-hide timer)
 */

export class ControlsPassthrough {
  /**
   * @param {ControlsPassthroughOptions} options
   */
  constructor({ canvas, controlsOverlay, bottomControls, showControls }) {
    /** @type {HTMLCanvasElement} */
    this.canvas = canvas;
    /** @type {HTMLElement} */
    this.controlsOverlay = controlsOverlay;
    /** @type {HTMLElement} */
    this.bottomControls = bottomControls;
    /** @type {function} */
    this.showControls = showControls;
    /** @type {boolean} */
    this._whiteboardActive = false;
    /** @type {boolean} */
    this._inControlsRegion = false;

    // Bind handlers for cleanup
    this._onPointerMove = this._handlePointerMove.bind(this);
    this._onCanvasPointerDown = this._handleCanvasPointerDown.bind(this);
    this._onCanvasTouchStart = this._handleCanvasTouchStart.bind(this);

    // Listen on document for pointermove to detect position regardless of which element has pointer-events
    document.addEventListener('pointermove', this._onPointerMove);

    // Listen on canvas for pointerdown/touchstart to intercept interactions in controls region
    this.canvas.addEventListener('pointerdown', this._onCanvasPointerDown);
    this.canvas.addEventListener('touchstart', this._onCanvasTouchStart, { passive: false });
  }

  /**
   * Update whiteboard active state. When inactive, restore normal pointer-events.
   * @param {boolean} active
   */
  setWhiteboardActive(active) {
    this._whiteboardActive = active;
    if (!active) {
      this._inControlsRegion = false;
      // When whiteboard is inactive, canvas pointer-events is managed by whiteboard.js (set to 'none')
    }
  }

  /**
   * Check if a clientY coordinate falls within the bottom controls region.
   * Uses the bottom-controls element's bounding rect for accurate detection.
   * @param {number} clientY - The Y coordinate in viewport pixels
   * @returns {boolean}
   */
  _isInControlsRegion(clientY) {
    const rect = this.bottomControls.getBoundingClientRect();
    // Include some padding above the controls for easier hover access (8px grace zone)
    return clientY >= rect.top - 8;
  }

  /**
   * Handle pointermove on document.
   * Toggles canvas pointer-events based on whether the pointer is in the controls region.
   * @param {PointerEvent} e
   */
  _handlePointerMove(e) {
    if (!this._whiteboardActive) return;

    const inRegion = this._isInControlsRegion(e.clientY);

    if (inRegion && !this._inControlsRegion) {
      // Entering controls region — disable canvas pointer-events
      this._inControlsRegion = true;
      this.canvas.style.pointerEvents = 'none';
      this.showControls();
    } else if (!inRegion && this._inControlsRegion) {
      // Leaving controls region — restore canvas pointer-events
      this._inControlsRegion = false;
      this.canvas.style.pointerEvents = 'auto';
    }
  }

  /**
   * Handle pointerdown on canvas.
   * If the pointer is in the controls region, prevent drawing and show controls.
   * This catches cases where pointermove hasn't updated yet (e.g., fast movements).
   * @param {PointerEvent} e
   */
  _handleCanvasPointerDown(e) {
    if (!this._whiteboardActive) return;

    if (this._isInControlsRegion(e.clientY)) {
      // Prevent canvas from starting a drawing stroke
      e.stopPropagation();
      e.preventDefault();
      // Disable canvas to let subsequent events reach controls
      this.canvas.style.pointerEvents = 'none';
      this._inControlsRegion = true;
      this.showControls();
    }
  }

  /**
   * Handle touchstart on canvas.
   * On touch devices, taps in the controls region are treated as controls interaction.
   * @param {TouchEvent} e
   */
  _handleCanvasTouchStart(e) {
    if (!this._whiteboardActive) return;
    if (e.touches.length === 0) return;

    const touch = e.touches[0];
    if (this._isInControlsRegion(touch.clientY)) {
      // Prevent canvas from interpreting this as a drawing gesture
      e.stopPropagation();
      e.preventDefault();
      // Disable canvas pointer-events to let controls handle the interaction
      this.canvas.style.pointerEvents = 'none';
      this._inControlsRegion = true;
      this.showControls();

      // Restore canvas pointer-events after a short delay (allow the controls to process the tap)
      // The controls auto-hide timer will keep them visible for the standard timeout
      setTimeout(() => {
        if (this._whiteboardActive && !this._isInControlsRegion(touch.clientY)) {
          this.canvas.style.pointerEvents = 'auto';
          this._inControlsRegion = false;
        }
      }, 300);
    }
  }

  /**
   * Clean up event listeners.
   */
  destroy() {
    document.removeEventListener('pointermove', this._onPointerMove);
    this.canvas.removeEventListener('pointerdown', this._onCanvasPointerDown);
    this.canvas.removeEventListener('touchstart', this._onCanvasTouchStart);
  }
}
