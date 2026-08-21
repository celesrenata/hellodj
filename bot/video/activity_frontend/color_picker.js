/**
 * ColorPicker — manages color selection from the Whiteboard HUD.
 *
 * Responsibilities:
 * - 8 preset color swatches (white, red, orange, yellow, green, cyan, blue, purple)
 * - Custom color input via native browser color picker
 * - Visual highlight (.active class) on the active swatch
 * - Persist selected color in localStorage key 'whiteboard-color'
 * - Default to #FFFFFF if no stored color or stored value invalid
 * - Expose getColor() for use by drawing tools
 */

const STORAGE_KEY = 'whiteboard-color';
const DEFAULT_COLOR = '#FFFFFF';
const HEX_PATTERN = /^#[0-9A-Fa-f]{6}$/;

export class ColorPicker {
  /**
   * @param {object} options
   * @param {NodeList|HTMLElement[]} options.swatches - The preset color swatch buttons (.color-swatch)
   * @param {HTMLInputElement} options.customInput - The <input type="color"> element (.color-custom)
   */
  constructor({ swatches, customInput }) {
    /** @type {HTMLElement[]} */
    this.swatches = Array.from(swatches);
    /** @type {HTMLInputElement} */
    this.customInput = customInput;
    /** @type {string} */
    this.currentColor = DEFAULT_COLOR;

    this._init();
  }

  /**
   * Initialize: read stored color, set active state, wire event listeners.
   */
  _init() {
    // Read persisted color from localStorage
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && HEX_PATTERN.test(stored)) {
      this.currentColor = stored.toUpperCase();
    } else {
      this.currentColor = DEFAULT_COLOR;
    }

    // Set initial active state on the matching swatch (if any)
    this._updateActiveState();

    // Sync custom input value to currentColor
    this.customInput.value = this.currentColor;

    // Wire swatch click events
    for (const swatch of this.swatches) {
      swatch.addEventListener('click', () => {
        const color = swatch.dataset.color;
        if (color) {
          this._selectColor(color);
        }
      });
    }

    // Wire custom color input change
    this.customInput.addEventListener('input', () => {
      const color = this.customInput.value;
      if (color) {
        this._selectColor(color.toUpperCase());
      }
    });
  }

  /**
   * Set the current color, update UI, and persist to localStorage.
   * @param {string} color - hex color string (#RRGGBB)
   */
  _selectColor(color) {
    this.currentColor = color.toUpperCase();
    this._updateActiveState();
    this.customInput.value = this.currentColor;
    localStorage.setItem(STORAGE_KEY, this.currentColor);
  }

  /**
   * Update the .active class on swatches to highlight the current color.
   * Removes .active from all swatches, adds it to the one matching currentColor.
   */
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

  /**
   * Get the currently selected color as a hex string.
   * @returns {string} hex color e.g. '#FF0000'
   */
  getColor() {
    return this.currentColor;
  }
}
