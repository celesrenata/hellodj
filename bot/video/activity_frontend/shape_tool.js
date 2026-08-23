/**
 * ShapeTool — draws shapes on the whiteboard overlay.
 *
 * Supported shape types:
 * - rect: Rectangle
 * - ellipse: Ellipse (oval)
 * - circle: Perfect circle (constrained to smallest dimension)
 * - triangle: Equilateral-ish triangle fitting the bounding box
 * - star: 5-pointed star
 * - arrow: Line with arrowhead
 *
 * Sub-types are configurable via setShapeType(). The tool records a start point
 * on pointerdown, renders a live preview on pointermove, and finalizes the stroke
 * on pointerup if the bounding box exceeds 5 CSS pixels in both dimensions.
 *
 * Shapes can optionally be marked as "animated" which causes them to rotate
 * continuously when rendered.
 *
 * All coordinates are normalized to 0.0–1.0 using the coords module.
 * Strokes use outline only (not filled), 3px width, current color.
 */

import { normalize, denormalize, normalizeWidth } from './coords.js';

const STROKE_WIDTH_PX = 3;
const MIN_SIZE_PX = 5;

/** @type {string[]} All valid shape types */
export const SHAPE_TYPES = ['rect', 'ellipse', 'circle', 'triangle', 'star', 'arrow'];

export class ShapeTool {
  constructor() {
    /** @type {string} */
    this.name = 'shape';
    /** @type {string} */
    this.cursor = 'crosshair';
    /** @type {string} */
    this.shapeType = 'rect';
    /** @type {boolean} Whether new shapes should be animated (rotating) */
    this.animated = false;

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
   * @param {string} type
   */
  setShapeType(type) {
    if (SHAPE_TYPES.includes(type)) {
      this.shapeType = type;
    }
  }

  /**
   * Set whether new shapes should be animated.
   * @param {boolean} animated
   */
  setAnimated(animated) {
    this.animated = !!animated;
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

    // Add animated flag if enabled
    if (this.animated) {
      stroke.animated = true;
    }

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
      case 'circle':
        this._previewCircle(ctx, x0, y0, x1, y1);
        break;
      case 'triangle':
        this._previewTriangle(ctx, x0, y0, x1, y1);
        break;
      case 'star':
        this._previewStar(ctx, x0, y0, x1, y1);
        break;
      case 'arrow':
        this._previewArrow(ctx, x0, y0, x1, y1);
        break;
    }

    ctx.restore();
  }

  // ─── Preview Methods ────────────────────────────────────────────────────

  _previewRect(ctx, x0, y0, x1, y1) {
    const rx = Math.min(x0, x1);
    const ry = Math.min(y0, y1);
    const rw = Math.abs(x1 - x0);
    const rh = Math.abs(y1 - y0);

    ctx.beginPath();
    ctx.strokeRect(rx, ry, rw, rh);
  }

  _previewEllipse(ctx, x0, y0, x1, y1) {
    const cx = (x0 + x1) / 2;
    const cy = (y0 + y1) / 2;
    const rx = Math.abs(x1 - x0) / 2;
    const ry = Math.abs(y1 - y0) / 2;

    ctx.beginPath();
    ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
    ctx.stroke();
  }

  _previewCircle(ctx, x0, y0, x1, y1) {
    const cx = (x0 + x1) / 2;
    const cy = (y0 + y1) / 2;
    // Use the smaller dimension to make a perfect circle
    const r = Math.min(Math.abs(x1 - x0), Math.abs(y1 - y0)) / 2;

    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.stroke();
  }

  _previewTriangle(ctx, x0, y0, x1, y1) {
    const minX = Math.min(x0, x1);
    const maxX = Math.max(x0, x1);
    const minY = Math.min(y0, y1);
    const maxY = Math.max(y0, y1);

    // Triangle: top-center, bottom-left, bottom-right
    const topX = (minX + maxX) / 2;
    const topY = minY;
    const blX = minX;
    const blY = maxY;
    const brX = maxX;
    const brY = maxY;

    ctx.beginPath();
    ctx.moveTo(topX, topY);
    ctx.lineTo(blX, blY);
    ctx.lineTo(brX, brY);
    ctx.closePath();
    ctx.stroke();
  }

  _previewStar(ctx, x0, y0, x1, y1) {
    const cx = (x0 + x1) / 2;
    const cy = (y0 + y1) / 2;
    const outerR = Math.min(Math.abs(x1 - x0), Math.abs(y1 - y0)) / 2;
    const innerR = outerR * 0.4;

    this._drawStar(ctx, cx, cy, outerR, innerR, 5);
    ctx.stroke();
  }

  _previewArrow(ctx, x0, y0, x1, y1) {
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();

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

  // ─── Helpers ────────────────────────────────────────────────────────────

  /**
   * Draw a star path (5-pointed by default).
   * @param {CanvasRenderingContext2D} ctx
   * @param {number} cx - Center X
   * @param {number} cy - Center Y
   * @param {number} outerR - Outer radius
   * @param {number} innerR - Inner radius
   * @param {number} points - Number of points
   */
  _drawStar(ctx, cx, cy, outerR, innerR, points) {
    const step = Math.PI / points;

    ctx.beginPath();
    for (let i = 0; i < points * 2; i++) {
      const r = i % 2 === 0 ? outerR : innerR;
      const angle = -Math.PI / 2 + i * step;
      const x = cx + r * Math.cos(angle);
      const y = cy + r * Math.sin(angle);
      if (i === 0) {
        ctx.moveTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
    }
    ctx.closePath();
  }

  _reset() {
    this.startPoint = null;
    this.currentPoint = null;
    this.drawing = false;
  }
}
