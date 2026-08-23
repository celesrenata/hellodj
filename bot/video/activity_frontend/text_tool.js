/**
 * TextTool — places text annotations on the whiteboard overlay.
 *
 * Implements the DrawingTool interface:
 *   name, cursor, onPointerDown, onPointerMove, onPointerUp, onCancel, renderPreview
 *
 * Behavior:
 * - On pointerdown: creates a text input element positioned at the tap/click location
 * - Max length 200 characters
 * - On Enter or "Done" button: finalizes a text stroke with content + text_bg toggle state
 * - Rejects empty/whitespace-only input (no stroke created)
 * - On Escape: cancels without creating a stroke
 * - Uses current color, font size 16px
 *
 * Mobile fixes:
 * - Uses clientX/clientY with bounding rect for reliable touch positioning
 * - Adds a ✓ (Done) button for mobile users (no reliable Enter key)
 * - Uses contenteditable div instead of input for better mobile keyboard support
 * - Prevents touch-action interference
 * - Delays focus to next frame for mobile browser compatibility
 */

import { normalize, normalizeWidth } from './coords.js';

export class TextTool {
  /**
   * @param {object} config
   * @param {function} config.getCanvasSize      - Returns { width, height } of the canvas in CSS pixels
   * @param {function} config.getColor           - Returns the current hex color string
   * @param {function} config.getTextBg          - Returns boolean: whether text background is enabled
   * @param {function} config.getContainer       - Returns the HTMLElement that contains the canvas (for positioning the input)
   * @param {function} config.requestRedraw      - Called when preview needs refresh
   * @param {function} config.onStrokeFinalized  - Callback invoked with the finalized stroke object (async delivery)
   */
  constructor(config) {
    this.name = 'text';
    this.cursor = 'text';

    this._getCanvasSize = config.getCanvasSize;
    this._getColor = config.getColor;
    this._getTextBg = config.getTextBg;
    this._getContainer = config.getContainer;
    this._requestRedraw = config.requestRedraw;
    this._onStrokeFinalized = config.onStrokeFinalized;

    /** @type {HTMLElement|null} Active text input wrapper */
    this._wrapperEl = null;
    /** @type {HTMLInputElement|null} Active text input element */
    this._inputEl = null;
    /** @type {[number, number]|null} Normalized position of the text placement */
    this._position = null;
  }

  /**
   * On pointerdown: normalize the click position and show a text input element.
   * If an input is already active, finalize it first.
   * Uses clientX/clientY for reliable mobile touch positioning.
   * @param {PointerEvent} e
   */
  onPointerDown(e) {
    // If there's already an active input, finalize it before opening a new one
    if (this._inputEl) {
      this._finalizeInput();
    }

    const { width, height } = this._getCanvasSize();
    if (width === 0 || height === 0) return;

    // Use clientX/clientY with bounding rect for reliable touch/pointer positioning
    const rect = e.target.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;

    this._position = normalize(px, py, width, height);
    this._showInput(px, py, rect);
  }

  /**
   * No-op for text tool — text placement is click-based.
   * @param {PointerEvent} _e
   */
  onPointerMove(_e) {
    // no-op
  }

  /**
   * No-op for text tool — text placement uses pointerdown for click position.
   * @param {PointerEvent} _e
   * @returns {null}
   */
  onPointerUp(_e) {
    // no-op: text strokes are delivered asynchronously via onStrokeFinalized callback
    return null;
  }

  /**
   * Cancel the text input without creating a stroke.
   * Removes the input element if present.
   */
  onCancel() {
    this._removeInput();
    this._position = null;
  }

  /**
   * No preview rendering for text tool — text appears in the input element.
   * @param {CanvasRenderingContext2D} _ctx
   */
  renderPreview(_ctx) {
    // no-op: text is shown in the input element, not on canvas
  }

