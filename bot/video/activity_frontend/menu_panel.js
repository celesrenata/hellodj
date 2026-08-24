/**
 * menu_panel.js — VisualizerMenu top-level controller.
 *
 * Manages the visualizer menu panel lifecycle, navigation between sub-views
 * (engines, presets, settings), WebSocket message routing, and state sync.
 *
 * ES module — imported by app.js via <script type="module">.
 */

import { EngineSelector } from './engine_selector.js';
import { PresetBrowser } from './preset_browser.js';
import { SettingsPanel } from './settings_panel.js';
import { SavePresetModal } from './save_preset_modal.js';

/**
 * VisualizerMenu — Top-level menu panel controller.
 *
 * @param {HTMLElement} containerEl - Parent div to render the menu panel into.
 * @param {function} wsSend - Function that sends a JSON object over WebSocket.
 * @param {function} onClose - Callback invoked when the menu closes itself.
 */
export class VisualizerMenu {
  constructor(containerEl, wsSend, onClose) {
    this._container = containerEl;
    this._wsSend = wsSend;
    this._onClose = onClose;

    // State
    this._isOpen = false;
    this._activeView = 'engines'; // 'engines' | 'presets' | 'settings'
    this._activeEngine = null;
    this._activePreset = null;
    this._engines = [];
    this._destroyed = false;

    // Build DOM structure
    this._buildDOM();

    // Sub-components (created once, rendered on demand)
    this._engineSelector = new EngineSelector(this._viewEnginesEl, (engineId) => {
      this._onEngineSelect(engineId);
    });

    this._presetBrowser = new PresetBrowser(this._viewPresetsEl, {
      onApply: (presetName) => this._onPresetApply(presetName),
      onSave: () => this._onPresetSaveRequest(),
      onDelete: (presetName) => this._onPresetDelete(presetName),
    });

    this._settingsPanel = new SettingsPanel(this._viewSettingsEl, {
      onChange: (setting, value) => this._onSettingChange(setting, value),
      onSavePreset: () => this._onPresetSaveRequest(),
    });

    this._savePresetModal = new SavePresetModal(
      (name) => this._onPresetSaveSubmit(name),
      () => {} // onCancel — no-op, modal hides itself
    );

    // Keyboard handler
    this._keyHandler = (e) => this._onKeyDown(e);
  }

  // --- Panel Lifecycle ---

  /** Open the menu panel with slide-in animation. */
  open() {
    if (this._isOpen || this._destroyed) return;
    this._isOpen = true;

    this._panelEl.classList.add('menu-open');
    this._container.classList.add('menu-active');

    // Trap focus within menu
    document.addEventListener('keydown', this._keyHandler);

    // Request current state from backend
    this._wsSend({ type: 'menu_init' });

    // Show engines view by default
    this._navigateTo('engines', false);

    // Focus the panel for accessibility
    requestAnimationFrame(() => {
      this._panelEl.focus();
    });
  }

  /** Close the menu panel with slide-out animation. */
  close() {
    if (!this._isOpen || this._destroyed) return;
    this._isOpen = false;

    this._panelEl.classList.remove('menu-open');
    this._container.classList.remove('menu-active');

    // Remove keyboard handler
    document.removeEventListener('keydown', this._keyHandler);

    // Invoke close callback (returns focus to toggle)
    if (this._onClose) this._onClose();
  }

  /** Toggle open/close state. */
  toggle() {
    if (this._isOpen) {
      this.close();
    } else {
      this.open();
    }
  }

  /** Destroy the menu panel and cleanup all event listeners. */
  destroy() {
    if (this._destroyed) return;
    this._destroyed = true;
    this._isOpen = false;

    document.removeEventListener('keydown', this._keyHandler);

    // Cleanup sub-components if they have destroy methods
    if (this._engineSelector.destroy) this._engineSelector.destroy();
    if (this._presetBrowser.destroy) this._presetBrowser.destroy();
    if (this._settingsPanel.destroy) this._settingsPanel.destroy();
    if (this._savePresetModal.destroy) this._savePresetModal.destroy();

    // Remove DOM
    if (this._panelEl && this._panelEl.parentNode) {
      this._panelEl.parentNode.removeChild(this._panelEl);
    }
  }

  /** Whether the menu is currently open. */
  get isOpen() {
    return this._isOpen;
  }

  // --- Navigation ---

  /** Navigate to the engine selector view. */
  showEngines() {
    this._navigateTo('engines', true);
  }

