/**
 * engine_selector.js — EngineSelector sub-component.
 *
 * Renders the engine grid with Glass_Panel cards. Each card displays:
 * - Icon (CSS class: engine-card-icon)
 * - Name (CSS class: engine-card-name)
 * - Description ≤60 chars (CSS class: engine-card-desc)
 * - Active badge when current engine (CSS class: engine-card-badge)
 *
 * Supports loading, error, and active states with 10s timeout handling.
 * Arrow key navigation with role="radiogroup" / role="radio" semantics.
 *
 * Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 9.5
 */

/** Engine icon map — maps engine IDs to emoji/symbol representations. */
const ENGINE_ICONS = {
  projectm: '🌀',
  audiovis: '📊',
  fosfora: '✨',
  varda: '🔮',
  drift: '🌊',
  dvd: '📀',
};

/**
 * EngineSelector — Renders the engine selection grid.
 *
 * @param {HTMLElement} containerEl - Parent element to render into.
 * @param {function} onEngineSelect - Callback invoked with engineId when a card is clicked.
 */
export class EngineSelector {
  constructor(containerEl, onEngineSelect) {
    this._container = containerEl;
    this._onEngineSelect = onEngineSelect;

    // State
    this._activeEngine = null;
    this._previousActiveEngine = null;
    this._engines = [];
    this._cards = new Map(); // engineId → card element
    this._loadingTimers = new Map(); // engineId → timeout id
    this._errorTimers = new Map(); // engineId → timeout id
    this._focusedIndex = -1;
    this._gridEl = null;

    // Bound handlers
    this._onGridKeyDown = this._handleGridKeyDown.bind(this);
  }

  /**
   * Render the engine grid cards.
   *
   * @param {Array} engines - Array of engine metadata objects {id, name, description, icon}.
   * @param {string|null} activeEngine - ID of the currently active engine.
   */
  render(engines, activeEngine) {
    this._engines = engines || [];
    this._activeEngine = activeEngine;
    this._previousActiveEngine = activeEngine;
    this._cards.clear();
    this._focusedIndex = -1;

    // Clear previous content
    this._container.innerHTML = '';

    // Create grid container
    this._gridEl = document.createElement('div');
    this._gridEl.className = 'engine-grid';
    this._gridEl.setAttribute('role', 'radiogroup');
    this._gridEl.setAttribute('aria-label', 'Visualizer engines');
    this._gridEl.addEventListener('keydown', this._onGridKeyDown);

    // Render each engine card
    this._engines.forEach((engine, index) => {
      const card = this._createCard(engine, index);
      this._cards.set(engine.id, card);
      this._gridEl.appendChild(card);
    });

    this._container.appendChild(this._gridEl);
  }

  /**
   * Show loading indicator on a card.
   * @param {string} engineId
   */
  setLoading(engineId) {
    const card = this._cards.get(engineId);
    if (!card) return;

    card.classList.add('loading');
    card.setAttribute('aria-busy', 'true');

    // 10s timeout: clear loading, restore previous active, show error
    this._clearLoadingTimer(engineId);
    const timer = setTimeout(() => {
      this.clearLoading(engineId);
      this.setError(engineId, 'Switch timed out');
      // Restore previous active engine
      if (this._previousActiveEngine) {
        this.setActive(this._previousActiveEngine);
      }
    }, 10000);
    this._loadingTimers.set(engineId, timer);
  }

  /**
   * Remove loading indicator from a card.
   * @param {string} engineId
   */
  clearLoading(engineId) {
    const card = this._cards.get(engineId);
    if (!card) return;

    card.classList.remove('loading');
    card.removeAttribute('aria-busy');
    this._clearLoadingTimer(engineId);
  }

  /**
   * Update the active engine highlight.
   * @param {string} engineId
   */
  setActive(engineId) {
    this._previousActiveEngine = this._activeEngine;
    this._activeEngine = engineId;

    // Remove active state from all cards
    for (const [id, card] of this._cards) {
      const isActive = id === engineId;
      card.classList.toggle('active', isActive);
      card.setAttribute('aria-checked', isActive ? 'true' : 'false');

      // Toggle badge
      const badge = card.querySelector('.engine-card-badge');
      if (badge) {
        badge.style.display = isActive ? '' : 'none';
      }
    }
  }

  /**
   * Show error indicator on a card for 3 seconds.
   * @param {string} engineId
   * @param {string} message - Error message (used for aria-label).
   */
  setError(engineId, message) {
    const card = this._cards.get(engineId);
    if (!card) return;

    card.classList.add('error');
    card.setAttribute('aria-invalid', 'true');

    // Add error tooltip/message
    let errorEl = card.querySelector('.engine-card-error');
    if (!errorEl) {
      errorEl = document.createElement('span');
      errorEl.className = 'engine-card-error error-indicator';
      errorEl.setAttribute('role', 'alert');
      card.appendChild(errorEl);
    }
    errorEl.textContent = message || 'Error';
    errorEl.style.display = '';

    // Clear after 3 seconds
    this._clearErrorTimer(engineId);
    const timer = setTimeout(() => {
      card.classList.remove('error');
      card.removeAttribute('aria-invalid');
      if (errorEl) {
        errorEl.style.display = 'none';
      }
    }, 3000);
    this._errorTimers.set(engineId, timer);
  }

