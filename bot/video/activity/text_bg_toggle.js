/**
 * Text Background Toggle — wires the #text-bg-toggle checkbox in the HUD.
 *
 * Exports a single function `getTextBg()` that returns the current checked
 * state of the checkbox (boolean). The TextTool reads this when finalizing
 * a text stroke to set the `text_bg` field.
 *
 * Persists the toggle state to localStorage so it survives page reloads
 * within the same session.
 */

const STORAGE_KEY = 'hellodj-text-bg';

/** @type {HTMLInputElement} */
let checkbox = null;

/**
 * Initialize the text background toggle module.
 * Binds to the #text-bg-toggle checkbox element and restores persisted state.
 *
 * @param {HTMLInputElement} [el] - Optional direct reference to the checkbox element.
 *   If omitted, queries the DOM for #text-bg-toggle.
 */
export function initTextBgToggle(el) {
  checkbox = el || document.getElementById('text-bg-toggle');
  if (!checkbox) return;

  // Restore persisted state from localStorage
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored === 'true') {
    checkbox.checked = true;
  } else if (stored === 'false') {
    checkbox.checked = false;
  }
  // If no stored value, keep the checkbox's default (unchecked)

  // Persist on change
  checkbox.addEventListener('change', () => {
    localStorage.setItem(STORAGE_KEY, String(checkbox.checked));
  });
}

/**
 * Returns whether the text background toggle is currently enabled.
 * Used by TextTool to set the `text_bg` field on finalized text strokes.
 *
 * @returns {boolean}
 */
export function getTextBg() {
  if (!checkbox) return false;
  return checkbox.checked;
}