  /** Navigate to the preset browser view. */
  showPresets() {
    this._navigateTo('presets', true);
    // Request presets for current engine
    if (this._activeEngine) {
      this._wsSend({ type: 'presets_list', engine: this._activeEngine });
    }
  }

  /** Navigate to the settings panel view. */
  showSettings() {
    this._navigateTo('settings', true);
    // Request settings schema for current engine
    if (this._activeEngine) {
      this._wsSend({ type: 'settings_schema', engine: this._activeEngine });
    }
  }

  // --- WebSocket Message Routing ---

  /**
   * Handle incoming WebSocket messages related to the menu system.
   * Returns true if the message was consumed, false otherwise.
   *
   * @param {object} data - Parsed JSON message from WebSocket.
   * @returns {boolean} Whether the message was handled.
   */
  handleMessage(data) {
    switch (data.type) {
      case 'menu_init_response':
        return this._handleMenuInitResponse(data);
      case 'presets_list_response':
        return this._handlePresetsListResponse(data);
      case 'settings_schema_response':
        return this._handleSettingsSchemaResponse(data);
      case 'engine_switch_ack':
        return this._handleEngineSwitchAck(data);
      case 'preset_apply_ack':
        return this._handlePresetApplyAck(data);
      case 'setting_change_ack':
        return this._handleSettingChangeAck(data);
      case 'preset_save_ack':
        return this._handlePresetSaveAck(data);
      case 'preset_delete_ack':
        return this._handlePresetDeleteAck(data);
      case 'visualizer_state':
        return this._handleVisualizerState(data);
      case 'preset_added':
        return this._handlePresetAdded(data);
      case 'preset_removed':
        return this._handlePresetRemoved(data);
      default:
        return false;
    }
  }

  /**
   * Update the menu UI when visualizer state changes via broadcast.
   * Called externally by the main WS handler for state sync.
   *
   * @param {object} state - Visualizer state {engine, preset, config, hls_ready}
   */
  onVisualizerStateChange(state) {
    if (!state) return;

    // Update tracked state
    if (state.engine) {
      this._activeEngine = state.engine;
      this._engineSelector.setActive(state.engine);
    }

    if (state.preset !== undefined) {
      this._activePreset = state.preset;
      if (this._presetBrowser.setActive) {
        this._presetBrowser.setActive(state.preset);
      }
    }

    if (state.config && this._settingsPanel.updateValues) {
      this._settingsPanel.updateValues(state.config);
    }
  }

  // --- Private: DOM Construction ---

  _buildDOM() {
    // Main panel container
    this._panelEl = document.createElement('div');
    this._panelEl.className = 'visualizer-menu';
    this._panelEl.setAttribute('role', 'dialog');
    this._panelEl.setAttribute('aria-label', 'Visualizer Menu');
    this._panelEl.setAttribute('tabindex', '-1');

    // Header with navigation tabs
    const header = document.createElement('div');
    header.className = 'menu-header';

    // Header title row with close button
    const titleRow = document.createElement('div');
    titleRow.className = 'menu-header-title';

    const title = document.createElement('h2');
    title.textContent = 'Visualizer';
    titleRow.appendChild(title);

    // Close button
    const closeBtn = document.createElement('button');
    closeBtn.className = 'menu-close-btn';
    closeBtn.setAttribute('aria-label', 'Close visualizer menu');
    closeBtn.textContent = '✕';
    closeBtn.addEventListener('click', () => this.close());
    titleRow.appendChild(closeBtn);

    header.appendChild(titleRow);

    // Navigation tabs
    const nav = document.createElement('nav');
    nav.className = 'menu-tabs';
    nav.setAttribute('role', 'tablist');
    nav.setAttribute('aria-label', 'Menu navigation');

    this._tabEngines = this._createTab('engines', 'Engines', true);
    this._tabPresets = this._createTab('presets', 'Presets', false);
    this._tabSettings = this._createTab('settings', 'Settings', false);

    nav.appendChild(this._tabEngines);
    nav.appendChild(this._tabPresets);
    nav.appendChild(this._tabSettings);

    header.appendChild(nav);

    // Content viewport (clips slides)
    const viewport = document.createElement('div');
    viewport.className = 'menu-content';

    // Individual view containers
    this._viewEnginesEl = document.createElement('div');
    this._viewEnginesEl.className = 'menu-view active';
    this._viewEnginesEl.setAttribute('role', 'tabpanel');
    this._viewEnginesEl.setAttribute('aria-labelledby', 'viz-tab-engines');

    this._viewPresetsEl = document.createElement('div');
    this._viewPresetsEl.className = 'menu-view slide-right';
    this._viewPresetsEl.setAttribute('role', 'tabpanel');
    this._viewPresetsEl.setAttribute('aria-labelledby', 'viz-tab-presets');

    this._viewSettingsEl = document.createElement('div');
    this._viewSettingsEl.className = 'menu-view slide-right';
    this._viewSettingsEl.setAttribute('role', 'tabpanel');
    this._viewSettingsEl.setAttribute('aria-labelledby', 'viz-tab-settings');

    viewport.appendChild(this._viewEnginesEl);
    viewport.appendChild(this._viewPresetsEl);
    viewport.appendChild(this._viewSettingsEl);

    // Connection status indicator
    this._statusEl = document.createElement('div');
    this._statusEl.className = 'menu-disconnected';
    this._statusEl.style.display = 'none';
    this._statusEl.setAttribute('aria-live', 'polite');

    // Assemble panel
    this._panelEl.appendChild(header);
    this._panelEl.appendChild(this._statusEl);
    this._panelEl.appendChild(viewport);

    this._container.appendChild(this._panelEl);
  }

