/**
 * PenTool — freehand drawing tool for the whiteboard overlay.
 *
 * Captures pointer positions as normalized coordinates on pointermove,
 * finalizes a stroke on pointerup (or pointer leaving canvas bounds).
 * Renders a live preview using the same quadratic Bézier interpolation
 * as the StrokeRenderer.
 *
 * Implements the DrawingTool interface (see tools.js).
 */

import { normalize, normalizeWidth, denormalize, denormalizeWidth } from './coords.js';

export class PenTool {
  /**
   * @param {object} config
   * @param {() => HTMLCanvasElement} config.getCanvas — returns the whiteboard canvas element
   * @param {() => string} config.getColor             — returns the current hex color string
   */
  constructor(config) {
    this.name = 'pen';
    this.cursor = 'crosshair';

    /** @type {() => HTMLCanvasElement} */
    this._getCanvas = config.getCanvas;
    /** @type {() => string} */
    this._getColor = config.getColor;

    /** @type {Array<[number, number]>} Normalized points captured during draw */
    this._points = [];
    /** @type {boolean} Whether we are actively capturing a stroke */
    this._capturing = false;
  }

  /**
   * Start capturing a freehand stroke. Records the first normalized point.
   * @param {PointerEvent} e
   */
  onPointerDown(e) {
    const canvas = this._getCanvas();
    const w = canvas.width;
    const h = canvas.height;
    if (w === 0 || h === 0) return;

    this._capturing = true;
    this._points = [];

    const point = normalize(e.offsetX, e.offsetY, w, h);
    this._points.push(point);
  }

  /**
   * Record additional normalized points while the pointer is held down.
   * @param {PointerEvent} e
   */
  onPointerMove(e) {
    if (!this._capturing) return;

    const canvas = this._getCanvas();
    const w = canvas.width;
    const h = canvas.height;
    if (w === 0 || h === 0) return;

    const point = normalize(e.offsetX, e.offsetY, w, h);
    this._points.push(point);
  }

  /**
   * Finalize the stroke on pointer release.
   * Returns a stroke object if we have ≥2 points, null otherwise.
   * @param {PointerEvent} e
   * @returns {object|null} Finalized stroke or null
   */
  onPointerUp(e) {
    if (!this._capturing) return null;

    // Record the final point
    const canvas = this._getCanvas();
    const w = canvas.width;
    const h = canvas.height;
    if (w > 0 && h > 0) {
      const point = normalize(e.offsetX, e.offsetY, w, h);
      this._points.push(point);
    }

    return this._finalize();
  }

  /**
   * Finalize the stroke when the pointer leaves the canvas bounds.
   * Called externally by the whiteboard overlay on pointerleave.
   * @param {PointerEvent} e
   * @returns {object|null} Finalized stroke or null
   */
  onPointerLeave(e) {
    if (!this._capturing) return null;

    // Record the boundary point (clamped by normalize)
    const canvas = this._getCanvas();
    const w = canvas.width;
    const h = canvas.height;
    if (w > 0 && h > 0) {
      const point = normalize(e.offsetX, e.offsetY, w, h);
      this._points.push(point);
    }

    return this._finalize();
  }

  /**
   * Cancel the current drawing state (e.g. on tool switch or Escape).
   */
  onCancel() {
    this._capturing = false;
    this._points = [];
  }

  /**
   * Render the in-progress freehand path on the provided canvas context.
   * Uses the same quadratic Bézier interpolation as renderer.js.
   * @param {CanvasRenderingContext2D} ctx
   */
  renderPreview(ctx) {
    if (!this._capturing || this._points.length < 2) return;

    const canvas = this._getCanvas();
    const w = canvas.width;
    const h = canvas.height;
    if (w === 0 || h === 0) return;

    const lineWidth = denormalizeWidth(normalizeWidth(3, w), w);

    ctx.save();
    ctx.strokeStyle = this._getColor();
    ctx.lineWidth = lineWidth;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.beginPath();

    const [x0, y0] = denormalize(this._points[0][0], this._points[0][1], w, h);
    ctx.moveTo(x0, y0);

    if (this._points.length === 2) {
      const [x1, y1] = denormalize(this._points[1][0], this._points[1][1], w, h);
      ctx.lineTo(x1, y1);
    } else {
      for (let i = 0; i < this._points.length - 2; i++) {
        const [cx, cy] = denormalize(this._points[i][0], this._points[i][1], w, h);
        const [nx, ny] = denormalize(this._points[i + 1][0], this._points[i + 1][1], w, h);
        const midX = (cx + nx) / 2;
        const midY = (cy + ny) / 2;
        ctx.quadraticCurveTo(cx, cy, midX, midY);
      }
      // Final segment to the last point
      const [lastCtrlX, lastCtrlY] = denormalize(
        this._points[this._points.length - 2][0],
        this._points[this._points.length - 2][1],
        w, h
      );
      const [lastX, lastY] = denormalize(
        this._points[this._points.length - 1][0],
        this._points[this._points.length - 1][1],
        w, h
      );
      ctx.quadraticCurveTo(lastCtrlX, lastCtrlY, lastX, lastY);
    }

    ctx.stroke();
    ctx.restore();
  }

  /**
   * Finalize the current stroke and reset capture state.
   * @returns {object|null} Finalized stroke object or null if insufficient points
   * @private
   */
  _finalize() {
    const points = this._points;
    this._capturing = false;
    this._points = [];

    if (points.length < 2) return null;

    const canvas = this._getCanvas();
    const w = canvas.width;
    const strokeWidth = normalizeWidth(3, w);

    return {
      id: crypto.randomUUID(),
      type: 'freehand',
      points,
      color: this._getColor(),
      width: strokeWidth,
    };
  }
}
