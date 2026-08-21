/**
 * LineTool — draws straight lines between two points on the whiteboard.
 *
 * Implements the DrawingTool interface:
 *   name, cursor, onPointerDown, onPointerMove, onPointerUp, onCancel, renderPreview
 *
 * Behavior:
 * - Records start point on pointerdown (normalized to 0.0–1.0)
 * - Renders a live preview line on pointermove
 * - Finalizes a 2-point line stroke on pointerup
 * - Finalizes at last valid position if pointer leaves canvas (pointerleave)
 * - Discards zero-length lines (start == end)
 * - Uses current color from config, fixed width 3px (normalized)
 */

import { normalize, normalizeWidth, denormalize } from './coords.js';

export class LineTool {
  /**
   * @param {object} config
   * @param {() => string} config.getColor — returns current hex color
   * @param {() => HTMLCanvasElement} config.getCanvas — returns the whiteboard canvas
   * @param {() => number} [config.getWidth] — returns the current stroke width (1–20)
   * @param {() => number} [config.getOpacity] — returns the current opacity (0.1–1.0)
   */
  constructor(config) {
    this.name = 'line';
    this.cursor = 'crosshair';

    /** @type {() => string} */
    this._getColor = config.getColor;
    /** @type {() => HTMLCanvasElement} */
    this._getCanvas = config.getCanvas;
    /** @type {() => number} */
    this._getWidth = config.getWidth || (() => 3);
    /** @type {() => number} */
    this._getOpacity = config.getOpacity || (() => 1.0);

    /** @type {[number, number] | null} */
    this._startPoint = null;
    /** @type {[number, number] | null} */
    this._currentPoint = null;
    /** @type {boolean} */
    this._drawing = false;
  }

  /**
   * Record the start point when the pointer goes down.
   * @param {PointerEvent} e
   */
  onPointerDown(e) {
    const canvas = this._getCanvas();
    const w = canvas.width;
    const h = canvas.height;

    this._startPoint = normalize(e.offsetX, e.offsetY, w, h);
    this._currentPoint = this._startPoint;
    this._drawing = true;
  }

  /**
   * Update the current endpoint for the preview line.
   * @param {PointerEvent} e
   */
  onPointerMove(e) {
    if (!this._drawing) return;

    const canvas = this._getCanvas();
    const w = canvas.width;
    const h = canvas.height;

    this._currentPoint = normalize(e.offsetX, e.offsetY, w, h);
  }

  /**
   * Finalize the line stroke on pointer release.
   * Returns the stroke object or null if zero-length.
   * @param {PointerEvent} e
   * @returns {object|null}
   */
  onPointerUp(e) {
    if (!this._drawing) return null;

    const canvas = this._getCanvas();
    const w = canvas.width;
    const h = canvas.height;

    const endPoint = normalize(e.offsetX, e.offsetY, w, h);
    const stroke = this._finalize(endPoint, w);

    this._reset();
    return stroke;
  }

  /**
   * Finalize at last valid position when pointer leaves canvas bounds.
   * @returns {object|null}
   */
  onPointerLeave() {
    if (!this._drawing) return null;

    const canvas = this._getCanvas();
    const w = canvas.width;

    // Use _currentPoint as the last valid position within bounds
    const stroke = this._finalize(this._currentPoint, w);

    this._reset();
    return stroke;
  }

  /**
   * Cancel any in-progress line without producing a stroke.
   */
  onCancel() {
    this._reset();
  }

  /**
   * Render a preview line from start to current point.
   * @param {CanvasRenderingContext2D} ctx
   */
  renderPreview(ctx) {
    if (!this._drawing || !this._startPoint || !this._currentPoint) return;

    const canvas = this._getCanvas();
    const w = canvas.width;
    const h = canvas.height;

    const [x0, y0] = denormalize(this._startPoint[0], this._startPoint[1], w, h);
    const [x1, y1] = denormalize(this._currentPoint[0], this._currentPoint[1], w, h);

    ctx.save();
    ctx.globalAlpha = this._getOpacity();
    ctx.strokeStyle = this._getColor();
    ctx.lineWidth = this._getWidth();
    ctx.lineCap = 'round';
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();
    ctx.restore();
  }

  /**
   * Build the finalized stroke from start to end.
   * Returns null if the line is zero-length (start == end).
   *
   * @param {[number, number]} endPoint
   * @param {number} viewportWidth
   * @returns {object|null}
   * @private
   */
  _finalize(endPoint, viewportWidth) {
    if (!this._startPoint || !endPoint) return null;

    // Discard zero-length lines
    if (this._startPoint[0] === endPoint[0] && this._startPoint[1] === endPoint[1]) {
      return null;
    }

    return {
      id: crypto.randomUUID(),
      type: 'line',
      points: [this._startPoint, endPoint],
      color: this._getColor(),
      width: normalizeWidth(this._getWidth(), viewportWidth),
      opacity: this._getOpacity(),
    };
  }

  /**
   * Reset internal drawing state.
   * @private
   */
  _reset() {
    this._startPoint = null;
    this._currentPoint = null;
    this._drawing = false;
  }
}