  _createTab(id, label, active) {
    const tab = document.createElement('button');
    tab.className = 'menu-tab';
    tab.id = `viz-tab-${id}`;
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', active ? 'true' : 'false');
    tab.setAttribute('aria-controls', `viz-view-${id}`);
    tab.textContent = label;
    if (active) tab.classList.add('active');

    tab.addEventListener('click', () => {
      switch (id) {
        case 'engines': this.showEngines(); break;
        case 'presets': this.showPresets(); break;
        case 'settings': this.showSettings(); break;
      }
    });

    return tab;
  }

  // --- Private: Navigation ---

  _navigateTo(view, animate) {
    const views = ['engines', 'presets', 'settings'];
    const index = views.indexOf(view);
    if (index === -1) return;

    this._activeView = view;

    // Update tab states
    [this._tabEngines, this._tabPresets, this._tabSettings].forEach((tab, i) => {
      const isActive = i === index;
      tab.classList.toggle('active', isActive);
      tab.setAttribute('aria-selected', isActive ? 'true' : 'false');
    });

    // Update view visibility using CSS classes
    const viewEls = [this._viewEnginesEl, this._viewPresetsEl, this._viewSettingsEl];
    viewEls.forEach((el, i) => {
      el.classList.remove('active', 'slide-left', 'slide-right');
      if (i === index) {
        el.classList.add('active');
      } else if (i < index) {
        el.classList.add('slide-left');
      } else {
        el.classList.add('slide-right');
      }
    });
  }

  // --- Private: Keyboard ---

  _onKeyDown(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      this.close();
    }

