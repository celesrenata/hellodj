/**
 * settings_panel.js — SettingsPanel sub-component.
 *
 * Renders dynamic controls based on engine config schema.
 * Supports slider (float/int), toggle (bool), dropdown (choice) controls.
 * Groups settings under labeled sections when >4 parameters.
 * Debounces continuous inputs (100ms) before emitting setting_change.
 *
 * Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 6.1
 */

export class SettingsPanel {
  /**
   * @param {HTMLElement} containerEl - Parent element to render controls into.
   * @param {object} callbacks
   * @param {function} callbacks.onChange - Called with (setting, value) after debounce.
   * @param {function} callbacks.onSavePreset - Called when Save Preset button is clicked.
   */
  constructor(containerEl, { onChange, onSavePreset }) {
    this._container = containerEl;
    this._onChange = onChange;
    this._onSavePreset = onSavePreset;

    /** @type {Map<string, HTMLElement>} setting key → control container element */
    this._controlEls = new Map();

    /** @type {Map<string, HTMLElement>} setting key → value display element */
    this._valueEls = new Map();

    /** @type {Map<string, HTMLInputElement|HTMLSelectElement>} setting key → input element */
    this._inputEls = new Map();

    /** @type {Map<string, number>} setting key → debounce timer ID */
    this._debounceTimers = new Map();

    /** @type {Map<string, number>} setting key → error timeout ID */
    this._errorTimers = new Map();

    /** @type {object} Current schema entries */
    this._schema = [];

    /** @type {object} Current values keyed by setting name */
    this._currentValues = {};
  }

  /**
   * Render controls from the engine config schema.
   * Clears any existing controls and rebuilds from scratch.
   *
   * @param {Array} schema - Array of schema entry objects.
   * @param {object} currentValues - Map of setting key → current value.
   */
  render(schema, currentValues) {
    // Clear existing state
    this._clearDebounceTimers();
    this._clearErrorTimers();
    this._controlEls.clear();
    this._valueEls.clear();
    this._inputEls.clear();
    this._container.innerHTML = '';

    this._schema = schema || [];
    this._currentValues = { ...currentValues };

    if (this._schema.length === 0) {
      this._renderEmpty();
      return;
    }

    const shouldGroup = this._schema.length > 4;

    if (shouldGroup) {
      this._renderGrouped();
    } else {
      this._renderFlat();
    }

    // Add "Save Preset" button at the bottom
    this._renderSaveButton();
  }

  /**
   * Update a single control's displayed value after server confirmation.
   *
   * @param {string} setting - The setting key.
   * @param {*} value - The confirmed value.
   */
  updateValue(setting, value) {
    this._currentValues[setting] = value;
    this._updateControlDisplay(setting, value);
  }

  /**
   * Update multiple control values at once (e.g., from visualizer_state broadcast).
   *
   * @param {object} valuesObj - Map of setting key → value.
   */
  updateValues(valuesObj) {
    if (!valuesObj) return;
    for (const [setting, value] of Object.entries(valuesObj)) {
      if (this._inputEls.has(setting)) {
        this._currentValues[setting] = value;
        this._updateControlDisplay(setting, value);
      }
    }
  }

  /**
   * Revert a control to a previous value (on server rejection).
   *
   * @param {string} setting - The setting key.
   * @param {*} previousValue - The value to revert to.
   */
  revertValue(setting, previousValue) {
    this._currentValues[setting] = previousValue;
    this._updateControlDisplay(setting, previousValue);
  }

  /**
   * Show an error indicator on a setting control for 3 seconds.
   *
   * @param {string} setting - The setting key.
   * @param {string} message - Error message (used as title tooltip).
   */
  showError(setting, message) {
    const controlEl = this._controlEls.get(setting);
    if (!controlEl) return;

    controlEl.classList.add('error');
    controlEl.setAttribute('title', message || 'Error');

    // Clear any existing error timer for this setting
    if (this._errorTimers.has(setting)) {
      clearTimeout(this._errorTimers.get(setting));
    }

    // Remove error state after 3 seconds
    const timerId = setTimeout(() => {
      controlEl.classList.remove('error');
      controlEl.removeAttribute('title');
      this._errorTimers.delete(setting);
    }, 3000);

    this._errorTimers.set(setting, timerId);
  }

  /**
   * Cleanup timers and event listeners.
   */
  destroy() {
    this._clearDebounceTimers();
    this._clearErrorTimers();
    this._controlEls.clear();
    this._valueEls.clear();
    this._inputEls.clear();
    this._container.innerHTML = '';
  }

  // --- Private: Rendering ---

