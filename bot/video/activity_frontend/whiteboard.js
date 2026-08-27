/**
 * WhiteboardOverlay — manages the drawing canvas surface and stroke state.
 *
 * Responsibilities:
 * - Mode toggle (active/inactive) controlling HUD visibility and pointer-events
 * - Stroke storage (Map preserving insertion order)
 * - Undo stack per local author
 * - Full re-render delegation to StrokeRenderer
 * - Canvas resize handling (anchored to video content rect)
 * - Off-screen stroke indicators (blinking arrows at canvas edges)
 */

import { StrokeRenderer } from './renderer.js';

export class WhiteboardOverlay {
  /**
   * @param {object} options
   * @param {HTMLCanvasElement} options.canvas - The whiteboard canvas element
   * @param {HTMLElement} options.hud - The whiteboard HUD toolbar element
   * @param {HTMLElement} options.toggleButton - The whiteboard toggle button in controls
   * @param {string} options.localAuthorId - This viewer's unique ID (Discord user_id)
   */
  constructor({ canvas, hud, toggleButton, localAuthorId }) {
    /** @type {HTMLCanvasElement} */
    this.canvas = canvas;
    /** @type {CanvasRenderingContext2D} */
    this.ctx = canvas.getContext('2d');
    /** @type {HTMLElement} */
    this.hud = hud;
    /** @type {HTMLElement} */
    this.toggleButton = toggleButton;
    /** @type {string} */
    this.localAuthorId = localAuthorId;

    /** @type {Map<string, object>} stroke_id → Stroke (ordered by insertion) */
    this.strokes = new Map();
    /** @type {'active' | 'inactive'} */
    this.mode = 'inactive';
    /** @type {string|null} */
    this.currentTool = null;
    /** @type {string} hex color e.g. '#FF0000' */
    this.currentColor = '#FFFFFF';
    /** @type {number} stroke width in CSS pixels (1–20) */
    this.currentWidth = 3;
    /** @type {number} stroke opacity (0.1–1.0) */
    this.currentOpacity = 1.0;
    /** @type {string[]} stroke IDs authored by this viewer (most recent last) */
    this.undoStack = [];

    /** @type {StrokeRenderer} */
    this.renderer = new StrokeRenderer(
      this.ctx,
      this.canvas.width,
      this.canvas.height,
      () => this.redraw()
    );

    // Wire the animation loop's redraw to our redraw method
    this.renderer.setFullRedraw(() => this.redraw());

    // Set initial state: inactive, HUD hidden, pointer-events disabled
    this.hud.style.display = 'none';
    this.canvas.style.pointerEvents = 'none';
    this.toggleButton.dataset.active = 'false';

    // Off-screen indicator state
    /** @type {{ top: boolean, bottom: boolean, left: boolean, right: boolean }} */
    this._offscreenDirs = { top: false, bottom: false, left: false, right: false };
    /** @type {number|null} */
    this._indicatorAnimFrame = null;
    /** @type {boolean} */
    this._indicatorBlinkOn = true;
    /** @type {number} */
    this._indicatorBlinkStart = 0;

    // Wire toggle button click
    this.toggleButton.addEventListener('click', () => {
      if (this.mode === 'inactive') {
        this.activate();
      } else {
        this.deactivate();
      }
    });
  }

  /**
   * Enable drawing mode.
   * Shows HUD, enables pointer-events on canvas, updates toggle button state.
   */
  activate() {
    this.mode = 'active';
    this.hud.classList.add('visible');
    this.hud.style.display = '';
    this.canvas.style.pointerEvents = 'auto';
    this.toggleButton.dataset.active = 'true';
  }

  /**
   * Disable drawing mode (keep rendering strokes read-only).
   * Hides HUD, disables pointer-events on canvas, updates toggle button state.
   */
  deactivate() {
    this.mode = 'inactive';
    this.hud.classList.remove('visible');
    this.hud.style.display = '';
    this.canvas.style.pointerEvents = 'none';
    this.toggleButton.dataset.active = 'false';
  }