    // Tab trapping within the panel
    if (e.key === 'Tab') {
      const focusable = this._panelEl.querySelectorAll(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      );
      if (focusable.length === 0) return;

      const first = focusable[0];
      const last = focusable[focusable.length - 1];

      if (e.shiftKey) {
        if (document.activeElement === first) {
          e.preventDefault();
          last.focus();
        }
      } else {
        if (document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    }
  }

  // --- Private: Actions ---

  _onEngineSelect(engineId) {
    if (engineId === this._activeEngine) return;
    const requestId = crypto.randomUUID();
    this._engineSelector.setLoading(engineId);
    this._wsSend({
      type: 'engine_switch',
      engine: engineId,
      request_id: requestId,
    });

    // 10s timeout: clear loading, restore previous, show error
    this._engineSwitchTimeout = setTimeout(() => {
      this._engineSelector.clearLoading(engineId);
      this._engineSelector.setError(engineId, 'Switch timed out');
      if (this._activeEngine) {
        this._engineSelector.setActive(this._activeEngine);
      }
    }, 10000);
  }

  _onPresetApply(presetName) {
    const requestId = crypto.randomUUID();
    this._presetBrowser.setLoading(presetName);
    this._wsSend({
      type: 'preset_apply',
      preset_name: presetName,
      request_id: requestId,
    });
  }

  _onPresetSaveRequest() {
    this._savePresetModal.show();
  }

  _onPresetSaveSubmit(name) {
    const requestId = crypto.randomUUID();
    this._wsSend({
      type: 'preset_save',
      name: name,
      request_id: requestId,
    });
  }

  _onPresetDelete(presetName) {
    const requestId = crypto.randomUUID();
    this._wsSend({
      type: 'preset_delete',
      name: presetName,
      request_id: requestId,
    });
  }

  _onSettingChange(setting, value) {
    const requestId = crypto.randomUUID();
    this._wsSend({
      type: 'setting_change',
      setting: setting,
      value: value,
      request_id: requestId,
    });
  }

  // --- Private: WS Message Handlers ---

  _handleMenuInitResponse(data) {
    if (data.error) {
      this._showStatus('Unable to load menu data', 'error');
      return true;
    }

    this._engines = data.engines || [];
    this._activeEngine = data.active_engine || null;
    this._activePreset = data.active_preset || null;

    // Render engine selector
    this._engineSelector.render(this._engines, this._activeEngine);

    // Clear error status
    this._hideStatus();

    return true;
  }

  _handlePresetsListResponse(data) {
    const presets = [
      ...(data.factory_presets || []).map(p => ({ ...p, factory: true })),
      ...(data.user_presets || []).map(p => ({ ...p, factory: false })),
    ];
    this._presetBrowser.render(presets, this._activePreset);
    return true;
  }

  _handleSettingsSchemaResponse(data) {
    const schema = data.settings || [];
    const currentValues = {};
    for (const entry of schema) {
      currentValues[entry.setting] = entry.current;
    }
    this._settingsPanel.render(schema, currentValues);
    return true;
  }

  _handleEngineSwitchAck(data) {
    // Clear timeout
    if (this._engineSwitchTimeout) {
      clearTimeout(this._engineSwitchTimeout);
      this._engineSwitchTimeout = null;
    }

    if (data.success) {
      const newEngine = data.engine;
      this._engineSelector.clearLoading(newEngine);
      this._engineSelector.setActive(newEngine);
      this._activeEngine = newEngine;
    } else {
      // Error — restore previous state
      const failedEngine = data.engine || this._activeEngine;
      this._engineSelector.clearLoading(failedEngine);
      this._engineSelector.setError(failedEngine, data.error || 'Switch failed');
      if (this._activeEngine) {
        this._engineSelector.setActive(this._activeEngine);
      }
    }
    return true;
  }

  _handlePresetApplyAck(data) {
    if (data.success) {
      this._presetBrowser.clearLoading();
      this._presetBrowser.setActive(data.preset_name);
      this._activePreset = data.preset_name;
    } else {
      this._presetBrowser.clearLoading();
      // Show brief error on the browser
    }
    return true;
  }

  _handleSettingChangeAck(data) {
    if (data.success) {
      this._settingsPanel.updateValue(data.setting, data.value);
    } else {
      this._settingsPanel.revertValue(data.setting, data.value);
      this._settingsPanel.showError(data.setting, data.error || 'Rejected');
    }
    return true;
  }

  _handlePresetSaveAck(data) {
    if (data.success) {
      this._savePresetModal.hide();
      if (data.preset) {
        this._presetBrowser.addPreset(data.preset);
      }
    } else {
      // Show inline error in modal
      this._savePresetModal.showError(data.error || 'Save failed');
    }
    return true;
  }

  _handlePresetDeleteAck(data) {
    if (data.success) {
      this._presetBrowser.removePreset(data.name);
    }
    return true;
  }

  _handleVisualizerState(data) {
    this.onVisualizerStateChange(data);
    return true;
  }

  _handlePresetAdded(data) {
    if (data.preset) {
      this._presetBrowser.addPreset(data.preset);
    }
    return true;
  }

  _handlePresetRemoved(data) {
    if (data.name) {
      this._presetBrowser.removePreset(data.name);
    }
    return true;
  }

  // --- Private: Status ---

  _showStatus(message, level = 'info') {
    this._statusEl.textContent = message;
    this._statusEl.className = `menu-disconnected`;
    this._statusEl.style.display = '';
  }

  _hideStatus() {
    this._statusEl.style.display = 'none';
  }

  /**
   * Show disconnected state — disables interactive controls.
   * Called externally when WebSocket disconnects.
   */
  setDisconnected(disconnected) {
    if (disconnected) {
      this._showStatus('Disconnected', 'error');
      this._panelEl.classList.add('menu-disconnected');
    } else {
      this._hideStatus();
      this._panelEl.classList.remove('menu-disconnected');
    }
  }
}
