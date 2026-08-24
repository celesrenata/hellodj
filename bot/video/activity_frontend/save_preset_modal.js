/**
 * save_preset_modal.js — SavePresetModal class.
 *
 * Glass_Panel modal for naming and saving presets.
 * Appended to document.body (teleported) to avoid z-index issues.
 * Focus trap within modal; Escape to close.
 *
 * Requirements: 6.1, 6.2, 9.1
 */

const PRESET_NAME_REGEX = /^[a-zA-Z0-9 -]{1,50}$/;
const VALIDATION_ERROR_MSG = 'Name must be 1\u201350 characters (letters, numbers, hyphens, spaces)';

export class SavePresetModal {
  /**
   * @param {(name: string) => void} onSubmit — called with validated preset name
   * @param {() => void} onCancel — called on cancel or Escape
   */
  constructor(onSubmit, onCancel) {
    this._onSubmit = onSubmit;
    this._onCancel = onCancel;
    this._visible = false;
    this._previousFocus = null;

    this._buildDOM();
    this._bindEvents();
  }

  /* === Public API === */

  show() {
    this._previousFocus = document.activeElement;
    this._input.value = '';
    this._clearError();
    this._input.classList.remove('invalid');
    this._backdrop.classList.add('visible');
    this._visible = true;

    // Focus the input after the modal appears
    requestAnimationFrame(() => this._input.focus());
  }

  hide() {
    this._backdrop.classList.remove('visible');
    this._visible = false;

    // Return focus to the previously focused element
    if (this._previousFocus && typeof this._previousFocus.focus === 'function') {
      this._previousFocus.focus();
    }
    this._previousFocus = null;
  }

  /**
   * Display an inline error message (e.g. from server rejection).
   * @param {string} message
   */
  showError(message) {
    this._errorEl.textContent = message;
    this._input.classList.add('invalid');
  }

  destroy() {
    this._removeEvents();
    if (this._backdrop.parentNode) {
      this._backdrop.parentNode.removeChild(this._backdrop);
    }
  }

  /* === Internal === */

  _buildDOM() {
    // Backdrop
    this._backdrop = document.createElement('div');
    this._backdrop.className = 'save-modal-backdrop';
    this._backdrop.setAttribute('role', 'dialog');
    this._backdrop.setAttribute('aria-modal', 'true');
    this._backdrop.setAttribute('aria-label', 'Save preset');

    // Modal panel
    const modal = document.createElement('div');
    modal.className = 'save-modal';
    this._modal = modal;

    // Title
    const title = document.createElement('h3');
    title.className = 'save-modal-title';
    title.textContent = 'Save Preset';
    modal.appendChild(title);

    // Input
    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'save-modal-input';
    input.placeholder = 'Preset name\u2026';
    input.maxLength = 50;
    input.setAttribute('aria-label', 'Preset name');
    input.autocomplete = 'off';
    this._input = input;
    modal.appendChild(input);

    // Error text
    const errorEl = document.createElement('div');
    errorEl.className = 'save-modal-error';
    errorEl.setAttribute('role', 'alert');
    errorEl.setAttribute('aria-live', 'polite');
    this._errorEl = errorEl;
    modal.appendChild(errorEl);

    // Actions
    const actions = document.createElement('div');
    actions.className = 'save-modal-actions';

    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'save-modal-cancel';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.setAttribute('aria-label', 'Cancel save preset');
    this._cancelBtn = cancelBtn;

    const submitBtn = document.createElement('button');
    submitBtn.type = 'button';
    submitBtn.className = 'save-modal-submit';
    submitBtn.textContent = 'Save';
    submitBtn.setAttribute('aria-label', 'Save preset');
    this._submitBtn = submitBtn;

    actions.appendChild(cancelBtn);
    actions.appendChild(submitBtn);
    modal.appendChild(actions);

    this._backdrop.appendChild(modal);

    // Teleport to body
    document.body.appendChild(this._backdrop);
  }

  _bindEvents() {
    this._onKeyDown = this._handleKeyDown.bind(this);
    this._onCancelClick = this._handleCancel.bind(this);
    this._onSubmitClick = this._handleSubmit.bind(this);
    this._onBackdropClick = this._handleBackdropClick.bind(this);
    this._onInputChange = this._handleInputChange.bind(this);

    document.addEventListener('keydown', this._onKeyDown);
    this._cancelBtn.addEventListener('click', this._onCancelClick);
    this._submitBtn.addEventListener('click', this._onSubmitClick);
    this._backdrop.addEventListener('mousedown', this._onBackdropClick);
    this._input.addEventListener('input', this._onInputChange);
  }

  _removeEvents() {
    document.removeEventListener('keydown', this._onKeyDown);
    this._cancelBtn.removeEventListener('click', this._onCancelClick);
    this._submitBtn.removeEventListener('click', this._onSubmitClick);
    this._backdrop.removeEventListener('mousedown', this._onBackdropClick);
    this._input.removeEventListener('input', this._onInputChange);
  }

  _handleKeyDown(e) {
    if (!this._visible) return;

    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      this._handleCancel();
      return;
    }

    // Focus trap: Tab cycles within modal elements
    if (e.key === 'Tab') {
      this._trapFocus(e);
    }

    // Enter submits
    if (e.key === 'Enter' && document.activeElement === this._input) {
      e.preventDefault();
      this._handleSubmit();
    }
  }

  _trapFocus(e) {
    const focusable = [this._input, this._cancelBtn, this._submitBtn];
    const currentIndex = focusable.indexOf(document.activeElement);

    if (e.shiftKey) {
      // Move backwards
      const nextIndex = currentIndex <= 0 ? focusable.length - 1 : currentIndex - 1;
      e.preventDefault();
      focusable[nextIndex].focus();
    } else {
      // Move forwards
      const nextIndex = currentIndex >= focusable.length - 1 ? 0 : currentIndex + 1;
      e.preventDefault();
      focusable[nextIndex].focus();
    }
  }

  _handleBackdropClick(e) {
    // Close on clicking backdrop (outside modal)
    if (e.target === this._backdrop) {
      this._handleCancel();
    }
  }

  _handleInputChange() {
    // Clear error state as user types
    const value = this._input.value;
    if (PRESET_NAME_REGEX.test(value) || value === '') {
      this._input.classList.remove('invalid');
      this._clearError();
    }
  }

  _handleCancel() {
    this.hide();
    if (this._onCancel) {
      this._onCancel();
    }
  }

  _handleSubmit() {
    const name = this._input.value.trim();

    if (!this._validate(name)) {
      return;
    }

    if (this._onSubmit) {
      this._onSubmit(name);
    }
  }

  /**
   * Validate preset name against regex.
   * @param {string} name
   * @returns {boolean}
   */
  _validate(name) {
    if (!PRESET_NAME_REGEX.test(name)) {
      this.showError(VALIDATION_ERROR_MSG);
      this._input.focus();
      return false;
    }
    return true;
  }

  _clearError() {
    this._errorEl.textContent = '';
  }
}
