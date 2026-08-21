/**
 * ToolManager — manages drawing tool registration, selection, and cursor state.
 *
 * DrawingTool interface (implemented by PenTool, LineTool, ShapeTool, etc.):
 *   name: string               — unique tool identifier
 *   cursor: string             — CSS cursor value applied to canvas
 *   onPointerDown(e): void     — handle pointer-down event
 *   onPointerMove(e): void     — handle pointer-move event
 *   onPointerUp(e): Stroke|null — handle pointer-up, return finalized stroke or null
 *   onCancel(): void           — cleanup on tool switch or escape
 *   renderPreview(ctx): void   — render in-progress preview on the canvas context
 */

export class ToolManager {
  /**
   * @param {HTMLCanvasElement} canvas — canvas element for cursor updates
   */
  constructor(canvas) {
    /** @type {HTMLCanvasElement} */
    this.canvas = canvas;
    /** @type {Map<string, DrawingTool>} */
    this.tools = new Map();
    /** @type {DrawingTool|null} */
    this.activeTool = null;
  }

  /**
   * Register a tool instance. The tool is keyed by its `.name` property.
   * @param {DrawingTool} tool
   */
  registerTool(tool) {
    this.tools.set(tool.name, tool);
  }

  /**
   * Select a tool by name. Cancels the previous active tool, activates
   * the new tool (if it has an activate method), and updates the canvas
   * cursor to the new tool's cursor value.
   * @param {string} name
   */
  selectTool(name) {
    const tool = this.tools.get(name);
    if (!tool) return;

    if (this.activeTool && this.activeTool !== tool) {
      this.activeTool.onCancel();
    }

    this.activeTool = tool;
    this.canvas.style.cursor = tool.cursor;

    // Activate the new tool if it supports activation (e.g. StickerTool shows picker)
    if (typeof tool.activate === 'function') {
      tool.activate();
    }
  }

  /**
   * Return the currently active tool.
   * @returns {DrawingTool|null}
   */
  getActiveTool() {
    return this.activeTool;
  }
}
