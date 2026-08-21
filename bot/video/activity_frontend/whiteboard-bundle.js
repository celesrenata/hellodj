/**
 * whiteboard-bundle.js — Self-contained whiteboard overlay bundle.
 * Auto-generated from individual ES modules. Do not edit directly.
 *
 * Exposes window.WhiteboardBundle for use by app.js.
 */
(function() {
'use strict';

// ─── coords.js ───────────────────────────────────────────────────────────────

/**
 * Coordinate normalization utilities for the whiteboard overlay.
 *
 * All stroke data uses viewport-relative coordinates (0.0–1.0) so
 * drawings render at correct positions regardless of viewer screen size.
 * Normalization uses 4 decimal places (0.01% precision).
 */

/**
 * Normalize pixel coordinates to the 0.0–1.0 range.
 * Out-of-bounds values are clamped.
 *
 * @param {number} pixelX - Horizontal pixel position
 * @param {number} pixelY - Vertical pixel position
 * @param {number} width  - Viewport width in pixels
 * @param {number} height - Viewport height in pixels
 * @returns {[number, number]} Normalized [x, y] each in [0, 1]
 */
function normalize(pixelX, pixelY, width, height) {
  const x = Math.round(Math.max(0, Math.min(1, pixelX / width)) * 10000) / 10000;
  const y = Math.round(Math.max(0, Math.min(1, pixelY / height)) * 10000) / 10000;
  return [x, y];
}

/**
 * Denormalize coordinates back to pixel positions.
 *
 * @param {number} normX  - Normalized x coordinate (0.0–1.0)
 * @param {number} normY  - Normalized y coordinate (0.0–1.0)
 * @param {number} width  - Viewport width in pixels
 * @param {number} height - Viewport height in pixels
 * @returns {[number, number]} Pixel [x, y] positions
 */
function denormalize(normX, normY, width, height) {
  return [normX * width, normY * height];
}

/**
 * Normalize stroke width relative to viewport width.
 *
 * @param {number} cssPixels    - Stroke width in CSS pixels
 * @param {number} viewportWidth - Viewport width in pixels
 * @returns {number} Normalized width (4 decimal places)
 */
function normalizeWidth(cssPixels, viewportWidth) {
  return Math.round((cssPixels / viewportWidth) * 10000) / 10000;
}

/**
 * Denormalize stroke width back to CSS pixels.
 *
 * @param {number} normalizedWidth - Normalized width value
 * @param {number} viewportWidth   - Viewport width in pixels
 * @returns {number} Width in CSS pixels
 */
function denormalizeWidth(normalizedWidth, viewportWidth) {
  return normalizedWidth * viewportWidth;
}

// ─── hittest.js ──────────────────────────────────────────────────────────────

/**
 * Hit-testing module for the whiteboard overlay.
 *
 * All coordinates are normalized (0.0–1.0). The tolerance is provided
 * in CSS pixels and normalized against the viewport dimensions.
 */

/**
 * Compute perpendicular distance from point (px, py) to line segment (ax, ay)→(bx, by).
 */
function distToSegment(px, py, ax, ay, bx, by) {
  const dx = bx - ax;
  const dy = by - ay;
  const lenSq = dx * dx + dy * dy;

  if (lenSq === 0) {
    const ex = px - ax;
    const ey = py - ay;
    return Math.sqrt(ex * ex + ey * ey);
  }

  let t = ((px - ax) * dx + (py - ay) * dy) / lenSq;
  t = Math.max(0, Math.min(1, t));

  const projX = ax + t * dx;
  const projY = ay + t * dy;
  const ex = px - projX;
  const ey = py - projY;
  return Math.sqrt(ex * ex + ey * ey);
}

function hitFreehand(cx, cy, points) {
  if (points.length < 2) {
    if (points.length === 1) {
      const dx = cx - points[0][0];
      const dy = cy - points[0][1];
      return Math.sqrt(dx * dx + dy * dy);
    }
    return Infinity;
  }

  let minDist = Infinity;
  for (let i = 0; i < points.length - 1; i++) {
    const d = distToSegment(
      cx, cy,
      points[i][0], points[i][1],
      points[i + 1][0], points[i + 1][1]
    );
    if (d < minDist) minDist = d;
  }
  return minDist;
}

function hitLine(cx, cy, points) {
  if (points.length < 2) return Infinity;
  return distToSegment(
    cx, cy,
    points[0][0], points[0][1],
    points[1][0], points[1][1]
  );
}

function hitRect(cx, cy, points) {
  if (points.length < 2) return Infinity;

  const [x1, y1] = points[0];
  const [x2, y2] = points[1];

  const tl = [Math.min(x1, x2), Math.min(y1, y2)];
  const tr = [Math.max(x1, x2), Math.min(y1, y2)];
  const br = [Math.max(x1, x2), Math.max(y1, y2)];
  const bl = [Math.min(x1, x2), Math.max(y1, y2)];

  const d1 = distToSegment(cx, cy, tl[0], tl[1], tr[0], tr[1]);
  const d2 = distToSegment(cx, cy, tr[0], tr[1], br[0], br[1]);
  const d3 = distToSegment(cx, cy, br[0], br[1], bl[0], bl[1]);
  const d4 = distToSegment(cx, cy, bl[0], bl[1], tl[0], tl[1]);

  return Math.min(d1, d2, d3, d4);
}

function hitEllipse(cx, cy, points) {
  if (points.length < 2) return Infinity;

  const [x1, y1] = points[0];
  const [x2, y2] = points[1];

  const centerX = (x1 + x2) / 2;
  const centerY = (y1 + y2) / 2;
  const rx = Math.abs(x2 - x1) / 2;
  const ry = Math.abs(y2 - y1) / 2;

  if (rx === 0 && ry === 0) {
    const dx = cx - centerX;
    const dy = cy - centerY;
    return Math.sqrt(dx * dx + dy * dy);
  }

  const NUM_SAMPLES = 32;
  let minDist = Infinity;

  for (let i = 0; i < NUM_SAMPLES; i++) {
    const angle1 = (2 * Math.PI * i) / NUM_SAMPLES;
    const angle2 = (2 * Math.PI * ((i + 1) % NUM_SAMPLES)) / NUM_SAMPLES;

    const ax = centerX + rx * Math.cos(angle1);
    const ay = centerY + ry * Math.sin(angle1);
    const bx = centerX + rx * Math.cos(angle2);
    const by = centerY + ry * Math.sin(angle2);

    const d = distToSegment(cx, cy, ax, ay, bx, by);
    if (d < minDist) minDist = d;
  }

  return minDist;
}

function hitTextBbox(cx, cy, points, tolerance) {
  if (points.length < 1) return false;

  const [x, y] = points[0];
  const boxWidth = tolerance * 10;
  const boxHeight = tolerance * 3;

  return cx >= x && cx <= x + boxWidth && cy >= y - boxHeight && cy <= y + boxHeight;
}

function hitBbox(cx, cy, points) {
  if (points.length < 2) return false;

  const [x1, y1] = points[0];
  const [x2, y2] = points[1];

  const minX = Math.min(x1, x2);
  const maxX = Math.max(x1, x2);
  const minY = Math.min(y1, y2);
  const maxY = Math.max(y1, y2);

  return cx >= minX && cx <= maxX && cy >= minY && cy <= maxY;
}

function hitTest(clickX, clickY, strokes, tolerancePx, viewportWidth, viewportHeight) {
  if (!strokes || strokes.length === 0) return null;

  const tolX = tolerancePx / viewportWidth;
  const tolY = tolerancePx / viewportHeight;
  const tolerance = Math.max(tolX, tolY);

  for (let i = strokes.length - 1; i >= 0; i--) {
    const stroke = strokes[i];
    const { type, points } = stroke;

    if (!points || points.length === 0) continue;

    let hit = false;

    switch (type) {
      case 'freehand': {
        const dist = hitFreehand(clickX, clickY, points);
        hit = dist <= tolerance;
        break;
      }
      case 'line':
      case 'arrow': {
        const dist = hitLine(clickX, clickY, points);
        hit = dist <= tolerance;
        break;
      }
      case 'rect': {
        const dist = hitRect(clickX, clickY, points);
        hit = dist <= tolerance;
        break;
      }
      case 'ellipse': {
        const dist = hitEllipse(clickX, clickY, points);
        hit = dist <= tolerance;
        break;
      }
      case 'text': {
        hit = hitTextBbox(clickX, clickY, points, tolerance);
        break;
      }
      case 'sticker': {
        hit = hitBbox(clickX, clickY, points);
        break;
      }
      default:
        break;
    }

    if (hit) return stroke;
  }

  return null;
}

// ─── renderer.js ─────────────────────────────────────────────────────────────

class StrokeRenderer {
  constructor(ctx, width, height, redrawCallback) {
    this.ctx = ctx;
    this.width = width;
    this.height = height;
    this.redrawCallback = redrawCallback || null;
    this.imageCache = new Map();
  }

  resize(width, height) {
    this.width = width;
    this.height = height;
  }

  renderStroke(stroke) {
    const ctx = this.ctx;
    const w = this.width;
    const h = this.height;
    const lineWidth = denormalizeWidth(stroke.width, w);

    ctx.save();
    ctx.globalAlpha = stroke.opacity != null ? stroke.opacity : 1.0;
    ctx.strokeStyle = stroke.color;
    ctx.fillStyle = stroke.color;
    ctx.lineWidth = lineWidth;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';

    switch (stroke.type) {
      case 'freehand':
        this._renderFreehand(stroke.points, w, h);
        break;
      case 'line':
        this._renderLine(stroke.points, w, h);
        break;
      case 'arrow':
        this._renderArrow(stroke.points, w, h, lineWidth);
        break;
      case 'rect':
        this._renderRect(stroke.points, w, h);
        break;
      case 'ellipse':
        this._renderEllipse(stroke.points, w, h);
        break;
      case 'text':
        this._renderText(stroke, w, h);
        break;
      case 'sticker':
        this._renderSticker(stroke, w, h);
        break;
    }

    ctx.restore();
  }

  _renderFreehand(points, w, h) {
    if (points.length < 2) return;

    const ctx = this.ctx;
    ctx.beginPath();

    const [x0, y0] = denormalize(points[0][0], points[0][1], w, h);
    ctx.moveTo(x0, y0);

    if (points.length === 2) {
      const [x1, y1] = denormalize(points[1][0], points[1][1], w, h);
      ctx.lineTo(x1, y1);
    } else {
      for (let i = 0; i < points.length - 2; i++) {
        const [cx, cy] = denormalize(points[i][0], points[i][1], w, h);
        const [nx, ny] = denormalize(points[i + 1][0], points[i + 1][1], w, h);
        const midX = (cx + nx) / 2;
        const midY = (cy + ny) / 2;
        ctx.quadraticCurveTo(cx, cy, midX, midY);
      }
      const [lastCtrlX, lastCtrlY] = denormalize(
        points[points.length - 2][0], points[points.length - 2][1], w, h
      );
      const [lastX, lastY] = denormalize(
        points[points.length - 1][0], points[points.length - 1][1], w, h
      );
      ctx.quadraticCurveTo(lastCtrlX, lastCtrlY, lastX, lastY);
    }

    ctx.stroke();
  }

  _renderLine(points, w, h) {
    if (points.length < 2) return;

    const ctx = this.ctx;
    const [x0, y0] = denormalize(points[0][0], points[0][1], w, h);
    const [x1, y1] = denormalize(points[1][0], points[1][1], w, h);

    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();
  }

  _renderArrow(points, w, h, lineWidth) {
    if (points.length < 2) return;

    const ctx = this.ctx;
    const [x0, y0] = denormalize(points[0][0], points[0][1], w, h);
    const [x1, y1] = denormalize(points[1][0], points[1][1], w, h);

    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();

    const headLength = Math.max(lineWidth * 4, 10);
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

  _renderRect(points, w, h) {
    if (points.length < 2) return;

    const ctx = this.ctx;
    const [x0, y0] = denormalize(points[0][0], points[0][1], w, h);
    const [x1, y1] = denormalize(points[1][0], points[1][1], w, h);

    const rx = Math.min(x0, x1);
    const ry = Math.min(y0, y1);
    const rw = Math.abs(x1 - x0);
    const rh = Math.abs(y1 - y0);

    ctx.beginPath();
    ctx.strokeRect(rx, ry, rw, rh);
  }

  _renderEllipse(points, w, h) {
    if (points.length < 2) return;

    const ctx = this.ctx;
    const [x0, y0] = denormalize(points[0][0], points[0][1], w, h);
    const [x1, y1] = denormalize(points[1][0], points[1][1], w, h);

    const cx = (x0 + x1) / 2;
    const cy = (y0 + y1) / 2;
    const rx = Math.abs(x1 - x0) / 2;
    const ry = Math.abs(y1 - y0) / 2;

    ctx.beginPath();
    ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
    ctx.stroke();
  }

  _renderText(stroke, w, h) {
    if (!stroke.text || !stroke.points || stroke.points.length < 1) return;

    const ctx = this.ctx;
    const [x, y] = denormalize(stroke.points[0][0], stroke.points[0][1], w, h);
    const fontSize = 16;
    const padding = 4;

    ctx.font = `${fontSize}px sans-serif`;
    ctx.textBaseline = 'top';

    if (stroke.textBg || stroke.text_bg) {
      const metrics = ctx.measureText(stroke.text);
      const textWidth = metrics.width;
      const textHeight = fontSize;

      ctx.fillStyle = 'rgba(0, 0, 0, 0.5)';
      ctx.fillRect(
        x - padding,
        y - padding,
        textWidth + padding * 2,
        textHeight + padding * 2
      );
    }

    ctx.fillStyle = stroke.color;
    ctx.fillText(stroke.text, x, y);
  }

  _renderSticker(stroke, w, h) {
    if (!stroke.sticker_category || !stroke.sticker_filename) return;
    if (!stroke.points || stroke.points.length < 2) return;

    const [x1, y1] = denormalize(stroke.points[0][0], stroke.points[0][1], w, h);
    const [x2, y2] = denormalize(stroke.points[1][0], stroke.points[1][1], w, h);
    const boxW = Math.abs(x2 - x1);
    const boxH = Math.abs(y2 - y1);
    const boxX = Math.min(x1, x2);
    const boxY = Math.min(y1, y2);

    const url = `/activity/stickers/${stroke.sticker_category}/${stroke.sticker_filename}`;
    const img = this._getOrLoadImage(url);

    if (img && img.complete && img.naturalWidth > 0) {
      const imgAspect = img.naturalWidth / img.naturalHeight;
      const boxAspect = boxW / boxH;
      let drawW, drawH, drawX, drawY;

      if (imgAspect > boxAspect) {
        drawW = boxW;
        drawH = boxW / imgAspect;
        drawX = boxX;
        drawY = boxY + (boxH - drawH) / 2;
      } else {
        drawH = boxH;
        drawW = boxH * imgAspect;
        drawX = boxX + (boxW - drawW) / 2;
        drawY = boxY;
      }

      this.ctx.drawImage(img, drawX, drawY, drawW, drawH);
    }
  }

  _getOrLoadImage(url) {
    if (this.imageCache.has(url)) {
      return this.imageCache.get(url);
    }

    const img = new Image();
    img.src = url;
    img.onload = () => {
      if (this.redrawCallback) {
        this.redrawCallback();
      }
    };
    this.imageCache.set(url, img);
    return img;
  }
}

// ─── undo_restore.js ─────────────────────────────────────────────────────────

function restoreUndoHistory(overlay, strokes) {
  overlay.undoStack.length = 0;

  for (const stroke of strokes) {
    if (stroke.author === overlay.localAuthorId) {
      overlay.undoStack.push(stroke.id);
    }
  }
}

// ─── tools.js ────────────────────────────────────────────────────────────────

class ToolManager {
  constructor(canvas) {
    this.canvas = canvas;
    this.tools = new Map();
    this.activeTool = null;
  }

  registerTool(tool) {
    this.tools.set(tool.name, tool);
  }

  selectTool(name) {
    const tool = this.tools.get(name);
    if (!tool) return;

    if (this.activeTool && this.activeTool !== tool) {
      this.activeTool.onCancel();
    }

    this.activeTool = tool;
    this.canvas.style.cursor = tool.cursor;

    if (typeof tool.activate === 'function') {
      tool.activate();
    }
  }

  getActiveTool() {
    return this.activeTool;
  }
}

// ─── pen_tool.js ─────────────────────────────────────────────────────────────

class PenTool {
  constructor(config) {
    this.name = 'pen';
    this.cursor = 'crosshair';

    this._getCanvas = config.getCanvas;
    this._getColor = config.getColor;
    this._getWidth = config.getWidth || (() => 3);
    this._getOpacity = config.getOpacity || (() => 1.0);

    this._points = [];
    this._capturing = false;
  }

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

  onPointerMove(e) {
    if (!this._capturing) return;

    const canvas = this._getCanvas();
    const w = canvas.width;
    const h = canvas.height;
    if (w === 0 || h === 0) return;

    const point = normalize(e.offsetX, e.offsetY, w, h);
    this._points.push(point);
  }

  onPointerUp(e) {
    if (!this._capturing) return null;

    const canvas = this._getCanvas();
    const w = canvas.width;
    const h = canvas.height;
    if (w > 0 && h > 0) {
      const point = normalize(e.offsetX, e.offsetY, w, h);
      this._points.push(point);
    }

    return this._finalize();
  }

  onPointerLeave(e) {
    if (!this._capturing) return null;

    const canvas = this._getCanvas();
    const w = canvas.width;
    const h = canvas.height;
    if (w > 0 && h > 0) {
      const point = normalize(e.offsetX, e.offsetY, w, h);
      this._points.push(point);
    }

    return this._finalize();
  }

  onCancel() {
    this._capturing = false;
    this._points = [];
  }

  renderPreview(ctx) {
    if (!this._capturing || this._points.length < 2) return;

    const canvas = this._getCanvas();
    const w = canvas.width;
    const h = canvas.height;
    if (w === 0 || h === 0) return;

    const lineWidth = denormalizeWidth(normalizeWidth(this._getWidth(), w), w);

    ctx.save();
    ctx.globalAlpha = this._getOpacity();
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

  _finalize() {
    const points = this._points;
    this._capturing = false;
    this._points = [];

    if (points.length < 2) return null;

    const canvas = this._getCanvas();
    const w = canvas.width;
    const strokeWidth = normalizeWidth(this._getWidth(), w);

    return {
      id: crypto.randomUUID(),
      type: 'freehand',
      points,
      color: this._getColor(),
      width: strokeWidth,
      opacity: this._getOpacity(),
    };
  }
}

// ─── line_tool.js ────────────────────────────────────────────────────────────

class LineTool {
  constructor(config) {
    this.name = 'line';
    this.cursor = 'crosshair';

    this._getColor = config.getColor;
    this._getCanvas = config.getCanvas;
    this._getWidth = config.getWidth || (() => 3);
    this._getOpacity = config.getOpacity || (() => 1.0);

    this._startPoint = null;
    this._currentPoint = null;
    this._drawing = false;
  }

  onPointerDown(e) {
    const canvas = this._getCanvas();
    const w = canvas.width;
    const h = canvas.height;

    this._startPoint = normalize(e.offsetX, e.offsetY, w, h);
    this._currentPoint = this._startPoint;
    this._drawing = true;
  }

  onPointerMove(e) {
    if (!this._drawing) return;

    const canvas = this._getCanvas();
    const w = canvas.width;
    const h = canvas.height;

    this._currentPoint = normalize(e.offsetX, e.offsetY, w, h);
  }

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

  onPointerLeave() {
    if (!this._drawing) return null;

    const canvas = this._getCanvas();
    const w = canvas.width;

    const stroke = this._finalize(this._currentPoint, w);

    this._reset();
    return stroke;
  }

  onCancel() {
    this._reset();
  }

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

  _finalize(endPoint, viewportWidth) {
    if (!this._startPoint || !endPoint) return null;

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

  _reset() {
    this._startPoint = null;
    this._currentPoint = null;
    this._drawing = false;
  }
}

// ─── shape_tool.js ───────────────────────────────────────────────────────────

const SHAPE_STROKE_WIDTH_PX = 3;
const SHAPE_MIN_SIZE_PX = 5;

class ShapeTool {
  constructor() {
    this.name = 'shape';
    this.cursor = 'crosshair';
    this.shapeType = 'rect';

    this.startPoint = null;
    this.currentPoint = null;
    this.drawing = false;

    this.color = '#FFFFFF';
    this.canvasWidth = 0;
    this.canvasHeight = 0;
  }

  setShapeType(type) {
    if (type === 'rect' || type === 'ellipse' || type === 'arrow') {
      this.shapeType = type;
    }
  }

  setColor(color) {
    this.color = color;
  }

  setCanvasSize(width, height) {
    this.canvasWidth = width;
    this.canvasHeight = height;
  }

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

    const [sx, sy] = denormalize(this.startPoint[0], this.startPoint[1], w, h);
    const [ex, ey] = denormalize(this.currentPoint[0], this.currentPoint[1], w, h);
    const bboxWidth = Math.abs(ex - sx);
    const bboxHeight = Math.abs(ey - sy);

    if (bboxWidth <= SHAPE_MIN_SIZE_PX || bboxHeight <= SHAPE_MIN_SIZE_PX) {
      this._reset();
      return null;
    }

    const stroke = {
      id: crypto.randomUUID(),
      type: this.shapeType,
      points: [this.startPoint, this.currentPoint],
      color: this.color,
      width: normalizeWidth(SHAPE_STROKE_WIDTH_PX, w),
    };

    this._reset();
    return stroke;
  }

  onCancel() {
    this._reset();
  }

  renderPreview(ctx) {
    if (!this.drawing || !this.startPoint || !this.currentPoint) return;

    const w = this.canvasWidth;
    const h = this.canvasHeight;
    if (w === 0 || h === 0) return;

    const [x0, y0] = denormalize(this.startPoint[0], this.startPoint[1], w, h);
    const [x1, y1] = denormalize(this.currentPoint[0], this.currentPoint[1], w, h);

    ctx.save();
    ctx.strokeStyle = this.color;
    ctx.lineWidth = SHAPE_STROKE_WIDTH_PX;
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

  _previewArrow(ctx, x0, y0, x1, y1) {
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();

    const headLength = Math.max(SHAPE_STROKE_WIDTH_PX * 4, 10);
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

  _reset() {
    this.startPoint = null;
    this.currentPoint = null;
    this.drawing = false;
  }
}

// ─── text_tool.js ────────────────────────────────────────────────────────────

class TextTool {
  constructor(config) {
    this.name = 'text';
    this.cursor = 'text';

    this._getCanvasSize = config.getCanvasSize;
    this._getColor = config.getColor;
    this._getTextBg = config.getTextBg;
    this._getContainer = config.getContainer;
    this._requestRedraw = config.requestRedraw;
    this._onStrokeFinalized = config.onStrokeFinalized;

    this._inputEl = null;
    this._position = null;
  }

  onPointerDown(e) {
    if (this._inputEl) {
      this._finalizeInput();
    }

    const { width, height } = this._getCanvasSize();
    if (width === 0 || height === 0) return;

    this._position = normalize(e.offsetX, e.offsetY, width, height);
    this._showInput(e.offsetX, e.offsetY);
  }

  onPointerMove(_e) {}

  onPointerUp(_e) {
    return null;
  }

  onCancel() {
    this._removeInput();
    this._position = null;
  }

  renderPreview(_ctx) {}

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

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        this._finalizeInput();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        this._removeInput();
      }
    });

    input.addEventListener('blur', () => {
      if (this._inputEl) {
        this._finalizeInput();
      }
    });

    container.appendChild(input);
    this._inputEl = input;

    requestAnimationFrame(() => {
      if (this._inputEl) {
        this._inputEl.focus();
      }
    });
  }

  _finalizeInput() {
    if (!this._inputEl || !this._position) {
      this._removeInput();
      return;
    }

    const text = this._inputEl.value;
    this._removeInput();

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

    if (this._onStrokeFinalized) {
      this._onStrokeFinalized(stroke);
    }
  }

  _removeInput() {
    if (this._inputEl) {
      if (this._inputEl.parentNode) {
        this._inputEl.parentNode.removeChild(this._inputEl);
      }
      this._inputEl = null;
    }
  }
}

// ─── eraser_tool.js ──────────────────────────────────────────────────────────

const ERASER_TOLERANCE_PX = 5;

class EraserTool {
  constructor({ getStrokes, getCanvas, onErase }) {
    this.name = 'eraser';
    this.cursor = 'not-allowed';

    this._getStrokes = getStrokes;
    this._getCanvas = getCanvas;
    this._onErase = onErase;
  }

  onPointerDown(e) {
    const canvas = this._getCanvas();
    const width = canvas.width;
    const height = canvas.height;

    if (width === 0 || height === 0) return;

    const [clickX, clickY] = normalize(e.offsetX, e.offsetY, width, height);

    const strokes = this._getStrokes();

    const hitStroke = hitTest(clickX, clickY, strokes, ERASER_TOLERANCE_PX, width, height);

    if (hitStroke) {
      this._onErase(hitStroke.id);
    }
  }

  onPointerMove(_e) {}

  onPointerUp(_e) {
    return null;
  }

  onCancel() {}

  renderPreview(_ctx) {}
}

// ─── sticker_picker.js ───────────────────────────────────────────────────────

class StickerPicker {
  constructor({ container, onSelect }) {
    this.container = container;
    this.onSelect = onSelect;

    this.catalog = null;
    this.selectedCategory = null;
    this.selectedSticker = null;

    this._categoriesContainer = container.querySelector('#sticker-picker-categories');
    this._gridContainer = container.querySelector('#sticker-picker-grid');
    this._closeButton = container.querySelector('#sticker-picker-close');

    if (this._closeButton) {
      this._closeButton.addEventListener('click', () => this.hide());
    }
  }

  async show() {
    if (!this.catalog) {
      try {
        const resp = await fetch('/activity/stickers/catalog');
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        this.catalog = await resp.json();
      } catch (_err) {
        this._gridContainer.innerHTML = '';
        this._categoriesContainer.innerHTML = '';
        this._gridContainer.textContent = 'Stickers unavailable';
        this.container.style.display = 'block';
        return;
      }
    }

    if (!this.selectedCategory || !this._findCategory(this.selectedCategory)) {
      const first = this.catalog.categories[0];
      this.selectedCategory = first ? first.slug : null;
    }

    this.renderCategories();
    this.container.style.display = 'block';
  }

  hide() {
    this.container.style.display = 'none';
  }

  renderCategories() {
    this._categoriesContainer.innerHTML = '';

    if (!this.catalog) return;

    for (const category of this.catalog.categories) {
      const btn = document.createElement('button');
      btn.className = 'sticker-category-btn';
      btn.textContent = category.name;
      btn.dataset.slug = category.slug;

      if (category.slug === this.selectedCategory) {
        btn.classList.add('active');
      }

      btn.addEventListener('click', () => {
        this.selectedCategory = category.slug;
        this.renderCategories();
      });

      this._categoriesContainer.appendChild(btn);
    }

    const selected = this._findCategory(this.selectedCategory);
    if (selected) {
      this.renderThumbnails(selected.slug, selected.images);
    }
  }

  renderThumbnails(categorySlug, images) {
    this._gridContainer.innerHTML = '';

    for (const filename of images) {
      const img = document.createElement('img');
      img.src = `/activity/stickers/${categorySlug}/${filename}`;
      img.alt = filename;
      img.className = 'sticker-thumbnail';
      img.style.maxWidth = '64px';
      img.style.maxHeight = '64px';
      img.style.objectFit = 'contain';
      img.style.cursor = 'pointer';

      if (
        this.selectedSticker &&
        this.selectedSticker.category === categorySlug &&
        this.selectedSticker.filename === filename
      ) {
        img.classList.add('selected');
      }

      img.addEventListener('click', () => {
        this.selectedSticker = { category: categorySlug, filename };
        this.onSelect(categorySlug, filename);
        this.renderThumbnails(categorySlug, images);
      });

      this._gridContainer.appendChild(img);
    }
  }

  _findCategory(slug) {
    if (!this.catalog || !slug) return undefined;
    return this.catalog.categories.find((c) => c.slug === slug);
  }
}

// ─── sticker_tool.js ─────────────────────────────────────────────────────────

const STICKER_STROKE_WIDTH_PX = 3;
const STICKER_MIN_SIZE_PX = 5;
const MAX_WIDTH_RATIO = 0.5;
const MAX_HEIGHT_RATIO = 0.5;

class StickerTool {
  constructor({ getCanvas, getColor, stickerPicker }) {
    this.name = 'sticker';
    this.cursor = 'crosshair';

    this._getCanvas = getCanvas;
    this._getColor = getColor;
    this._stickerPicker = stickerPicker;

    this.selectedCategory = null;
    this.selectedFilename = null;

    this.startPoint = null;
    this.currentPoint = null;
    this.drawing = false;

    this.canvasWidth = 0;
    this.canvasHeight = 0;

    this._imageCache = new Map();

    this._stickerPicker.onSelect = (category, filename) => {
      this.onStickerSelected(category, filename);
    };
  }

  onStickerSelected(category, filename) {
    this.selectedCategory = category;
    this.selectedFilename = filename;
  }

  activate() {
    this._stickerPicker.show();
  }

  onPointerDown(e) {
    if (!this.selectedCategory || !this.selectedFilename) return;

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

    let [sx, sy] = this.startPoint;
    let [ex, ey] = this.currentPoint;

    const normWidth = Math.abs(ex - sx);
    const normHeight = Math.abs(ey - sy);

    if (normWidth > MAX_WIDTH_RATIO) {
      const direction = ex >= sx ? 1 : -1;
      ex = sx + direction * MAX_WIDTH_RATIO;
      ex = Math.max(0, Math.min(1, ex));
    }

    if (normHeight > MAX_HEIGHT_RATIO) {
      const direction = ey >= sy ? 1 : -1;
      ey = sy + direction * MAX_HEIGHT_RATIO;
      ey = Math.max(0, Math.min(1, ey));
    }

    const cappedEnd = [
      Math.round(ex * 10000) / 10000,
      Math.round(ey * 10000) / 10000,
    ];

    const [pxStart, pyStart] = denormalize(sx, sy, w, h);
    const [pxEnd, pyEnd] = denormalize(cappedEnd[0], cappedEnd[1], w, h);
    const bboxWidth = Math.abs(pxEnd - pxStart);
    const bboxHeight = Math.abs(pyEnd - pyStart);

    if (bboxWidth <= STICKER_MIN_SIZE_PX || bboxHeight <= STICKER_MIN_SIZE_PX) {
      this._reset();
      return null;
    }

    const stroke = {
      id: crypto.randomUUID(),
      type: 'sticker',
      points: [this.startPoint, cappedEnd],
      color: this._getColor(),
      width: normalizeWidth(STICKER_STROKE_WIDTH_PX, w),
      sticker_category: this.selectedCategory,
      sticker_filename: this.selectedFilename,
    };

    this._reset();
    return stroke;
  }

  onCancel() {
    this._reset();
    this._stickerPicker.hide();
  }

  renderPreview(ctx) {
    if (!this.drawing || !this.startPoint || !this.currentPoint) return;
    if (!this.selectedCategory || !this.selectedFilename) return;

    const w = this.canvasWidth;
    const h = this.canvasHeight;
    if (w === 0 || h === 0) return;

    let [sx, sy] = this.startPoint;
    let [ex, ey] = this.currentPoint;

    const normWidth = Math.abs(ex - sx);
    const normHeight = Math.abs(ey - sy);

    if (normWidth > MAX_WIDTH_RATIO) {
      const direction = ex >= sx ? 1 : -1;
      ex = Math.max(0, Math.min(1, sx + direction * MAX_WIDTH_RATIO));
    }
    if (normHeight > MAX_HEIGHT_RATIO) {
      const direction = ey >= sy ? 1 : -1;
      ey = Math.max(0, Math.min(1, sy + direction * MAX_HEIGHT_RATIO));
    }

    const [x0, y0] = denormalize(sx, sy, w, h);
    const [x1, y1] = denormalize(ex, ey, w, h);

    const rx = Math.min(x0, x1);
    const ry = Math.min(y0, y1);
    const rw = Math.abs(x1 - x0);
    const rh = Math.abs(y1 - y0);

    if (rw === 0 || rh === 0) return;

    ctx.save();

    ctx.strokeStyle = 'rgba(255, 255, 255, 0.6)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.strokeRect(rx, ry, rw, rh);
    ctx.setLineDash([]);

    const img = this._getImage(this.selectedCategory, this.selectedFilename);
    if (img && img.complete && img.naturalWidth > 0) {
      const imgAspect = img.naturalWidth / img.naturalHeight;
      const boxAspect = rw / rh;

      let drawW, drawH, drawX, drawY;
      if (imgAspect > boxAspect) {
        drawW = rw;
        drawH = rw / imgAspect;
        drawX = rx;
        drawY = ry + (rh - drawH) / 2;
      } else {
        drawH = rh;
        drawW = rh * imgAspect;
        drawX = rx + (rw - drawW) / 2;
        drawY = ry;
      }

      ctx.globalAlpha = 0.7;
      ctx.drawImage(img, drawX, drawY, drawW, drawH);
      ctx.globalAlpha = 1.0;
    }

    ctx.restore();
  }

  _getImage(category, filename) {
    const url = `/activity/stickers/${category}/${filename}`;
    if (this._imageCache.has(url)) {
      return this._imageCache.get(url);
    }

    const img = new Image();
    img.src = url;
    this._imageCache.set(url, img);
    return img;
  }

  _reset() {
    this.startPoint = null;
    this.currentPoint = null;
    this.drawing = false;
  }
}

// ─── color_picker.js ─────────────────────────────────────────────────────────

const COLOR_STORAGE_KEY = 'whiteboard-color';
const COLOR_DEFAULT = '#FFFFFF';
const COLOR_HEX_PATTERN = /^#[0-9A-Fa-f]{6}$/;

class ColorPicker {
  constructor({ swatches, customInput }) {
    this.swatches = Array.from(swatches);
    this.customInput = customInput;
    this.currentColor = COLOR_DEFAULT;

    this._init();
  }

  _init() {
    const stored = localStorage.getItem(COLOR_STORAGE_KEY);
    if (stored && COLOR_HEX_PATTERN.test(stored)) {
      this.currentColor = stored.toUpperCase();
    } else {
      this.currentColor = COLOR_DEFAULT;
    }

    this._updateActiveState();

    this.customInput.value = this.currentColor;

    for (const swatch of this.swatches) {
      swatch.addEventListener('click', () => {
        const color = swatch.dataset.color;
        if (color) {
          this._selectColor(color);
        }
      });
    }

    this.customInput.addEventListener('input', () => {
      const color = this.customInput.value;
      if (color) {
        this._selectColor(color.toUpperCase());
      }
    });
  }

  _selectColor(color) {
    this.currentColor = color.toUpperCase();
    this._updateActiveState();
    this.customInput.value = this.currentColor;
    localStorage.setItem(COLOR_STORAGE_KEY, this.currentColor);
  }

  _updateActiveState() {
    for (const swatch of this.swatches) {
      const swatchColor = (swatch.dataset.color || '').toUpperCase();
      if (swatchColor === this.currentColor) {
        swatch.classList.add('active');
      } else {
        swatch.classList.remove('active');
      }
    }
  }

  getColor() {
    return this.currentColor;
  }
}

// ─── undo.js ─────────────────────────────────────────────────────────────────

function initUndo(button, overlay, sendRemove) {
  function updateButtonState() {
    if (overlay.undoStack.length === 0) {
      button.classList.add('disabled');
    } else {
      button.classList.remove('disabled');
    }
  }

  function performUndo() {
    if (overlay.undoStack.length === 0) return;

    const strokeId = overlay.undoStack[overlay.undoStack.length - 1];
    overlay.removeStroke(strokeId);
    sendRemove(strokeId);
    updateButtonState();
  }

  button.addEventListener('click', () => {
    performUndo();
  });

  document.addEventListener('keydown', (e) => {
    if (overlay.mode !== 'active') return;

    const isUndo = (e.ctrlKey || e.metaKey) && e.key === 'z' && !e.shiftKey;
    if (isUndo) {
      e.preventDefault();
      performUndo();
    }
  });

  updateButtonState();

  return { updateButtonState };
}

// ─── reset.js ────────────────────────────────────────────────────────────────

function initReset(button, overlay, sendReset) {
  let armed = false;
  let armedTimer = null;

  button.addEventListener('click', () => {
    if (!armed) {
      // First click — arm the button (show red state for 3 seconds)
      armed = true;
      button.style.background = 'rgba(220, 38, 38, 0.7)';
      button.title = 'Click again to confirm reset';
      armedTimer = setTimeout(() => {
        armed = false;
        button.style.background = '';
        button.title = 'Reset whiteboard';
      }, 3000);
    } else {
      // Second click within 3s — execute reset
      clearTimeout(armedTimer);
      armed = false;
      button.style.background = '';
      button.title = 'Reset whiteboard';
      overlay.clearAll();
      sendReset();
    }
  });
}

// ─── text_bg_toggle.js ───────────────────────────────────────────────────────

const TEXT_BG_STORAGE_KEY = 'hellodj-text-bg';

let _textBgCheckbox = null;

function initTextBgToggle(el) {
  _textBgCheckbox = el || document.getElementById('text-bg-toggle');
  if (!_textBgCheckbox) return;

  const stored = localStorage.getItem(TEXT_BG_STORAGE_KEY);
  if (stored === 'true') {
    _textBgCheckbox.checked = true;
  } else if (stored === 'false') {
    _textBgCheckbox.checked = false;
  }

  _textBgCheckbox.addEventListener('change', () => {
    localStorage.setItem(TEXT_BG_STORAGE_KEY, String(_textBgCheckbox.checked));
  });
}

function getTextBg() {
  if (!_textBgCheckbox) return false;
  return _textBgCheckbox.checked;
}

// ─── canvas_resize.js ────────────────────────────────────────────────────────

function initCanvasResize(canvas, overlay) {
  const parent = canvas.parentElement;

  const observer = new ResizeObserver((entries) => {
    for (const entry of entries) {
      const { width, height } = entry.contentRect;
      if (width === 0 || height === 0) return;
      overlay.resize();
    }
  });

  observer.observe(parent);

  // Initial resize after layout is complete
  requestAnimationFrame(() => {
    overlay.resize();
  });

  return {
    disconnect() {
      observer.disconnect();
    },
  };
}

// ─── controls_passthrough.js ─────────────────────────────────────────────────

class ControlsPassthrough {
  constructor({ canvas, controlsOverlay, bottomControls, showControls }) {
    this.canvas = canvas;
    this.controlsOverlay = controlsOverlay;
    this.bottomControls = bottomControls;
    this.showControls = showControls;
    this._whiteboardActive = false;
    this._inControlsRegion = false;

    this._onPointerMove = this._handlePointerMove.bind(this);
    this._onCanvasPointerDown = this._handleCanvasPointerDown.bind(this);
    this._onCanvasTouchStart = this._handleCanvasTouchStart.bind(this);

    document.addEventListener('pointermove', this._onPointerMove);

    this.canvas.addEventListener('pointerdown', this._onCanvasPointerDown);
    this.canvas.addEventListener('touchstart', this._onCanvasTouchStart, { passive: false });
  }

  setWhiteboardActive(active) {
    this._whiteboardActive = active;
    if (!active) {
      this._inControlsRegion = false;
    }
  }

  _isInControlsRegion(clientY) {
    const rect = this.bottomControls.getBoundingClientRect();
    return clientY >= rect.top - 8;
  }

  _handlePointerMove(e) {
    if (!this._whiteboardActive) return;

    const inRegion = this._isInControlsRegion(e.clientY);

    if (inRegion && !this._inControlsRegion) {
      this._inControlsRegion = true;
      this.canvas.style.pointerEvents = 'none';
      this.showControls();
    } else if (!inRegion && this._inControlsRegion) {
      this._inControlsRegion = false;
      this.canvas.style.pointerEvents = 'auto';
    }
  }

  _handleCanvasPointerDown(e) {
    if (!this._whiteboardActive) return;

    if (this._isInControlsRegion(e.clientY)) {
      e.stopPropagation();
      e.preventDefault();
      this.canvas.style.pointerEvents = 'none';
      this._inControlsRegion = true;
      this.showControls();
    }
  }

  _handleCanvasTouchStart(e) {
    if (!this._whiteboardActive) return;
    if (e.touches.length === 0) return;

    const touch = e.touches[0];
    if (this._isInControlsRegion(touch.clientY)) {
      e.stopPropagation();
      e.preventDefault();
      this.canvas.style.pointerEvents = 'none';
      this._inControlsRegion = true;
      this.showControls();

      setTimeout(() => {
        if (this._whiteboardActive && !this._isInControlsRegion(touch.clientY)) {
          this.canvas.style.pointerEvents = 'auto';
          this._inControlsRegion = false;
        }
      }, 300);
    }
  }

  destroy() {
    document.removeEventListener('pointermove', this._onPointerMove);
    this.canvas.removeEventListener('pointerdown', this._onCanvasPointerDown);
    this.canvas.removeEventListener('touchstart', this._onCanvasTouchStart);
  }
}

// ─── whiteboard.js ───────────────────────────────────────────────────────────

class WhiteboardOverlay {
  constructor({ canvas, hud, toggleButton, localAuthorId }) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.hud = hud;
    this.toggleButton = toggleButton;
    this.localAuthorId = localAuthorId;

    this.strokes = new Map();
    this.mode = 'inactive';
    this.currentTool = null;
    this.currentColor = '#FFFFFF';
    this.currentWidth = 3;
    this.currentOpacity = 1.0;
    this.undoStack = [];

    this.renderer = new StrokeRenderer(
      this.ctx,
      this.canvas.width,
      this.canvas.height,
      () => this.redraw()
    );

    this.hud.style.display = 'none';
    this.canvas.style.pointerEvents = 'none';
    this.toggleButton.dataset.active = 'false';

    this.toggleButton.addEventListener('click', () => {
      if (this.mode === 'inactive') {
        this.activate();
      } else {
        this.deactivate();
      }
    });
  }

  activate() {
    this.mode = 'active';
    this.hud.classList.add('visible');
    this.hud.style.display = '';
    this.canvas.style.pointerEvents = 'auto';
    this.toggleButton.dataset.active = 'true';
  }

  deactivate() {
    this.mode = 'inactive';
    this.hud.classList.remove('visible');
    this.hud.style.display = '';
    this.canvas.style.pointerEvents = 'none';
    this.toggleButton.dataset.active = 'false';
  }

  addStroke(stroke) {
    this.strokes.set(stroke.id, stroke);
    if (stroke.author === this.localAuthorId) {
      this.undoStack.push(stroke.id);
    }
    this.redraw();
  }

  removeStroke(strokeId) {
    this.strokes.delete(strokeId);
    const undoIdx = this.undoStack.indexOf(strokeId);
    if (undoIdx !== -1) {
      this.undoStack.splice(undoIdx, 1);
    }
    this.redraw();
  }

  clearAll() {
    this.strokes.clear();
    this.undoStack.length = 0;
    this.redraw();
  }

  redraw() {
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    for (const stroke of this.strokes.values()) {
      this.renderer.renderStroke(stroke);
    }
  }

  resize() {
    const rect = this.canvas.parentElement.getBoundingClientRect();
    const width = Math.floor(rect.width);
    const height = Math.floor(rect.height);

    if (width === 0 || height === 0) return;

    this.canvas.width = width;
    this.canvas.height = height;
    this.renderer.resize(width, height);
    this.redraw();
  }
}

// ─── ws_whiteboard.js ────────────────────────────────────────────────────────

function initWhiteboardSync(wsSend, overlay) {
  function sendStrokeAdd(stroke) {
    wsSend({
      type: 'stroke_add',
      id: stroke.id,
      stroke_type: stroke.type,
      points: stroke.points,
      color: stroke.color,
      width: stroke.width,
      opacity: stroke.opacity,
      author: overlay.localAuthorId,
      ...(stroke.text != null && { text: stroke.text }),
      ...(stroke.text_bg != null && { text_bg: stroke.text_bg }),
      ...(stroke.sticker_category != null && { sticker_category: stroke.sticker_category }),
      ...(stroke.sticker_filename != null && { sticker_filename: stroke.sticker_filename }),
    });
  }

  function sendStrokeRemove(strokeId) {
    wsSend({
      type: 'stroke_remove',
      id: strokeId,
    });
  }

  function sendWhiteboardReset() {
    wsSend({
      type: 'whiteboard_reset',
    });
  }

  function handleMessage(data) {
    switch (data.type) {
      case 'stroke_add':
        _handleStrokeAdd(data);
        return true;

      case 'stroke_remove':
        _handleStrokeRemove(data);
        return true;

      case 'whiteboard_reset':
        _handleWhiteboardReset();
        return true;

      case 'whiteboard_clear':
        _handleWhiteboardClear();
        return true;

      case 'state':
        _handleState(data);
        return false;

      case 'error':
        _handleError(data);
        return true;

      default:
        return false;
    }
  }

  function _handleStrokeAdd(data) {
    const stroke = {
      id: data.id,
      type: data.stroke_type,
      points: data.points,
      color: data.color,
      width: data.width,
      opacity: data.opacity,
      author: data.author,
    };

    if (data.text != null) stroke.text = data.text;
    if (data.text_bg != null) stroke.text_bg = data.text_bg;

    if (data.sticker_category != null) stroke.sticker_category = data.sticker_category;
    if (data.sticker_filename != null) stroke.sticker_filename = data.sticker_filename;

    overlay.addStroke(stroke);
  }

  function _handleStrokeRemove(data) {
    if (data.id) {
      overlay.removeStroke(data.id);
    }
  }

  function _handleWhiteboardReset() {
    overlay.clearAll();
  }

  function _handleWhiteboardClear() {
    overlay.clearAll();
    overlay.deactivate();
  }

  function _handleState(data) {
    const strokes = data.strokes;
    if (!Array.isArray(strokes)) return;

    overlay.strokes.clear();
    overlay.undoStack.length = 0;

    for (const s of strokes) {
      const stroke = {
        id: s.id,
        type: s.type,
        points: s.points,
        color: s.color,
        width: s.width,
        opacity: s.opacity,
        author: s.author,
      };

      if (s.text != null) stroke.text = s.text;
      if (s.text_bg != null) stroke.text_bg = s.text_bg;

      if (s.sticker_category != null) stroke.sticker_category = s.sticker_category;
      if (s.sticker_filename != null) stroke.sticker_filename = s.sticker_filename;

      overlay.strokes.set(stroke.id, stroke);
    }

    restoreUndoHistory(overlay, strokes);

    overlay.redraw();
  }

  function _handleError(data) {
    if (data.message) {
      console.warn('[Whiteboard] Server error:', data.message);
    }
  }

  return {
    handleMessage,
    sendStrokeAdd,
    sendStrokeRemove,
    sendWhiteboardReset,
  };
}

// ─── Expose public API ───────────────────────────────────────────────────────

window.WhiteboardBundle = {
  WhiteboardOverlay,
  ToolManager,
  PenTool,
  LineTool,
  ShapeTool,
  TextTool,
  EraserTool,
  StickerTool,
  StickerPicker,
  ColorPicker,
  initUndo,
  initReset,
  initTextBgToggle,
  getTextBg,
  initCanvasResize,
  ControlsPassthrough,
  initWhiteboardSync,
};

console.log('[HelloDJ] WhiteboardBundle loaded, keys:', Object.keys(window.WhiteboardBundle).length);

})();
