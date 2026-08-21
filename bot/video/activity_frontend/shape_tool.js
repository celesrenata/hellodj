/**
 * ShapeTool — draws rect, ellipse, and arrow shapes on the whiteboard overlay.
 *
 * Sub-types are configurable via setShapeType(). The tool records a start point
 * on pointerdown, renders a live preview on pointermove, and finalizes the stroke
 * on pointerup if the bounding box exceeds 5 CSS pixels in both dimensions.
 *
 * All coordinates are normalized to 0.0–1.0 using the coords module.
 * Strokes use outline only (not filled), 3px width, current color.
 */

import { normalize, denormalize, normalizeWidth } from './coords.js';

const STROKE_WIDTH_PX = 3;
const MIN_SIZE_PX = 5;

export class ShapeTool {
  constructor() {
    /** @type {string} */
    this.name = 'shape';
    /** @type {string} */
    this.cursor = 'crosshair';
    /** @type {'rect'|'ellipse'|'arrow'} */
    this.shapeType = 'rect';

    /** @type {[number, number]|null} Normalized start point */
    this.startPoint = null;
    /** @type {[number, number]|null} Normalized current point */
    this.currentPoint = null;
    /** @type {boolean} */
    this.drawing = false;

    /** @type {string} */
    this.color = '#FFFFFF';
    /** @type {number} Canvas width in CSS pixels */
    this.canvasWidth = 0;
    /** @type {number} Canvas height in CSS pixels */
    this.canvasHeight = 0;
  }

  /**
   * Set the active shape sub-type.
   * @param {'rect'|'ellipse'|'arrow'} type
   */
  setShapeType(type) {
    if (type === 'rect' || type === 'ellipse' || type === 'arrow') {
      this.shapeType = type;
    }
  }

  /**
   * Set the stroke color.
   * @param {string} color - Hex color string
   */
  setColor(color) {
    this.color = color;
  }

  /**
   * Update canvas dimensions (call on resize or before use).
   * @param {number} width
   * @param {number} height
   */
  setCanvasSize(width, height) {
    this.canvasWidth = width;
    this.canvasHeight = height;
  }

  /**
   * Handle pointer down — record start point.
   * @param {PointerEvent} e
   */
  onPointerDown(e) {
    const rect = e.target.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;
    const w = rect.width;
    const h = rect.height;

    this.canvasWidth = w;
    this.canvasHeight = h;
    this.startPoint = normalize(px, py, w, h);
    this.currentPoint = this.startPoint;
    this.drawing = true;
  }

  /**
   * Handle pointer move — update current point (clamped to [0,1]).
   * @param {PointerEvent} e
   */
  onPointerMove(e) {
    if (!this.drawing || !this.startPoint) return;

    const rect = e.target.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;
    const w = rect.width;
    const h = rect.height;

    this.canvasWidth = w;
    this.canvasHeight = h;
    // normalize() already clamps to [0, 1]
    this.currentPoint = normalize(px, py, w, h);
  }

  /**
   * Handle pointer up — finalize shape if bounding box > 5px in both dimensions.
   * @param {PointerEvent} e
   * @returns {object|null} Finalized stroke or null if too small
   */
  onPointerUp(e) {
    if (!this.drawing || !this.startPoint) {
      this._reset();
      return null;
    }

    const rect = e.target.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;
    const w = rect.width;
    const h = rect.height;

    this.canvasWidth = w;
    this.canvasHeight = h;
    this.currentPoint = normalize(px, py, w, h);

    // Denormalize to check pixel dimensions of bounding box
    const [sx, sy] = denormalize(this.startPoint[0], this.startPoint[1], w, h);
    const [ex, ey] = denormalize(this.currentPoint[0], this.currentPoint[1], w, h);
    const bboxWidth = Math.abs(ex - sx);
    const bboxHeight = Math.abs(ey - sy);

    if (bboxWidth <= MIN_SIZE_PX || bboxHeight <= MIN_SIZE_PX) {
      this._reset();
      return null;
    }

    const stroke = {
      id: crypto.randomUUID(),
      type: this.shapeType,
      points: [this.startPoint, this.currentPoint],
      color: this.color,
      width: normalizeWidth(STROKE_WIDTH_PX, w),
    };

    this._reset();
    return stroke;
  }

  /**
   * Cancel the current drawing operation.
   */
  onCancel() {
    this._reset();
  }

  /**
   * Render a live preview of the shape being drawn.
   * @param {CanvasRenderingContext2D} ctx
   */
  renderPreview(ctx) {
    if (!this.drawing || !this.startPoint || !this.currentPoint) return;

    const w = this.canvasWidth;
    const h = this.canvasHeight;
    if (w === 0 || h === 0) return;

    const [x0, y0] = denormalize(this.startPoint[0], this.startPoint[1], w, h);
    const [x1, y1] = denormalize(this.currentPoint[0], this.currentPoint[1], w, h);

    ctx.save();
    ctx.strokeStyle = this.color;
    ctx.lineWidth = STROKE_WIDTH_PX;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';

    switch (this.shapeType) {
      case 'rect':
        this._previewRect(ctx, x0, y0, x1, y1);
        break;
      case 'ellipse':
        this._previewEllipse(ctx, x0, y0, x1, y1);
        break;
      case 'arrow':
        this._previewArrow(ctx, x0, y0, x1, y1);
        break;
    }

    ctx.restore();
  }

  /**
   * Preview rectangle — outline only.
   */
  _previewRect(ctx, x0, y0, x1, y1) {
    const rx = Math.min(x0, x1);
    const ry = Math.min(y0, y1);
    const rw = Math.abs(x1 - x0);
    const rh = Math.abs(y1 - y0);

    ctx.beginPath();
    ctx.strokeRect(rx, ry, rw, rh);
  }

  /**
   * Preview ellipse — outline only.
   */
  _previewEllipse(ctx, x0, y0, x1, y1) {
    const cx = (x0 + x1) / 2;
    const cy = (y0 + y1) / 2;
    const rx = Math.abs(x1 - x0) / 2;
    const ry = Math.abs(y1 - y0) / 2;

    ctx.beginPath();
    ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
    ctx.stroke();
  }

  /**
   * Preview arrow — line with proportional arrowhead at endpoint.
   */
  _previewArrow(ctx, x0, y0, x1, y1) {
    // Draw the line
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();

    // Draw arrowhead proportional to stroke width
    const headLength = Math.max(STROKE_WIDTH_PX * 4, 10);
    const angle = Math.atan2(y1 - y0, x1 - x0);

    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(
      x1 - headLength * Math.cos(angle - Math.PI / 6),
      y1 - headLength * Math.sin(angle - Math.PI / 6)
    );
    ctx.moveTo(x1, y1);
    ctx.lineTo(
      x1 - headLength * Math.cos(angle + Math.PI / 6),
      y1 - headLength * Math.sin(angle + Math.PI / 6)
    );
    ctx.stroke();
  }

  /**
   * Reset internal state.
   */
  _reset() {
    this.startPoint = null;
    this.currentPoint = null;
    this.drawing = false;
  }
}
