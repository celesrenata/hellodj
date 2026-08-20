/**
 * TextTool — places text annotations on the whiteboard overlay.
 *
 * Implements the DrawingTool interface:
 *   name, cursor, onPointerDown, onPointerMove, onPointerUp, onCancel, renderPreview
 *
 * Behavior:
 * - On pointerdown: creates a text input element positioned at the click location
 * - Max length 200 characters
 * - On Enter or blur: finalizes a text stroke with content + text_bg toggle state
 * - Rejects empty/whitespace-only input (no stroke created)
 * - On Escape: cancels without creating a stroke
 * - Uses current color, font size 16px
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

    /** @type {HTMLInputElement|null} Active text input element */
    this._inputEl = null;
    /** @type {[number, number]|null} Normalized position of the text placement */
    this._position = null;
  }

  /**
   * On pointerdown: normalize the click position and show a text input element.
   * If an input is already active, finalize it first.
   * @param {PointerEvent} e
   */
  onPointerDown(e) {
    // If there's already an active input, finalize it before opening a new one
    if (this._inputEl) {
      this._finalizeInput();
    }

    const { width, height } = this._getCanvasSize();
    if (width === 0 || height === 0) return;

    this._position = normalize(e.offsetX, e.offsetY, width, height);
    this._showInput(e.offsetX, e.offsetY);
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
   * @param {number} pixelX - X position in CSS pixels
   * @param {number} pixelY - Y position in CSS pixels
   * @private
   */
  _showInput(pixelX, pixelY) {
    const container = this._getContainer();
    if (!container) return;

    const input = document.createElement('input');
    input.type = 'text';
    input.maxLength = 200;
    input.style.position = 'absolute';
    input.style.left = `${pixelX}px`;
    input.style.top = `${pixelY}px`;
    input.style.fontSize = '16px';
    input.style.color = this._getColor();
    input.style.background = 'rgba(0, 0, 0, 0.5)';
    input.style.border = '1px solid rgba(255, 255, 255, 0.4)';
    input.style.borderRadius = '2px';
    input.style.padding = '2px 4px';
    input.style.outline = 'none';
    input.style.zIndex = '26';
    input.style.minWidth = '100px';
    input.style.fontFamily = 'sans-serif';

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

    // Handle blur → finalize
    input.addEventListener('blur', () => {
      // Only finalize if the input still exists (wasn't already removed by Escape/Enter)
      if (this._inputEl) {
        this._finalizeInput();
      }
    });

    container.appendChild(input);
    this._inputEl = input;

    // Focus the input after appending to DOM
    requestAnimationFrame(() => {
      if (this._inputEl) {
        this._inputEl.focus();
      }
    });
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
   * Remove the input element from the DOM and clear internal reference.
   * @private
   */
  _removeInput() {
    if (this._inputEl) {
      // Remove event listeners by removing the element
      if (this._inputEl.parentNode) {
        this._inputEl.parentNode.removeChild(this._inputEl);
      }
      this._inputEl = null;
    }
  }
}
