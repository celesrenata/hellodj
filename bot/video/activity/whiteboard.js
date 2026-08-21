/**
 * WhiteboardOverlay — manages the drawing canvas surface and stroke state.
 *
 * Responsibilities:
 * - Mode toggle (active/inactive) controlling HUD visibility and pointer-events
 * - Stroke storage (Map preserving insertion order)
 * - Undo stack per local author
 * - Full re-render delegation to StrokeRenderer
 * - Canvas resize handling
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

    // Set initial state: inactive, HUD hidden, pointer-events disabled
    this.hud.style.display = 'none';
    this.canvas.style.pointerEvents = 'none';
    this.toggleButton.dataset.active = 'false';

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
    this.redraw();
  }

  /**
   * Clear all strokes and the undo stack.
   * Triggers a full redraw (clears the canvas).
   */
  clearAll() {
    this.strokes.clear();
    this.undoStack.length = 0;
    this.redraw();
  }

  /**
   * Full re-render: clear canvas, iterate strokes in insertion order,
   * render each via StrokeRenderer.
   */
  redraw() {
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    for (const stroke of this.strokes.values()) {
      this.renderer.renderStroke(stroke);
    }
  }

  /**
   * Recalculate canvas dimensions to match its container,
   * update StrokeRenderer dimensions, and redraw.
   */
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