  /**
   * Create and display a text input element at the specified pixel position.
   * Positions relative to the video-container for correct overlay placement.
   * Includes a Done button for mobile users.
   * @param {number} pixelX - X position relative to canvas
   * @param {number} pixelY - Y position relative to canvas
   * @param {DOMRect} canvasRect - Bounding rect of the canvas element
   * @private
   */
  _showInput(pixelX, pixelY, canvasRect) {
    const container = this._getContainer();
    if (!container) return;

    // Create a wrapper that holds input + done button
    const wrapper = document.createElement('div');
    wrapper.className = 'whiteboard-text-input-wrapper';
    wrapper.style.position = 'absolute';
    wrapper.style.zIndex = '55';
    wrapper.style.display = 'flex';
    wrapper.style.alignItems = 'center';
    wrapper.style.gap = '4px';

    // Position relative to the container (#app) — account for canvas offset within app
    const containerRect = container.getBoundingClientRect();
    const absoluteX = canvasRect.left - containerRect.left + pixelX;
    const absoluteY = canvasRect.top - containerRect.top + pixelY;

    wrapper.style.left = `${absoluteX}px`;
    wrapper.style.top = `${absoluteY}px`;

    // Prevent the wrapper from going off-screen right
    wrapper.style.maxWidth = `calc(100% - ${absoluteX}px - 8px)`;

    const input = document.createElement('input');
    input.type = 'text';
    input.maxLength = 200;
    input.inputMode = 'text';
    input.autocomplete = 'off';
    input.autocapitalize = 'sentences';
    input.style.fontSize = '16px'; // Prevents iOS zoom on focus
    input.style.color = this._getColor();
    input.style.background = 'rgba(0, 0, 0, 0.7)';
    input.style.border = '1px solid rgba(255, 255, 255, 0.4)';
    input.style.borderRadius = '4px';
    input.style.padding = '6px 8px';
    input.style.outline = 'none';
    input.style.minWidth = '120px';
    input.style.maxWidth = '100%';
    input.style.fontFamily = 'sans-serif';
    input.style.flex = '1';
    // Prevent canvas pointer events from interfering
    input.style.touchAction = 'manipulation';

    // Done button for mobile (and convenient for desktop too)
    const doneBtn = document.createElement('button');
    doneBtn.textContent = '✓';
    doneBtn.style.fontSize = '16px';
    doneBtn.style.background = 'rgba(88, 101, 242, 0.8)';
    doneBtn.style.border = 'none';
    doneBtn.style.borderRadius = '4px';
    doneBtn.style.color = '#fff';
    doneBtn.style.padding = '6px 10px';
    doneBtn.style.cursor = 'pointer';
    doneBtn.style.touchAction = 'manipulation';
    doneBtn.style.flexShrink = '0';

    // Cancel button
    const cancelBtn = document.createElement('button');
    cancelBtn.textContent = '✕';
    cancelBtn.style.fontSize = '14px';
    cancelBtn.style.background = 'rgba(255, 255, 255, 0.15)';
    cancelBtn.style.border = 'none';
    cancelBtn.style.borderRadius = '4px';
    cancelBtn.style.color = 'rgba(255,255,255,0.7)';
    cancelBtn.style.padding = '6px 8px';
    cancelBtn.style.cursor = 'pointer';
    cancelBtn.style.touchAction = 'manipulation';
    cancelBtn.style.flexShrink = '0';

    // Handle Enter → finalize
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        this._finalizeInput();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        this._removeInput();
      }
    });

    // Done button click → finalize
    doneBtn.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      e.stopPropagation();
      this._finalizeInput();
    });

    // Cancel button click → remove
    cancelBtn.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      e.stopPropagation();
      this._removeInput();
    });

    // Prevent pointer events from reaching canvas
    wrapper.addEventListener('pointerdown', (e) => e.stopPropagation());
    wrapper.addEventListener('pointermove', (e) => e.stopPropagation());
    wrapper.addEventListener('pointerup', (e) => e.stopPropagation());
    wrapper.addEventListener('touchstart', (e) => e.stopPropagation());
    wrapper.addEventListener('touchmove', (e) => e.stopPropagation());
    wrapper.addEventListener('touchend', (e) => e.stopPropagation());

    wrapper.appendChild(input);
    wrapper.appendChild(doneBtn);
    wrapper.appendChild(cancelBtn);
    container.appendChild(wrapper);

    this._wrapperEl = wrapper;
    this._inputEl = input;

    // Focus the input after appending to DOM — use setTimeout for mobile compatibility
    // Mobile browsers often need a frame or two before focus works reliably
    setTimeout(() => {
      if (this._inputEl) {
        this._inputEl.focus();
        // Some mobile browsers need a second attempt
        requestAnimationFrame(() => {
          if (this._inputEl && document.activeElement !== this._inputEl) {
            this._inputEl.focus();
          }
        });
      }
    }, 50);
  }

  /**
   * Finalize the text input: create a stroke if content is non-empty/non-whitespace.
   * Delivers the stroke via onStrokeFinalized callback. Removes the input element.
   * @private
   */
  _finalizeInput() {
    if (!this._inputEl || !this._position) {
      this._removeInput();
      return;
    }

    const text = this._inputEl.value;
    this._removeInput();

    // Reject empty or whitespace-only input
    if (!text || !text.trim()) {
      this._position = null;
      return;
    }

    const { width } = this._getCanvasSize();
    const strokeWidth = normalizeWidth(16, width);

    const stroke = {
      id: crypto.randomUUID(),
      type: 'text',
      points: [this._position],
      text: text,
      text_bg: this._getTextBg(),
      color: this._getColor(),
      width: strokeWidth,
    };

    this._position = null;

    // Deliver the stroke asynchronously via callback
    if (this._onStrokeFinalized) {
      this._onStrokeFinalized(stroke);
    }
  }

  /**
   * Remove the input wrapper from the DOM and clear internal references.
   * @private
   */
  _removeInput() {
    if (this._wrapperEl) {
      if (this._wrapperEl.parentNode) {
        this._wrapperEl.parentNode.removeChild(this._wrapperEl);
      }
      this._wrapperEl = null;
      this._inputEl = null;
    } else if (this._inputEl) {
      // Fallback for legacy single-input case
      if (this._inputEl.parentNode) {
        this._inputEl.parentNode.removeChild(this._inputEl);
      }
      this._inputEl = null;
    }
  }
}