  /** Render controls grouped by the `group` field from schema. */
  _renderGrouped() {
    // Collect groups in insertion order
    const groups = new Map();
    for (const entry of this._schema) {
      const groupName = entry.group || 'General';
      if (!groups.has(groupName)) {
        groups.set(groupName, []);
      }
      groups.get(groupName).push(entry);
    }

    for (const [groupName, entries] of groups) {
      const groupEl = document.createElement('div');
      groupEl.className = 'settings-group';

      const labelEl = document.createElement('div');
      labelEl.className = 'settings-group-label';
      labelEl.textContent = groupName;
      groupEl.appendChild(labelEl);

      for (const entry of entries) {
        const controlEl = this._createControl(entry);
        groupEl.appendChild(controlEl);
      }

      this._container.appendChild(groupEl);
    }
  }

  /** Render controls in a flat list (no grouping headers). */
  _renderFlat() {
    for (const entry of this._schema) {
      const controlEl = this._createControl(entry);
      this._container.appendChild(controlEl);
    }
  }

  /** Show an empty state message when no settings are available. */
  _renderEmpty() {
    const emptyEl = document.createElement('div');
    emptyEl.className = 'preset-empty';
    emptyEl.textContent = 'No settings available for this engine.';
    this._container.appendChild(emptyEl);
  }

  /** Render the "Save Preset" button at the bottom. */
  _renderSaveButton() {
    const btn = document.createElement('button');
    btn.className = 'settings-save-btn';
    btn.type = 'button';
    btn.setAttribute('aria-label', 'Save current settings as a preset');
    btn.innerHTML = '<span>💾</span><span>Save Preset</span>';
    btn.addEventListener('click', () => {
      if (this._onSavePreset) this._onSavePreset();
    });
    this._container.appendChild(btn);
  }

  /**
   * Create a control element for a single schema entry.
   *
   * @param {object} entry - Schema entry {setting, type, label, default, current, min, max, group, choices}
   * @returns {HTMLElement} The control container element.
   */
  _createControl(entry) {
    const controlEl = document.createElement('div');
    controlEl.className = 'setting-control';
    controlEl.dataset.setting = entry.setting;

    // Label row: name + current value display
    const labelEl = document.createElement('div');
    labelEl.className = 'setting-label';

    const labelText = document.createElement('span');
    labelText.className = 'setting-label-text';
    labelText.textContent = entry.label || entry.setting;

    const labelValue = document.createElement('span');
    labelValue.className = 'setting-label-value';
    labelValue.textContent = this._formatValue(entry.type, this._currentValues[entry.setting] ?? entry.current ?? entry.default);

    labelEl.appendChild(labelText);
    labelEl.appendChild(labelValue);
    controlEl.appendChild(labelEl);

    // Create the appropriate input control
    let inputEl;
    switch (entry.type) {
      case 'float':
      case 'int':
        inputEl = this._createSlider(entry);
        break;
      case 'bool':
        inputEl = this._createToggle(entry);
        break;
      case 'choice':
        inputEl = this._createDropdown(entry);
        break;
      default:
        // Fallback to slider for unknown numeric types
        inputEl = this._createSlider(entry);
        break;
    }

    controlEl.appendChild(inputEl);

    // Store references
    this._controlEls.set(entry.setting, controlEl);
    this._valueEls.set(entry.setting, labelValue);

    return controlEl;
  }

  /**
   * Create a slider (range input) for float/int settings.
   */
  _createSlider(entry) {
    const slider = document.createElement('input');
    slider.type = 'range';
    slider.className = 'setting-slider';
    slider.id = `setting-${entry.setting}`;
    slider.setAttribute('aria-label', entry.label || entry.setting);

    const currentVal = this._currentValues[entry.setting] ?? entry.current ?? entry.default ?? 0;
    const min = entry.min ?? 0;
    const max = entry.max ?? 1;

    slider.min = min;
    slider.max = max;
    slider.value = currentVal;

    // Step: float gets fine granularity, int gets step 1
    if (entry.type === 'int') {
      slider.step = 1;
    } else {
      // Calculate a reasonable step for float (1/100th of range)
      const range = max - min;
      slider.step = range > 0 ? (range / 100).toFixed(6) : 0.01;
    }

    // Debounced input handler (100ms settle time)
    slider.addEventListener('input', () => {
      const value = entry.type === 'int' ? parseInt(slider.value, 10) : parseFloat(slider.value);
      // Update display immediately (optimistic UI)
      const valueEl = this._valueEls.get(entry.setting);
      if (valueEl) {
        valueEl.textContent = this._formatValue(entry.type, value);
      }
      // Debounce the actual onChange callback
      this._debounce(entry.setting, value);
    });

    this._inputEls.set(entry.setting, slider);
    return slider;
  }

