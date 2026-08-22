/**
 * StrokeRenderer — renders whiteboard strokes on a canvas context.
 *
 * Uses denormalize/denormalizeWidth from coords.js to convert normalized
 * stroke coordinates back to pixel positions for the current canvas size.
 *
 * Rendering rules:
 * - Freehand: quadratic Bézier interpolation (midpoint algorithm)
 * - Line: simple lineTo between 2 points
 * - Arrow: line + arrowhead at endpoint (proportional to stroke width)
 * - Rect: strokeRect outline only
 * - Ellipse: ctx.ellipse outline only
 * - Text: fillText with color, 16px font. Optional black 50% opacity bg + 4px padding
 * - Sticker: letterbox-fit image within bounding box, cached with onload redraw
 */

import { denormalize, denormalizeWidth } from './coords.js';

export class StrokeRenderer {
  /**
   * @param {CanvasRenderingContext2D} ctx
   * @param {number} width  - Canvas pixel width
   * @param {number} height - Canvas pixel height
   * @param {function} [redrawCallback] - Called when a cached image loads to trigger full redraw
   */
  constructor(ctx, width, height, redrawCallback) {
    this.ctx = ctx;
    this.width = width;
    this.height = height;
    this.redrawCallback = redrawCallback || null;
    /** @type {Map<string, HTMLImageElement>} */
    this.imageCache = new Map();
  }

  /**
   * Update canvas dimensions (call on resize).
   * @param {number} width
   * @param {number} height
   */
  resize(width, height) {
    this.width = width;
    this.height = height;
  }

  /**
   * Render a single stroke on the canvas.
   * @param {object} stroke
   */
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

  /**
   * Freehand: quadratic Bézier interpolation for smooth curves.
   *
   * For points P0, P1, ..., Pn:
   *   - Move to P0
   *   - For each pair (Pi, Pi+1) where i < n-1:
   *       control point = Pi, end point = midpoint(Pi, Pi+1)
   *   - Final segment: quadraticCurveTo(Pn-1, Pn)
   *
   * @param {Array<[number, number]>} points
   * @param {number} w
   * @param {number} h
   */
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
      // Final segment to the last point
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

  /**
   * Line: simple lineTo between 2 points.
   * @param {Array<[number, number]>} points
   * @param {number} w
   * @param {number} h
   */
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

  /**
   * Arrow: line + arrowhead at endpoint.
   * Arrowhead size is proportional to stroke width.
   * @param {Array<[number, number]>} points
   * @param {number} w
   * @param {number} h
   * @param {number} lineWidth - Denormalized stroke width in pixels
   */
  _renderArrow(points, w, h, lineWidth) {
    if (points.length < 2) return;

    const ctx = this.ctx;
    const [x0, y0] = denormalize(points[0][0], points[0][1], w, h);
    const [x1, y1] = denormalize(points[1][0], points[1][1], w, h);

    // Draw the line
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();

    // Draw arrowhead
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

  /**
   * Rect: outline only (strokeRect), not filled.
   * Points are [topLeft, bottomRight] of bounding box.
   * @param {Array<[number, number]>} points
   * @param {number} w
   * @param {number} h
   */
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

  /**
   * Ellipse: outline only, using ctx.ellipse().
   * Points are [topLeft, bottomRight] of bounding box.
   * @param {Array<[number, number]>} points
   * @param {number} w
   * @param {number} h
   */
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

  /**
   * Text: fillText with selected color, font size 16px.
   * If textBg is enabled: fill a rect behind with rgba(0,0,0,0.5) and 4px padding.
   * @param {object} stroke
   * @param {number} w
   * @param {number} h
   */
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

  /**
   * Sticker: load image from stickers/{category}/{filename},
   * draw with letterbox fit in bounding box preserving aspect ratio.
   * Uses image cache. On image load, triggers full redraw.
   * @param {object} stroke
   * @param {number} w
   * @param {number} h
   */
  _renderSticker(stroke, w, h) {
    if (!stroke.sticker_category || !stroke.sticker_filename) return;
    if (!stroke.points || stroke.points.length < 2) return;

    const [x1, y1] = denormalize(stroke.points[0][0], stroke.points[0][1], w, h);
    const [x2, y2] = denormalize(stroke.points[1][0], stroke.points[1][1], w, h);
    const boxW = Math.abs(x2 - x1);
    const boxH = Math.abs(y2 - y1);
    const boxX = Math.min(x1, x2);
    const boxY = Math.min(y1, y2);

    const url = `stickers/${stroke.sticker_category}/${stroke.sticker_filename}`;
    const img = this._getOrLoadImage(url);

    if (img && img.complete && img.naturalWidth > 0) {
      // Letterbox fit: preserve aspect ratio within bounding box
      const imgAspect = img.naturalWidth / img.naturalHeight;
      const boxAspect = boxW / boxH;
      let drawW, drawH, drawX, drawY;

      if (imgAspect > boxAspect) {
        // Image wider than box — fit to width
        drawW = boxW;
        drawH = boxW / imgAspect;
        drawX = boxX;
        drawY = boxY + (boxH - drawH) / 2;
      } else {
        // Image taller than box — fit to height
        drawH = boxH;
        drawW = boxH * imgAspect;
        drawX = boxX + (boxW - drawW) / 2;
        drawY = boxY;
      }

      this.ctx.drawImage(img, drawX, drawY, drawW, drawH);
    }
    // If image not loaded yet, the onload handler will trigger a redraw
  }

  /**
   * Get a cached image or create and cache a new one.
   * On load, triggers the redraw callback so the sticker appears once fetched.
   * @param {string} url
   * @returns {HTMLImageElement}
   */
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
