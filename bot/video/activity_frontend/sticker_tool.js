/**
 * StickerTool — places sticker images on the whiteboard overlay.
 *
 * Uses the same drag-to-place pattern as ShapeTool (pointerdown → pointermove → pointerup).
 * On activation the Sticker_Picker panel is shown; on deactivation it's hidden.
 * A sticker must be selected in the picker before placement (pointer events are no-ops
 * until a selection is made). The bounding box is capped at 50% of the overlay in each
 * dimension and must exceed 5px in both dims to finalize.
 *
 * All coordinates are normalized to 0.0–1.0 using the coords module.
 */

import { normalize, denormalize, normalizeWidth } from './coords.js';
import { stickerImageUrl } from './sticker_picker.js';

const STROKE_WIDTH_PX = 3;
const MIN_SIZE_PX = 5;
const MAX_WIDTH_RATIO = 0.5;
const MAX_HEIGHT_RATIO = 0.5;

export class StickerTool {
  /**
   * @param {object} options
   * @param {() => HTMLCanvasElement} options.getCanvas - Returns the whiteboard canvas element
   * @param {() => string} options.getColor - Returns the current stroke color
   * @param {import('./sticker_picker.js').StickerPicker} options.stickerPicker - StickerPicker instance
   */
  constructor({ getCanvas, getColor, stickerPicker }) {
    /** @type {string} */
    this.name = 'sticker';
    /** @type {string} */
    this.cursor = 'crosshair';

    /** @type {() => HTMLCanvasElement} */
    this._getCanvas = getCanvas;
    /** @type {() => string} */
    this._getColor = getColor;
    /** @type {import('./sticker_picker.js').StickerPicker} */
    this._stickerPicker = stickerPicker;

    /** @type {string|null} */
    this.selectedCategory = null;
    /** @type {string|null} */
    this.selectedFilename = null;

    /** @type {[number, number]|null} Normalized start point */
    this.startPoint = null;
    /** @type {[number, number]|null} Normalized current point */
    this.currentPoint = null;
    /** @type {boolean} */
    this.drawing = false;

    /** @type {number} Canvas width in CSS pixels */
    this.canvasWidth = 0;
    /** @type {number} Canvas height in CSS pixels */
    this.canvasHeight = 0;

    /** @type {Map<string, HTMLImageElement>} Image cache keyed by URL */
    this._imageCache = new Map();

    // Wire up the sticker picker selection callback
    this._stickerPicker.onSelect = (category, filename) => {
      this.onStickerSelected(category, filename);
    };
  }

  /**
   * Called by StickerPicker when a sticker is selected.
   * @param {string} category
   * @param {string} filename
   */
  onStickerSelected(category, filename) {
    this.selectedCategory = category;
    this.selectedFilename = filename;
  }

  /**
   * Called when this tool becomes the active tool.
   * Shows the Sticker_Picker panel.
   */
  activate() {
    this._stickerPicker.show();
  }

  /**
   * Handle pointer down — record start point if a sticker is selected.
   * @param {PointerEvent} e
   */
  onPointerDown(e) {
    // No-op if no sticker selected
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

  /**
   * Handle pointer move — update current point for preview.
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
   * Handle pointer up — finalize sticker placement if bbox > 5px in both dims.
   * Caps bounding box at 50% overlay width and 50% overlay height.
   * @param {PointerEvent} e
   * @returns {object|null} Finalized stroke or null if too small or no sticker selected
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

    // Cap bounding box at MAX_WIDTH_RATIO / MAX_HEIGHT_RATIO of overlay
    let [sx, sy] = this.startPoint;
    let [ex, ey] = this.currentPoint;

    const normWidth = Math.abs(ex - sx);
    const normHeight = Math.abs(ey - sy);

    // Cap width
    if (normWidth > MAX_WIDTH_RATIO) {
      const direction = ex >= sx ? 1 : -1;
      ex = sx + direction * MAX_WIDTH_RATIO;
      // Clamp to [0, 1]
      ex = Math.max(0, Math.min(1, ex));
    }

    // Cap height
    if (normHeight > MAX_HEIGHT_RATIO) {
      const direction = ey >= sy ? 1 : -1;
      ey = sy + direction * MAX_HEIGHT_RATIO;
      // Clamp to [0, 1]
      ey = Math.max(0, Math.min(1, ey));
    }

    const cappedEnd = [
      Math.round(ex * 10000) / 10000,
      Math.round(ey * 10000) / 10000,
    ];

    // Denormalize to check pixel dimensions of bounding box
    const [pxStart, pyStart] = denormalize(sx, sy, w, h);
    const [pxEnd, pyEnd] = denormalize(cappedEnd[0], cappedEnd[1], w, h);
    const bboxWidth = Math.abs(pxEnd - pxStart);
    const bboxHeight = Math.abs(pyEnd - pyStart);

    if (bboxWidth <= MIN_SIZE_PX || bboxHeight <= MIN_SIZE_PX) {
      this._reset();
      return null;
    }

    const stroke = {
      id: crypto.randomUUID(),
      type: 'sticker',
      points: [this.startPoint, cappedEnd],
      color: this._getColor(),
      width: normalizeWidth(STROKE_WIDTH_PX, w),
      sticker_category: this.selectedCategory,
      sticker_filename: this.selectedFilename,
    };

    this._reset();
    return stroke;
  }

  /**
   * Cancel current operation and hide the Sticker_Picker (tool deactivation).
   */
  onCancel() {
    this._reset();
    this._stickerPicker.hide();
  }

  /**
   * Render a live preview of the sticker being placed within the current bounding box.
   * @param {CanvasRenderingContext2D} ctx
   */
  renderPreview(ctx) {
    if (!this.drawing || !this.startPoint || !this.currentPoint) return;
    if (!this.selectedCategory || !this.selectedFilename) return;

    const w = this.canvasWidth;
    const h = this.canvasHeight;
    if (w === 0 || h === 0) return;

    // Calculate capped bounding box for preview
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

    // Draw bounding box outline (dashed)
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.6)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.strokeRect(rx, ry, rw, rh);
    ctx.setLineDash([]);

    // Draw the sticker image within the bounding box
    const img = this._getImage(this.selectedCategory, this.selectedFilename);
    if (img && img.complete && img.naturalWidth > 0) {
      // Preserve aspect ratio (letterbox fit)
      const imgAspect = img.naturalWidth / img.naturalHeight;
      const boxAspect = rw / rh;

      let drawW, drawH, drawX, drawY;
      if (imgAspect > boxAspect) {
        // Image is wider than box — fit to width
        drawW = rw;
        drawH = rw / imgAspect;
        drawX = rx;
        drawY = ry + (rh - drawH) / 2;
      } else {
        // Image is taller than box — fit to height
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

  /**
   * Get or load a sticker image from cache.
   * @param {string} category
   * @param {string} filename
   * @returns {HTMLImageElement|null}
   */
  _getImage(category, filename) {
    const url = stickerImageUrl(category, filename);
    if (this._imageCache.has(url)) {
      return this._imageCache.get(url);
    }

    const img = new Image();
    img.src = url;
    this._imageCache.set(url, img);
    return img;
  }

  /**
   * Reset internal drawing state (does not hide picker).
   */
  _reset() {
    this.startPoint = null;
    this.currentPoint = null;
    this.drawing = false;
  }
}