  /**
   * Add a stroke to the map.
   * If the stroke was authored by the local viewer, push to undo stack.
   * Triggers a full redraw.
   *
   * @param {object} stroke - Stroke data with at least { id, author, ... }
   */
  addStroke(stroke) {
    this.strokes.set(stroke.id, stroke);
    if (stroke.author === this.localAuthorId) {
      this.undoStack.push(stroke.id);
    }
    this.renderer.updateAnimationState(this.strokes);
    this.redraw();
  }

  /**
   * Remove a stroke by ID.
   * Also removes from undo stack if present.
   * Triggers a full redraw.
   *
   * @param {string} strokeId
   */
  removeStroke(strokeId) {
    this.strokes.delete(strokeId);
    const undoIdx = this.undoStack.indexOf(strokeId);
    if (undoIdx !== -1) {
      this.undoStack.splice(undoIdx, 1);
    }
    this.renderer.updateAnimationState(this.strokes);
    this.redraw();
  }

  /**
   * Clear all strokes and the undo stack.
   * Triggers a full redraw (clears the canvas).
   */
  clearAll() {
    this.strokes.clear();
    this.undoStack.length = 0;
    this.renderer.updateAnimationState(this.strokes);
    this.redraw();
  }

  /**
   * Full re-render: clear canvas, iterate strokes in insertion order,
   * render each via StrokeRenderer, then draw off-screen indicators.
   */
  redraw() {
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    for (const stroke of this.strokes.values()) {
      this.renderer.renderStroke(stroke);
    }
    this._drawOffscreenIndicators();
  }

  /**
   * Recalculate canvas dimensions to match the video's rendered content rect,
   * update StrokeRenderer dimensions, and redraw.
   *
   * @param {{ x: number, y: number, width: number, height: number }} [videoRect]
   *   If provided, positions and sizes the canvas to match the video content area.
   *   If omitted, falls back to parent element dimensions (legacy behavior).
   */
  resize(videoRect) {
    let width, height, offsetX, offsetY;

    if (videoRect) {
      width = videoRect.width;
      height = videoRect.height;
      offsetX = videoRect.x;
      offsetY = videoRect.y;
    } else {
      const rect = this.canvas.parentElement.getBoundingClientRect();
      width = Math.floor(rect.width);
      height = Math.floor(rect.height);
      offsetX = 0;
      offsetY = 0;
    }

    if (width === 0 || height === 0) return;

    // Position the canvas over the video content area (not the full container)
    this.canvas.style.left = `${offsetX}px`;
    this.canvas.style.top = `${offsetY}px`;
    this.canvas.style.width = `${width}px`;
    this.canvas.style.height = `${height}px`;
    this.canvas.width = width;
    this.canvas.height = height;

    this.renderer.resize(width, height);
    this._computeOffscreenDirections();
    this.redraw();
  }

  // ─── Off-screen Stroke Indicators ──────────────────────────────────────

  /**
   * Scan all strokes and determine which edges have content beyond [0,1] bounds.
   * Since coords are normalized 0-1, off-screen means coords < 0 or > 1
   * (which can happen if strokes were recorded at different canvas geometries).
   *
   * For the common case: we check if any stroke has points that would render
   * visually outside the current canvas bounds. With the video-anchored canvas,
   * this detects strokes that were drawn when the canvas had a different aspect.
   */
  _computeOffscreenDirections() {
    const dirs = { top: false, bottom: false, left: false, right: false };

    for (const stroke of this.strokes.values()) {
      if (!stroke.points || stroke.points.length === 0) continue;
      for (const pt of stroke.points) {
        const [nx, ny] = pt;
        if (nx < -0.01) dirs.left = true;
        if (nx > 1.01) dirs.right = true;
        if (ny < -0.01) dirs.top = true;
        if (ny > 1.01) dirs.bottom = true;
      }
    }

    const changed = (
      dirs.top !== this._offscreenDirs.top ||
      dirs.bottom !== this._offscreenDirs.bottom ||
      dirs.left !== this._offscreenDirs.left ||
      dirs.right !== this._offscreenDirs.right
    );

    this._offscreenDirs = dirs;

    const hasAny = dirs.top || dirs.bottom || dirs.left || dirs.right;

    if (hasAny && !this._indicatorAnimFrame) {
      this._indicatorBlinkStart = performance.now();
      this._startIndicatorBlink();
    } else if (!hasAny && this._indicatorAnimFrame) {
      this._stopIndicatorBlink();
    }
  }

