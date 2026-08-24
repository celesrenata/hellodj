/**
 * preset_browser.js — PresetBrowser sub-component.
 *
 * Renders preset cards grouped by Factory/User sections with Glass_Panel cards.
 * Each card shows the preset name and up to 4 metadata tags.
 * Supports live updates (addPreset/removePreset) without full re-render.
 *
 * Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7
 */

export class PresetBrowser {
  /**
   * @param {HTMLElement} containerEl — Parent element to render into
   * @param {Object} callbacks
   * @param {function(string): void} callbacks.onApply — Called with preset name on card click
   * @param {function(): void} callbacks.onSave — Called when save action triggered
   * @param {function(string): void} callbacks.onDelete — Called with preset name on delete
   */
  constructor(containerEl, { onApply, onSave, onDelete }) {
    this._container = containerEl;
    this._onApply = onApply;
    this._onSave = onSave;
    this._onDelete = onDelete;
    this._activePreset = null;
    this._presets = [];
    this._loadingPreset = null;

    // Enable momentum scrolling for overflow
    this._container.style.overflowY = 'auto';
    this._container.style.webkitOverflowScrolling = 'touch';
  }

  /**
   * Render all presets grouped into Factory and User sections.
   * @param {Array<{name: string, engine: string, factory: boolean, config: Object, tags: string[]}>} presets
   * @param {string|null} activePreset — Name of currently active preset
   */
  render(presets, activePreset) {
    this._presets = presets || [];
    this._activePreset = activePreset;
    this._loadingPreset = null;

    this._container.innerHTML = '';

    if (!this._presets.length) {
      this.showEmpty();
      return;
    }

    const factory = this._presets.filter(p => p.factory);
    const user = this._presets.filter(p => !p.factory);

    if (factory.length) {
      this._container.appendChild(this._createSectionHeader('Factory'));
      const factoryList = this._createList();
      factory.forEach(preset => factoryList.appendChild(this._createCard(preset)));
      this._container.appendChild(factoryList);
    }

    if (user.length) {
      this._container.appendChild(this._createSectionHeader('User'));
      const userList = this._createList();
      user.forEach(preset => userList.appendChild(this._createCard(preset)));
      this._container.appendChild(userList);
    }
  }

  /**
   * Show loading indicator on a specific preset card.
   * @param {string} presetName
   */
  setLoading(presetName) {
    this._loadingPreset = presetName;
    const card = this._findCard(presetName);
    if (card) {
      card.classList.add('loading');
    }
  }

  /**
   * Remove loading indicator from all cards.
   */
  clearLoading() {
    if (this._loadingPreset) {
      const card = this._findCard(this._loadingPreset);
      if (card) {
        card.classList.remove('loading');
      }
      this._loadingPreset = null;
    }
  }

  /**
   * Update the active preset highlight.
   * @param {string|null} presetName
   */
  setActive(presetName) {
    // Remove old active
    if (this._activePreset) {
      const oldCard = this._findCard(this._activePreset);
      if (oldCard) {
        oldCard.classList.remove('active');
      }
    }

    this._activePreset = presetName;

    // Add new active
    if (presetName) {
      const newCard = this._findCard(presetName);
      if (newCard) {
        newCard.classList.add('active');
      }
    }
  }

  /**
   * Add a single preset to the appropriate section without full re-render.
   * @param {{name: string, engine: string, factory: boolean, config: Object, tags: string[]}} preset
   */
  addPreset(preset) {
    this._presets.push(preset);

    // If we were showing the empty state, clear and re-render
    const emptyEl = this._container.querySelector('.preset-empty');
    if (emptyEl) {
      this._container.innerHTML = '';
    }

    const sectionLabel = preset.factory ? 'Factory' : 'User';
    let list = this._findSectionList(sectionLabel);

    if (!list) {
      // Create the section header + list
      this._container.appendChild(this._createSectionHeader(sectionLabel));
      list = this._createList();
      this._container.appendChild(list);
    }

    const card = this._createCard(preset);
    list.appendChild(card);
  }

  /**
   * Remove a preset card from the DOM without full re-render.
   * @param {string} presetName
   */
  removePreset(presetName) {
    this._presets = this._presets.filter(p => p.name !== presetName);

    const card = this._findCard(presetName);
    if (card) {
      const list = card.parentElement;
      card.remove();

      // If the list is now empty, remove the section header and list
      if (list && list.children.length === 0) {
        const header = list.previousElementSibling;
        if (header && header.classList.contains('preset-section-header')) {
          header.remove();
        }
        list.remove();
      }
    }

    // If no presets remain, show empty state
    if (!this._presets.length) {
      this.showEmpty();
    }
  }

