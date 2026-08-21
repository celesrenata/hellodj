/**
 * EraserTool — removes strokes by clicking on them.
 *
 * On click, hit-tests against all strokes (topmost first) with a 5px
 * tolerance from the path centerline (normalized to viewport). If a stroke
 * is hit, invokes the onErase callback with the stroke ID to remove it
 * locally and send a stroke_remove via WebSocket.
 *
 * Implements the DrawingTool interface (see tools.js).
 */

import { hitTest } from './hittest.js';
import { normalize } from './coords.js';

/** Tolerance in CSS pixels from stroke centerline for hit detection */
const TOLERANCE_PX = 5;

export class EraserTool {
  /**
   * @param {object} options
   * @param {() => Array<object>} options.getStrokes - Returns all strokes for hit-testing (insertion order)
   * @param {() => HTMLCanvasElement} options.getCanvas - Returns the whiteboard canvas element
   * @param {(strokeId: string) => void} options.onErase - Callback to remove a stroke and send WS message
   */
  constructor({ getStrokes, getCanvas, onErase }) {
    /** @type {string} */
    this.name = 'eraser';
    /** @type {string} CSS cursor value — distinct eraser cursor */
    this.cursor = 'not-allowed';

    /** @type {() => Array<object>} */
    this._getStrokes = getStrokes;
    /** @type {() => HTMLCanvasElement} */
    this._getCanvas = getCanvas;
    /** @type {(strokeId: string) => void} */
    this._onErase = onErase;
  }

  /**
   * Handle pointer-down: normalize click position, hit-test against all
   * strokes with 5px tolerance, and erase the topmost hit stroke.
   *
   * @param {PointerEvent} e
   */
  onPointerDown(e) {
    const canvas = this._getCanvas();
    const width = canvas.width;
    const height = canvas.height;

    if (width === 0 || height === 0) return;

    // Normalize the click position to 0.0–1.0 viewport coordinates
    const [clickX, clickY] = normalize(e.offsetX, e.offsetY, width, height);

    // Get all strokes in insertion order for hit-testing
    const strokes = this._getStrokes();

    // Hit-test with 5px tolerance (topmost stroke returned first)
    const hitStroke = hitTest(clickX, clickY, strokes, TOLERANCE_PX, width, height);

    if (hitStroke) {
      this._onErase(hitStroke.id);
    }
  }

  /**
   * Handle pointer-move: no-op for eraser.
   * @param {PointerEvent} _e
   */
  onPointerMove(_e) {
    // Eraser does not track movement
  }

  /**
   * Handle pointer-up: eraser doesn't produce strokes.
   * @param {PointerEvent} _e
   * @returns {null}
   */
  onPointerUp(_e) {
    return null;
  }

  /**
   * Cancel any in-progress operation: no-op for eraser.
   */
  onCancel() {
    // Nothing to cancel
  }

  /**
   * Render preview: eraser has no preview.
   * @param {CanvasRenderingContext2D} _ctx
   */
  renderPreview(_ctx) {
    // No preview for eraser tool
  }
}