  /**
   * Create a toggle (checkbox) for bool settings.
   */
  _createToggle(entry) {
    const toggleContainer = document.createElement('label');
    toggleContainer.className = 'setting-toggle';

    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.id = `setting-${entry.setting}`;
    checkbox.setAttribute('aria-label', entry.label || entry.setting);

    const currentVal = this._currentValues[entry.setting] ?? entry.current ?? entry.default ?? false;
    checkbox.checked = Boolean(currentVal);

    const track = document.createElement('span');
    track.className = 'setting-toggle-track';

    toggleContainer.appendChild(checkbox);
    toggleContainer.appendChild(track);

    // Toggle fires immediately (no debounce needed for discrete inputs)
    checkbox.addEventListener('change', () => {
      const value = checkbox.checked;
      const valueEl = this._valueEls.get(entry.setting);
      if (valueEl) {
        valueEl.textContent = this._formatValue('bool', value);
      }
      // Fire immediately for boolean (discrete change, not continuous)
      if (this._onChange) {
        this._onChange(entry.setting, value);
      }
    });

    this._inputEls.set(entry.setting, checkbox);
    return toggleContainer;
  }

  /**
   * Create a dropdown (select) for choice settings.
   */
  _createDropdown(entry) {
    const select = document.createElement('select');
    select.className = 'setting-dropdown';
    select.id = `setting-${entry.setting}`;
    select.setAttribute('aria-label', entry.label || entry.setting);

    const choices = entry.choices || [];
    const currentVal = this._currentValues[entry.setting] ?? entry.current ?? entry.default ?? '';

    for (const choice of choices) {
      const option = document.createElement('option');
      option.value = choice;
      option.textContent = choice;
      if (choice === currentVal) {
        option.selected = true;
      }
      select.appendChild(option);
    }

    // Dropdown fires immediately (discrete change)
    select.addEventListener('change', () => {
      const value = select.value;
      const valueEl = this._valueEls.get(entry.setting);
      if (valueEl) {
        valueEl.textContent = this._formatValue('choice', value);
      }
      if (this._onChange) {
        this._onChange(entry.setting, value);
      }
    });

    this._inputEls.set(entry.setting, select);
    return select;
  }

  // --- Private: Debouncing ---

  /**
   * Debounce an onChange call for continuous inputs (sliders).
   * Only fires after 100ms of no new input events.
   *
   * @param {string} setting - The setting key.
   * @param {*} value - The latest value.
   */
  _debounce(setting, value) {
    // Clear existing timer for this setting
    if (this._debounceTimers.has(setting)) {
      clearTimeout(this._debounceTimers.get(setting));
    }

    const timerId = setTimeout(() => {
      this._debounceTimers.delete(setting);
      if (this._onChange) {
        this._onChange(setting, value);
      }
    }, 100);

    this._debounceTimers.set(setting, timerId);
  }

  // --- Private: Display Updates ---

  /**
   * Update a control's input element and value display to reflect a new value.
   *
   * @param {string} setting - The setting key.
   * @param {*} value - The new value.
   */
  _updateControlDisplay(setting, value) {
    const inputEl = this._inputEls.get(setting);
    const valueEl = this._valueEls.get(setting);

    if (!inputEl) return;

    // Determine type from schema
    const entry = this._schema.find(e => e.setting === setting);
    const type = entry ? entry.type : 'float';

    // Update the input element
    if (inputEl.type === 'checkbox') {
      inputEl.checked = Boolean(value);
    } else if (inputEl.tagName === 'SELECT') {
      inputEl.value = value;
    } else {
      // Slider
      inputEl.value = value;
    }

    // Update the value display
    if (valueEl) {
      valueEl.textContent = this._formatValue(type, value);
    }
  }

  /**
   * Format a value for display in the label.
   *
   * @param {string} type - The setting type (float, int, bool, choice).
   * @param {*} value - The value to format.
   * @returns {string} Formatted string.
   */
  _formatValue(type, value) {
    switch (type) {
      case 'float':
        return typeof value === 'number' ? value.toFixed(2) : String(value ?? '');
      case 'int':
        return String(Math.round(Number(value)) || 0);
      case 'bool':
        return value ? 'On' : 'Off';
      case 'choice':
        return String(value ?? '');
      default:
        return String(value ?? '');
    }
  }

  // --- Private: Cleanup ---

  /** Clear all debounce timers. */
  _clearDebounceTimers() {
    for (const timerId of this._debounceTimers.values()) {
      clearTimeout(timerId);
    }
    this._debounceTimers.clear();
  }

  /** Clear all error timeout timers. */
  _clearErrorTimers() {
    for (const timerId of this._errorTimers.values()) {
      clearTimeout(timerId);
    }
    this._errorTimers.clear();
  }
}