  /**
   * Display "No presets available" empty state.
   */
  showEmpty() {
    this._container.innerHTML = '';
    const emptyEl = document.createElement('div');
    emptyEl.className = 'preset-empty';
    emptyEl.setAttribute('role', 'status');
    emptyEl.innerHTML = `
      <span aria-hidden="true" style="font-size: 1.5rem; opacity: 0.5;">🎨</span>
      <span>No presets available for this engine</span>
    `;
    this._container.appendChild(emptyEl);
  }

  /**
   * Clean up event listeners.
   */
  destroy() {
    this._container.innerHTML = '';
    this._presets = [];
    this._activePreset = null;
    this._loadingPreset = null;
  }

  // --- Private helpers ---

  /**
   * Create a section header element ("Factory" or "User").
   * @param {string} label
   * @returns {HTMLElement}
   */
  _createSectionHeader(label) {
    const header = document.createElement('div');
    header.className = 'preset-section-header';
    header.textContent = label;
    header.setAttribute('role', 'heading');
    header.setAttribute('aria-level', '3');
    return header;
  }

  /**
   * Create a preset list container.
   * @returns {HTMLElement}
   */
  _createList() {
    const list = document.createElement('div');
    list.className = 'preset-list';
    list.setAttribute('role', 'list');
    return list;
  }

  /**
   * Create a preset card element.
   * @param {{name: string, engine: string, factory: boolean, config: Object, tags: string[]}} preset
   * @returns {HTMLElement}
   */
  _createCard(preset) {
    const card = document.createElement('div');
    card.className = 'preset-card';
    card.setAttribute('role', 'listitem');
    card.setAttribute('tabindex', '0');
    card.setAttribute('aria-label', `Preset: ${preset.name}${this._activePreset === preset.name ? ' (active)' : ''}`);
    card.dataset.presetName = preset.name;

    if (this._activePreset === preset.name) {
      card.classList.add('active');
    }

    // Top row: name + delete button (for user presets only)
    const topRow = document.createElement('div');
    topRow.style.display = 'flex';
    topRow.style.alignItems = 'center';
    topRow.style.justifyContent = 'space-between';
    topRow.style.gap = '8px';

    const nameEl = document.createElement('span');
    nameEl.className = 'preset-card-name';
    nameEl.textContent = preset.name;
    topRow.appendChild(nameEl);

    if (!preset.factory) {
      const deleteBtn = document.createElement('button');
      deleteBtn.className = 'preset-delete-btn';
      deleteBtn.setAttribute('aria-label', `Delete preset ${preset.name}`);
      deleteBtn.textContent = '✕';
      deleteBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (this._onDelete) this._onDelete(preset.name);
      });
      topRow.appendChild(deleteBtn);
    }

    card.appendChild(topRow);

    // Tags row (up to 4)
    const tags = (preset.tags || []).slice(0, 4);
    if (tags.length) {
      const tagsEl = document.createElement('div');
      tagsEl.className = 'preset-card-tags';
      tags.forEach(tag => {
        const pill = document.createElement('span');
        pill.className = 'preset-tag';
        pill.textContent = tag;
        tagsEl.appendChild(pill);
      });
      card.appendChild(tagsEl);
    }

    // Click to apply
    card.addEventListener('click', () => {
      if (this._loadingPreset) return; // Ignore clicks while loading
      if (this._onApply) this._onApply(preset.name);
    });

    // Keyboard: Enter/Space to apply
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        if (this._loadingPreset) return;
        if (this._onApply) this._onApply(preset.name);
      }
    });

    return card;
  }

  /**
   * Find a card element by preset name.
   * @param {string} presetName
   * @returns {HTMLElement|null}
   */
  _findCard(presetName) {
    return this._container.querySelector(`.preset-card[data-preset-name="${CSS.escape(presetName)}"]`);
  }

  /**
   * Find a preset-list element following a given section header label.
   * @param {string} sectionLabel — "Factory" or "User"
   * @returns {HTMLElement|null}
   */
  _findSectionList(sectionLabel) {
    const headers = this._container.querySelectorAll('.preset-section-header');
    for (const header of headers) {
      if (header.textContent === sectionLabel) {
        const next = header.nextElementSibling;
        if (next && next.classList.contains('preset-list')) {
          return next;
        }
      }
    }
    return null;
  }
}