  /**
   * Draw small blinking arrows at the edges of the canvas pointing toward
   * off-screen stroke content.
   */
  _drawOffscreenIndicators() {
    const dirs = this._offscreenDirs;
    if (!dirs.top && !dirs.bottom && !dirs.left && !dirs.right) return;
    if (!this._indicatorBlinkOn) return;

    const ctx = this.ctx;
    const w = this.canvas.width;
    const h = this.canvas.height;
    const arrowSize = 8;
    const margin = 12;

    ctx.save();
    ctx.fillStyle = 'rgba(255, 255, 255, 0.85)';
    ctx.shadowColor = 'rgba(88, 101, 242, 0.8)';
    ctx.shadowBlur = 6;

    // Top arrow (↑)
    if (dirs.top) {
      const cx = w / 2;
      const cy = margin;
      ctx.beginPath();
      ctx.moveTo(cx, cy - arrowSize);
      ctx.lineTo(cx - arrowSize, cy + arrowSize);
      ctx.lineTo(cx + arrowSize, cy + arrowSize);
      ctx.closePath();
      ctx.fill();
    }

    // Bottom arrow (↓)
    if (dirs.bottom) {
      const cx = w / 2;
      const cy = h - margin;
      ctx.beginPath();
      ctx.moveTo(cx, cy + arrowSize);
      ctx.lineTo(cx - arrowSize, cy - arrowSize);
      ctx.lineTo(cx + arrowSize, cy - arrowSize);
      ctx.closePath();
      ctx.fill();
    }

    // Left arrow (←)
    if (dirs.left) {
      const cx = margin;
      const cy = h / 2;
      ctx.beginPath();
      ctx.moveTo(cx - arrowSize, cy);
      ctx.lineTo(cx + arrowSize, cy - arrowSize);
      ctx.lineTo(cx + arrowSize, cy + arrowSize);
      ctx.closePath();
      ctx.fill();
    }

    // Right arrow (→)
    if (dirs.right) {
      const cx = w - margin;
      const cy = h / 2;
      ctx.beginPath();
      ctx.moveTo(cx + arrowSize, cy);
      ctx.lineTo(cx - arrowSize, cy - arrowSize);
      ctx.lineTo(cx - arrowSize, cy + arrowSize);
      ctx.closePath();
      ctx.fill();
    }

    ctx.restore();
  }

  /**
   * Start the blink animation for off-screen indicators (toggles every 800ms).
   */
  _startIndicatorBlink() {
    if (this._indicatorAnimFrame) return;

    const tick = () => {
      const elapsed = performance.now() - this._indicatorBlinkStart;
      const newBlink = Math.floor(elapsed / 800) % 2 === 0;
      if (newBlink !== this._indicatorBlinkOn) {
        this._indicatorBlinkOn = newBlink;
        this.redraw();
      }
      this._indicatorAnimFrame = requestAnimationFrame(tick);
    };

    this._indicatorAnimFrame = requestAnimationFrame(tick);
  }

  /**
   * Stop the blink animation.
   */
  _stopIndicatorBlink() {
    if (this._indicatorAnimFrame) {
      cancelAnimationFrame(this._indicatorAnimFrame);
      this._indicatorAnimFrame = null;
    }
    this._indicatorBlinkOn = true;
  }
}