  /**
   * Cleanup event listeners and timers.
   */
  destroy() {
    // Clear all timers
    for (const timer of this._loadingTimers.values()) {
      clearTimeout(timer);
    }
    for (const timer of this._errorTimers.values()) {
      clearTimeout(timer);
    }
    this._loadingTimers.clear();
    this._errorTimers.clear();

    // Remove grid keydown listener
    if (this._gridEl) {
      this._gridEl.removeEventListener('keydown', this._onGridKeyDown);
    }

    this._cards.clear();
    this._container.innerHTML = '';
  }

  // --- Private: Card Creation ---

  /**
   * Create a single engine card element.
   * @param {object} engine - Engine metadata {id, name, description, icon}.
   * @param {number} index - Position index in the grid.
   * @returns {HTMLElement}
   */
  _createCard(engine, index) {
    const isActive = engine.id === this._activeEngine;

    const card = document.createElement('div');
    card.className = 'engine-card';
    if (isActive) card.classList.add('active');
    card.setAttribute('role', 'radio');
    card.setAttribute('aria-checked', isActive ? 'true' : 'false');
    card.setAttribute('aria-label', `${engine.name}: ${engine.description || ''}`);
    card.setAttribute('tabindex', index === 0 ? '0' : '-1');
    card.dataset.engineId = engine.id;
    card.dataset.index = index;

    // Icon
    const iconEl = document.createElement('div');
    iconEl.className = 'engine-card-icon';
    iconEl.textContent = ENGINE_ICONS[engine.icon || engine.id] || ENGINE_ICONS[engine.id] || '🎵';
    iconEl.setAttribute('aria-hidden', 'true');
    card.appendChild(iconEl);

    // Name
    const nameEl = document.createElement('div');
    nameEl.className = 'engine-card-name';
    nameEl.textContent = engine.name || engine.id;
    card.appendChild(nameEl);

    // Description (clamped to 60 chars)
    const descEl = document.createElement('div');
    descEl.className = 'engine-card-desc';
    const description = engine.description || '';
    descEl.textContent = description.length > 60
      ? description.slice(0, 57) + '…'
      : description;
    card.appendChild(descEl);

    // Active badge
    const badge = document.createElement('span');
    badge.className = 'engine-card-badge';
    badge.textContent = 'Active';
    badge.style.display = isActive ? '' : 'none';
    card.appendChild(badge);

    // Click handler — emit engine_switch only if not already active
    card.addEventListener('click', () => {
      if (engine.id !== this._activeEngine && !card.classList.contains('loading')) {
        this._onEngineSelect(engine.id);
      }
    });

    // Enter/Space activation (radio button semantics)
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        if (engine.id !== this._activeEngine && !card.classList.contains('loading')) {
          this._onEngineSelect(engine.id);
        }
      }
    });

    return card;
  }

  // --- Private: Arrow Key Navigation ---

  /**
   * Handle arrow key navigation within the engine grid.
   * Moves focus between cards using a 2-column desktop / 1-column mobile layout.
   * @param {KeyboardEvent} e
   */
  _handleGridKeyDown(e) {
    const validKeys = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End'];
    if (!validKeys.includes(e.key)) return;

    e.preventDefault();

    const cards = Array.from(this._gridEl.querySelectorAll('.engine-card'));
    if (cards.length === 0) return;

    // Find currently focused card index
    const currentIndex = cards.findIndex(c => c === document.activeElement);
    if (currentIndex === -1) {
      // No card focused yet — focus first
      this._focusCard(cards, 0);
      return;
    }

    // Determine grid columns based on viewport (2 on desktop, 1 on mobile)
    const cols = window.matchMedia('(max-width: 599px)').matches ? 1 : 2;
    let nextIndex = currentIndex;

    switch (e.key) {
      case 'ArrowRight':
        nextIndex = Math.min(currentIndex + 1, cards.length - 1);
        break;
      case 'ArrowLeft':
        nextIndex = Math.max(currentIndex - 1, 0);
        break;
      case 'ArrowDown':
        nextIndex = Math.min(currentIndex + cols, cards.length - 1);
        break;
      case 'ArrowUp':
        nextIndex = Math.max(currentIndex - cols, 0);
        break;
      case 'Home':
        nextIndex = 0;
        break;
      case 'End':
        nextIndex = cards.length - 1;
        break;
    }

    if (nextIndex !== currentIndex) {
      this._focusCard(cards, nextIndex);
    }
  }

  /**
   * Move focus to a card at the given index, updating roving tabindex.
   * @param {HTMLElement[]} cards - All card elements.
   * @param {number} index - Index to focus.
   */
  _focusCard(cards, index) {
    // Update roving tabindex
    cards.forEach((card, i) => {
      card.setAttribute('tabindex', i === index ? '0' : '-1');
    });
    cards[index].focus();
    this._focusedIndex = index;
  }

  // --- Private: Timer Management ---

  _clearLoadingTimer(engineId) {
    const timer = this._loadingTimers.get(engineId);
    if (timer) {
      clearTimeout(timer);
      this._loadingTimers.delete(engineId);
    }
  }

  _clearErrorTimer(engineId) {
    const timer = this._errorTimers.get(engineId);
    if (timer) {
      clearTimeout(timer);
      this._errorTimers.delete(engineId);
    }
  }
}
