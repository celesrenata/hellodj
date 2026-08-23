/**
 * search_panel.js — Search UI panel for the Discord Activity.
 *
 * Provides a rich search interface with progressive WebSocket-driven results,
 * provider badges, loading states, stale-result discarding, and expandable
 * Track_Group provider grouping with ISRC/normalized-key deduplication.
 *
 * Exposed as window.SearchPanel for integration with app.js.
 */
(function () {
  'use strict';

  // --- Provider configuration ---
  const PROVIDERS = {
    spotify: { icon: '🟢', color: '#1DB954', label: 'Spotify' },
    tidal: { icon: '🔵', color: '#0070EB', label: 'Tidal' },
    youtube: { icon: '🔴', color: '#FF0000', label: 'YouTube' },
    soundcloud: { icon: '🟠', color: '#FF7700', label: 'SoundCloud' },
  };

  const PROVIDER_ORDER = ['spotify', 'tidal', 'youtube', 'soundcloud'];
  const ALL_PROVIDERS = new Set(PROVIDER_ORDER);

  // --- Provider priority (lower index = higher priority) ---
  const PROVIDER_PRIORITY = { spotify: 0, tidal: 1, youtube: 2, soundcloud: 3 };

  // --- Client-side normalization for Track_Group deduplication ---

  // Matches remaster annotations: "- Remaster", "- Remastered 2011", "(Remastered)", etc.
  const _REMASTER_RE = /\s*[-–—]\s*remaster(?:ed)?(?:\s+\d{4})?|\s*\(remaster(?:ed)?(?:\s+\d{4})?\)|\s*\[remaster(?:ed)?(?:\s+\d{4})?\]/gi;
  // Matches featuring credits: "(feat. Artist)", "(ft. Artist)"
  const _FEAT_RE = /\s*\((?:feat|ft)\.?\s+[^)]*\)/gi;
  // Matches trailing year patterns: "(2011)" or "[2011]"
  const _YEAR_SUFFIX_RE = /\s*[(\[]\d{4}[)\]]$/;
  // Collapses whitespace
  const _WHITESPACE_RE = /\s+/g;
  // Variant keywords at word boundaries only
  const _VARIANT_RE = /\b(live|remix|acoustic|music\s+video)\b/i;

  /**
   * Generate a normalized deduplication key from artist + title.
   * Mirrors bot/search/deduplicator.py normalize_key().
   */
  function normalizeKey(artist, title) {
    title = (title || '').replace(_REMASTER_RE, '');
    title = title.replace(_FEAT_RE, '');
    title = title.replace(_YEAR_SUFFIX_RE, '');
    artist = (artist || '').toLowerCase().replace(_WHITESPACE_RE, ' ').trim();
    title = title.toLowerCase().replace(_WHITESPACE_RE, ' ').trim();
    return `${artist}:${title}`;
  }

  /**
   * Detect variant type from a title string.
   * Returns 'live', 'remix', 'acoustic', or 'music_video', or null.
   */
  function detectVariant(title) {
    const match = _VARIANT_RE.exec(title || '');
    if (match) {
      return match[1].toLowerCase().replace(/\s+/g, '_');
    }
    return null;
  }

  /**
   * Compute the dedup key for a result object.
   * - If result has ISRC and no variant_type → key = isrc
   * - If result has ISRC and variant_type → key = `${isrc}:${variant_type}`
   * - No ISRC → key = normalizeKey(artist, title) (with variant suffix if applicable)
   */
  function computeDedupKey(result) {
    const variantType = result.variant_type || detectVariant(result.title);
    let base;
    if (result.isrc) {
      base = result.isrc;
    } else {
      base = normalizeKey(result.artist, result.title);
    }
    if (variantType) {
      return `${base}:${variantType}`;
    }
    return base;
  }

  /**
   * Get the numeric priority for a provider (lower = higher priority).
   */
  function getProviderPriority(provider) {
    return PROVIDER_PRIORITY[provider] !== undefined ? PROVIDER_PRIORITY[provider] : 99;
  }

  // --- Utility: debounce ---
  function debounce(fn, ms) {
    let timer = null;
    const debounced = (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), ms);
    };
    debounced.cancel = () => clearTimeout(timer);
    return debounced;
  }

  // --- Utility: format duration ---
  function formatDuration(ms) {
    if (ms == null || ms <= 0) return '';
    const totalSec = Math.floor(ms / 1000);
    const h = Math.floor(totalSec / 3600);
    const m = Math.floor((totalSec % 3600) / 60);
    const s = totalSec % 60;
    if (h > 0) {
      return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    }
    return `${m}:${String(s).padStart(2, '0')}`;
  }

  /**
   * SearchPanel — manages the search UI inside the Activity iframe.
   *
   * @param {object} opts
   * @param {HTMLElement} opts.container - Parent element to render into
   * @param {function} opts.wsSend - Function to send WebSocket messages (accepts object, stringifies internally)
   * @param {function} [opts.onPlayRequest] - Optional callback when a track is played
   */
  class SearchPanel {
    constructor({ container, wsSend, onPlayRequest }) {
      this.container = container;
      this.wsSend = wsSend;
      this.onPlayRequest = onPlayRequest || null;

      // State
      this.active = false;
      this.currentRequestId = null;
      this.pendingProviders = new Set();
      this.results = []; // Array of result objects from server
      this.trackGroups = new Map(); // dedup key → { primary, variants, groupEl, variantsEl, expandBtn }
      this.queue = []; // Current queue entries
      this.filters = {
        provider: 'all',
        content_type: 'tracks',
        sort_order: 'relevance',
      };

      // Context menu state
      this._contextMenuTarget = null;

      // Build DOM
      this._buildDOM();
      this._buildContextMenu();
      this._buildQueueSection();

      // Debounced search trigger
      this._debouncedSearch = debounce(() => this._executeSearch(), 300);
    }

    // --- Public API ---

    /** Activate search mode (show panel, focus input) */
    activate() {
      if (this.active) return;
      this.active = true;
      this.el.style.display = '';
      this.el.classList.add('search-panel-visible');
      // Autofocus input after a frame to ensure visibility
      requestAnimationFrame(() => {
        this.inputEl.focus();
      });
      // Request current queue state from backend
      this.wsSend({ type: 'queue_state_request' });
    }

    /** Deactivate search mode (hide panel, cancel pending) */
    deactivate() {
      if (!this.active) return;
      this.active = false;
      this.el.classList.remove('search-panel-visible');
      this.el.style.display = 'none';
      this._hideContextMenu();
      this._cancelPendingSearch();
      this._clearResults();
      this.inputEl.value = '';
    }

    /** Toggle search mode on/off */
    toggle() {
      if (this.active) {
        this.deactivate();
      } else {
        this.activate();
      }
    }

    /** Handle incoming WebSocket message. Returns true if consumed. */
    handleMessage(data) {
      switch (data.type) {
        case 'search_partial_result':
          return this._handlePartialResult(data);
        case 'search_complete':
          return this._handleComplete(data);
        case 'search_error':
          return this._handleError(data);
        case 'search_play_ack':
          return this._handlePlayAck(data);
        case 'search_enqueue_ack':
          return this._handleEnqueueAck(data);
        case 'queue_update':
          return this._handleQueueUpdate(data);
        default:
          return false;
      }
    }

    // --- DOM Construction ---

    _buildDOM() {
      this.el = document.createElement('div');
      this.el.className = 'search-panel';
      this.el.style.display = 'none';

      // Header with close button
      const header = document.createElement('div');
      header.className = 'search-panel-header';

      this.inputEl = document.createElement('input');
      this.inputEl.type = 'text';
      this.inputEl.className = 'search-panel-input';
      this.inputEl.placeholder = 'Search tracks...';
      this.inputEl.setAttribute('autocomplete', 'off');
      this.inputEl.setAttribute('aria-label', 'Search tracks');

      const closeBtn = document.createElement('button');
      closeBtn.className = 'search-panel-close';
      closeBtn.textContent = '✕';
      closeBtn.title = 'Close search';
      closeBtn.setAttribute('aria-label', 'Close search');
      closeBtn.addEventListener('click', () => this.deactivate());

      header.appendChild(this.inputEl);
      header.appendChild(closeBtn);

      // Filter bar
      this.filterBar = this._buildFilterBar();

      // Loading indicator
      this.loadingEl = document.createElement('div');
      this.loadingEl.className = 'search-panel-loading';
      this.loadingEl.style.display = 'none';
      this.loadingEl.innerHTML = '<div class="search-spinner"></div><span>Searching...</span>';

      // Provider badges loading row
      this.providerStatusEl = document.createElement('div');
      this.providerStatusEl.className = 'search-provider-status';
      this.providerStatusEl.style.display = 'none';

      // Error message
      this.errorEl = document.createElement('div');
      this.errorEl.className = 'search-panel-error';
      this.errorEl.style.display = 'none';

      // Results container
      this.resultsEl = document.createElement('div');
      this.resultsEl.className = 'search-panel-results';
      this.resultsEl.setAttribute('role', 'list');
      this.resultsEl.setAttribute('aria-label', 'Search results');

      // Assemble
      this.el.appendChild(header);
      this.el.appendChild(this.filterBar);
      this.el.appendChild(this.providerStatusEl);
      this.el.appendChild(this.loadingEl);
      this.el.appendChild(this.errorEl);
      this.el.appendChild(this.resultsEl);

      this.container.appendChild(this.el);

      // Input event handlers
      this.inputEl.addEventListener('input', () => this._onInputChange());
      this.inputEl.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          this.deactivate();
        }
      });
    }

    _buildFilterBar() {
      const bar = document.createElement('div');
      bar.className = 'search-filters';
      bar.setAttribute('role', 'toolbar');
      bar.setAttribute('aria-label', 'Search filters');

      // Provider filter group
      this._providerGroup = this._createFilterGroup(
        'Provider',
        [
          { value: 'all', label: 'All' },
          { value: 'spotify', label: 'Spotify' },
          { value: 'tidal', label: 'Tidal' },
          { value: 'youtube', label: 'YouTube' },
          { value: 'soundcloud', label: 'SoundCloud' },
        ],
        this.filters.provider,
        (value) => {
          this.filters.provider = value;
          this._onFilterChange();
        }
      );

      // Content type filter group
      this._typeGroup = this._createFilterGroup(
        'Type',
        [
          { value: 'tracks', label: 'Tracks' },
          { value: 'albums', label: 'Albums' },
          { value: 'playlists', label: 'Playlists' },
          { value: 'videos', label: 'Videos' },
        ],
        this.filters.content_type,
        (value) => {
          this.filters.content_type = value;
          this._onFilterChange();
        }
      );

      // Sort order filter group
      this._sortGroup = this._createFilterGroup(
        'Sort',
        [
          { value: 'relevance', label: 'Relevance' },
          { value: 'duration', label: 'Duration' },
          { value: 'year', label: 'Year' },
        ],
        this.filters.sort_order,
        (value) => {
          this.filters.sort_order = value;
          this._onFilterChange();
        }
      );

      bar.appendChild(this._providerGroup);
      bar.appendChild(this._typeGroup);
      bar.appendChild(this._sortGroup);

      return bar;
    }

    _createFilterGroup(label, options, defaultValue, onChange) {
      const group = document.createElement('div');
      group.className = 'search-filter-group';
      group.setAttribute('role', 'radiogroup');
      group.setAttribute('aria-label', label);

      for (const opt of options) {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'search-filter-btn';
        btn.textContent = opt.label;
        btn.dataset.value = opt.value;
        btn.setAttribute('role', 'radio');
        btn.setAttribute('aria-checked', opt.value === defaultValue ? 'true' : 'false');
        if (opt.value === defaultValue) {
          btn.classList.add('active');
        }
        btn.addEventListener('click', () => {
          // Deactivate siblings
          group.querySelectorAll('.search-filter-btn').forEach((sibling) => {
            sibling.classList.remove('active');
            sibling.setAttribute('aria-checked', 'false');
          });
          // Activate this one
          btn.classList.add('active');
          btn.setAttribute('aria-checked', 'true');
          onChange(opt.value);
        });
        group.appendChild(btn);
      }

      return group;
    }

    // --- Input & Search Logic ---

    _onInputChange() {
      const query = this.inputEl.value.trim();
      if (query.length < 2) {
        // Cancel any pending search and clear results
        this._debouncedSearch.cancel();
        this._cancelPendingSearch();
        this._clearResults();
        return;
      }
      // Debounce the search
      this._debouncedSearch();
    }

    _onFilterChange() {
      const query = this.inputEl.value.trim();
      if (query.length >= 2) {
        // Re-issue search immediately with new filters
        this._debouncedSearch.cancel();
        this._executeSearch();
      }
    }

    _executeSearch() {
      const query = this.inputEl.value.trim();
      if (query.length < 2) return;

      // Cancel previous search
      this._cancelPendingSearch();

      // Generate new request ID
      const requestId = crypto.randomUUID();
      this.currentRequestId = requestId;

      // Clear previous results and show loading
      this._clearResults();
      this._showLoading(true);
      this._hideError();

      // Determine which providers to show spinners for
      if (this.filters.provider === 'all') {
        this.pendingProviders = new Set(PROVIDER_ORDER);
      } else {
        this.pendingProviders = new Set([this.filters.provider]);
      }
      this._updateProviderStatus();

      // Send search request via WebSocket
      this.wsSend({
        type: 'search_request',
        query: query,
        request_id: requestId,
        filters: {
          provider: this.filters.provider,
          content_type: this.filters.content_type,
          sort_order: this.filters.sort_order,
        },
      });
    }

    _cancelPendingSearch() {
      if (this.currentRequestId) {
        // Send cancel to server
        this.wsSend({
          type: 'search_cancel',
          request_id: this.currentRequestId,
        });
        this.currentRequestId = null;
      }
      this.pendingProviders.clear();
      this._showLoading(false);
      this._updateProviderStatus();
    }

    // --- WebSocket Message Handlers ---

    _handlePartialResult(data) {
      // Discard stale results
      if (data.request_id !== this.currentRequestId) return true;

      const provider = data.provider;
      // Remove from pending set
      this.pendingProviders.delete(provider);
      this._updateProviderStatus();

      // Process results into Track_Groups progressively
      if (data.results && data.results.length > 0) {
        for (const result of data.results) {
          this.results.push(result);
          this._processResultIntoGroup(result);
        }
      }

      // Hide main loading once we have any results
      if (this.results.length > 0) {
        this._showLoading(false);
      }

      return true;
    }

    _handleComplete(data) {
      if (data.request_id !== this.currentRequestId) return true;

      // All done — remove all loading indicators
      this.pendingProviders.clear();
      this._showLoading(false);
      this._updateProviderStatus();

      // If no results at all, show a message
      if (this.results.length === 0) {
        this._showNoResults();
      }

      return true;
    }

    _handleError(data) {
      if (data.request_id !== this.currentRequestId) return true;

      this.pendingProviders.clear();
      this._showLoading(false);
      this._updateProviderStatus();
      this._showError(data.message || 'Search failed');

      return true;
    }

    _handlePlayAck(data) {
      // Correlate with pending play request for badge-level feedback
      if (this._pendingPlayRequests && data.request_id) {
        const pending = this._pendingPlayRequests.get(data.request_id);
        if (pending) {
          this._pendingPlayRequests.delete(data.request_id);
          const { badge, row } = pending;

          // Remove loading pulse
          badge.classList.remove('loading');

          if (data.success) {
            // Success: brief green flash on the row
            this._showRowConfirmation(row, '▶ Playing');
          } else {
            // Error: brief red flash on the badge and show error
            badge.classList.add('unavailable');
            setTimeout(() => badge.classList.remove('unavailable'), 2000);
            this._showError(`Playback failed: ${data.message || 'Unknown error'}`);
            setTimeout(() => this._hideError(), 3000);
          }
          return true;
        }
      }

      // Fallback for play requests not tracked (e.g., row clicks)
      if (!data.success) {
        this._showError(`Playback failed: ${data.message || 'Unknown error'}`);
        setTimeout(() => this._hideError(), 3000);
      }
      return true;
    }

    _handleEnqueueAck(data) {
      if (data.success) {
        // Show transient confirmation
        this._showEnqueueConfirmation(data.track_title, data.position);
        // Add to local queue display if we have track info
        if (data.track_title) {
          this.queue.push({
            title: data.track_title,
            artist: data.track_artist || '',
            duration_ms: data.track_duration_ms || null,
            provider: data.track_provider || '',
            track_id: data.track_id || '',
          });
          this._renderQueue();
        }
      } else {
        // Req 16.6: display error indicator on failure, preserve queue state
        this._showQueueError(`Enqueue failed: ${data.message || 'Unknown error'}`);
      }
      return true;
    }

    // --- Track_Group Logic ---

    /**
     * Process a result into the Track_Group system.
     * - Computes dedup key
     * - If no group exists: creates group with this as primary, renders group DOM
     * - If group exists: adds as variant, potentially swaps primary if higher priority
     */
    _processResultIntoGroup(result) {
      const key = computeDedupKey(result);
      const existingGroup = this.trackGroups.get(key);

      if (!existingGroup) {
        // New group — render as primary entry
        this._createGroup(key, result);
      } else {
        // Group exists — determine if this should be the new primary or a variant
        const existingPriority = getProviderPriority(existingGroup.primary.provider);
        const newPriority = getProviderPriority(result.provider);

        if (newPriority < existingPriority) {
          // New result has higher priority — swap: old primary becomes variant
          const oldPrimary = existingGroup.primary;
          existingGroup.primary = result;
          existingGroup.variants.push(oldPrimary);

          // Re-render the primary row with new result
          this._rerenderPrimaryRow(existingGroup, result);
          // Move old primary content to a variant row
          this._addVariantRow(existingGroup, oldPrimary);
        } else {
          // Existing primary stays — new result is a variant
          existingGroup.variants.push(result);
          this._addVariantRow(existingGroup, result);
        }

        // Update expand control visibility and count
        this._updateExpandControl(existingGroup);

        // Update the primary row's badges to include the new provider
        this._syncPrimaryBadges(existingGroup);
      }
    }

    /**
     * Create a new Track_Group and render its DOM structure.
     */
    _createGroup(key, result) {
      // Group container
      const groupEl = document.createElement('div');
      groupEl.className = 'search-result-group';
      groupEl.setAttribute('data-group-key', key);

      // Primary row
      const primaryRow = this._buildResultRow(result, true);
      primaryRow.classList.add('search-result-primary');

      // Expand button (hidden initially — shown when ≥2 providers)
      const expandBtn = document.createElement('button');
      expandBtn.className = 'search-group-expand';
      expandBtn.style.display = 'none';
      expandBtn.setAttribute('aria-label', 'Show other providers');
      expandBtn.setAttribute('aria-expanded', 'false');
      expandBtn.innerHTML = '<span class="search-group-expand-icon">▼</span>';
      primaryRow.appendChild(expandBtn);

      // Variants container (hidden by default)
      const variantsEl = document.createElement('div');
      variantsEl.className = 'search-group-variants';
      variantsEl.style.display = 'none';

      groupEl.appendChild(primaryRow);
      groupEl.appendChild(variantsEl);

      // Expand toggle handler
      expandBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        this._toggleGroup(key);
      });

      // Store group state
      const group = {
        primary: result,
        variants: [],
        groupEl,
        primaryRow,
        variantsEl,
        expandBtn,
        expanded: false,
      };
      this.trackGroups.set(key, group);

      this.resultsEl.appendChild(groupEl);
    }

    /**
     * Re-render the primary row when a higher-priority result arrives.
     */
    _rerenderPrimaryRow(group, newPrimary) {
      const newRow = this._buildResultRow(newPrimary, true);
      newRow.classList.add('search-result-primary');

      // Preserve the expand button
      newRow.appendChild(group.expandBtn);

      // Replace old primary row in DOM
      group.groupEl.replaceChild(newRow, group.primaryRow);
      group.primaryRow = newRow;
    }

    /**
     * Add a variant row to the group's variants container.
     */
    _addVariantRow(group, result) {
      const row = this._buildResultRow(result, false);
      group.variantsEl.appendChild(row);
    }

    /**
     * Update the expand control's visibility and label.
     * Shows when the group has ≥2 providers total (primary + ≥1 variant).
     */
    _updateExpandControl(group) {
      const variantCount = group.variants.length;
      if (variantCount >= 1) {
        group.expandBtn.style.display = '';
        // Update the text to show "+N providers"
        const countText = `+${variantCount} `;
        group.expandBtn.innerHTML = `${countText}<span class="search-group-expand-icon">${group.expanded ? '▲' : '▼'}</span>`;
        group.expandBtn.setAttribute('aria-label',
          group.expanded ? 'Hide other providers' : `Show ${variantCount} other provider${variantCount > 1 ? 's' : ''}`);
      } else {
        group.expandBtn.style.display = 'none';
      }
    }

    /**
     * Sync the primary row's badges container to reflect ALL providers in the group.
     * Called when a new variant arrives so the primary row shows badges for all available providers.
     */
    _syncPrimaryBadges(group) {
      const row = group.primaryRow;
      if (!row) return;

      const badgesContainer = row.querySelector('.search-provider-badges');
      if (!badgesContainer) return;

      // Collect all providers and their track_ids from the group
      const allProviders = new Map();
      allProviders.set(group.primary.provider, group.primary.track_id);
      for (const variant of group.variants) {
        allProviders.set(variant.provider, variant.track_id);
      }

      // For each provider in priority order, ensure a badge exists
      for (const providerName of PROVIDER_ORDER) {
        if (!allProviders.has(providerName)) continue;

        // Skip if badge already exists
        if (badgesContainer.querySelector(`.search-badge.${providerName}`)) continue;

        const providerTrackId = allProviders.get(providerName);
        const isDisplayed = (providerName === group.primary.provider);
        const badge = document.createElement('span');
        badge.className = `search-badge ${providerName} ${isDisplayed ? 'available' : 'other'}`;
        badge.title = `Play on ${PROVIDERS[providerName].label}`;
        badge.setAttribute('aria-label', `Play on ${PROVIDERS[providerName].label}`);
        badge.dataset.provider = providerName;
        badge.dataset.trackId = providerTrackId;

        badge.addEventListener('click', (e) => {
          e.stopPropagation();
          this._playViaProvider(providerName, providerTrackId, badge, row);
        });

        // Insert in priority order
        const existingBadges = Array.from(badgesContainer.querySelectorAll('.search-badge'));
        const newPriority = getProviderPriority(providerName);
        let inserted = false;
        for (const existing of existingBadges) {
          const existingPriority = getProviderPriority(existing.dataset.provider);
          if (newPriority < existingPriority) {
            badgesContainer.insertBefore(badge, existing);
            inserted = true;
            break;
          }
        }
        if (!inserted) {
          badgesContainer.appendChild(badge);
        }
      }
    }

    /**
     * Toggle expand/collapse state for a Track_Group.
     */
    _toggleGroup(key) {
      const group = this.trackGroups.get(key);
      if (!group) return;

      group.expanded = !group.expanded;
      group.variantsEl.style.display = group.expanded ? '' : 'none';
      group.expandBtn.setAttribute('aria-expanded', String(group.expanded));
      this._updateExpandControl(group);

      // Toggle class on group element for CSS animation hooks
      if (group.expanded) {
        group.groupEl.classList.add('search-result-group-expanded');
      } else {
        group.groupEl.classList.remove('search-result-group-expanded');
      }
    }

    // --- Rendering ---

    /**
     * Build a result row element (used for both primary and variant rows).
     * @param {object} result - The search result data
     * @param {boolean} isPrimary - Whether this is a primary (clickable to play) row
     * @returns {HTMLElement} The row element
     */
    _buildResultRow(result, isPrimary) {
      const row = document.createElement('div');
      row.className = 'search-result-row';
      if (!isPrimary) {
        row.classList.add('search-result-variant');
      }
      row.setAttribute('role', 'listitem');

      // Album art
      const art = document.createElement('img');
      art.className = 'search-result-art';
      art.width = 48;
      art.height = 48;
      art.alt = '';
      const placeholderSvg = 'data:image/svg+xml,' + encodeURIComponent(
        '<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48">' +
        '<rect width="48" height="48" fill="#333"/>' +
        '<text x="24" y="28" text-anchor="middle" fill="#666" font-size="18">♪</text></svg>'
      );
      if (result.artwork_url) {
        art.src = result.artwork_url;
        art.addEventListener('error', () => { art.src = placeholderSvg; });
      } else {
        art.src = placeholderSvg;
      }

      // Info container
      const info = document.createElement('div');
      info.className = 'search-result-info';

      const titleEl = document.createElement('div');
      titleEl.className = 'search-result-title';
      titleEl.textContent = result.title || 'Unknown';

      const metaEl = document.createElement('div');
      metaEl.className = 'search-result-meta';
      const metaParts = [];
      if (result.artist) metaParts.push(result.artist);
      if (result.album) metaParts.push(result.album);
      if (result.release_year) metaParts.push(String(result.release_year));
      metaEl.textContent = metaParts.join(' · ');

      info.appendChild(titleEl);
      info.appendChild(metaEl);

      // Right side: duration + provider badges
      const right = document.createElement('div');
      right.className = 'search-result-right';

      if (result.duration_ms) {
        const durEl = document.createElement('span');
        durEl.className = 'search-result-duration';
        durEl.textContent = formatDuration(result.duration_ms);
        right.appendChild(durEl);
      }

      // Provider badges — show ALL available providers for this entry
      const badgesContainer = document.createElement('div');
      badgesContainer.className = 'search-provider-badges';

      // Build provider→track_id map from group_providers (server-side dedup data)
      const groupProviders = result.group_providers || [];
      const providerTrackMap = new Map();

      // Always include the current result's own provider
      providerTrackMap.set(result.provider, result.track_id);

      // Add additional providers from server-side Track_Group data
      for (const gp of groupProviders) {
        if (gp.provider && gp.track_id) {
          providerTrackMap.set(gp.provider, gp.track_id);
        }
      }

      // Render badges in priority order
      for (const providerName of PROVIDER_ORDER) {
        if (!providerTrackMap.has(providerName)) continue;

        const providerTrackId = providerTrackMap.get(providerName);
        const isDisplayed = (providerName === result.provider);
        const badge = document.createElement('span');
        badge.className = `search-badge ${providerName} ${isDisplayed ? 'available' : 'other'}`;
        badge.title = `Play on ${PROVIDERS[providerName].label}`;
        badge.setAttribute('aria-label', `Play on ${PROVIDERS[providerName].label}`);
        badge.dataset.provider = providerName;
        badge.dataset.trackId = providerTrackId;

        // Click badge → send search_play for that provider's version
        badge.addEventListener('click', (e) => {
          e.stopPropagation(); // Don't trigger row click
          this._playViaProvider(providerName, providerTrackId, badge, row);
        });

        badgesContainer.appendChild(badge);
      }

      right.appendChild(badgesContainer);

      // Assemble row
      row.appendChild(art);
      row.appendChild(info);
      row.appendChild(right);

      // Click to play
      row.addEventListener('click', (e) => {
        // Don't play if context menu is visible (user might be dismissing it)
        if (this.contextMenuEl && this.contextMenuEl.classList.contains('visible')) return;
        this._playTrack(result);
      });

      // Right-click → show context menu with "Add to Queue"
      row.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        this._showContextMenu(e, result);
      });

      // Long-press support for touch (500ms) → show context menu
      let longPressTimer = null;
      row.addEventListener('pointerdown', (e) => {
        if (e.button !== 0) return;
        longPressTimer = setTimeout(() => {
          longPressTimer = null;
          this._showContextMenu(e, result);
        }, 500);
      });
      row.addEventListener('pointerup', () => {
        if (longPressTimer) {
          clearTimeout(longPressTimer);
          longPressTimer = null;
        }
      });
      row.addEventListener('pointerleave', () => {
        if (longPressTimer) {
          clearTimeout(longPressTimer);
          longPressTimer = null;
        }
      });

      return row;
    }

    /**
     * Legacy method: renders a single result row directly into resultsEl.
     * Delegates to _buildResultRow. The Track_Group path (_processResultIntoGroup)
     * is preferred for progressive rendering.
     */
    _renderResultRow(result) {
      const row = this._buildResultRow(result, true);
      this.resultsEl.appendChild(row);
    }

    // --- Actions ---

    _playTrack(result) {
      const requestId = crypto.randomUUID();
      this.wsSend({
        type: 'search_play',
        provider: result.provider,
        track_id: result.track_id,
        request_id: requestId,
      });
      if (this.onPlayRequest) {
        this.onPlayRequest(result);
      }
    }

    /**
     * Play a specific provider's version of a track (triggered by badge click).
     * Shows loading state on the badge and tracks the request for ack handling.
     */
    _playViaProvider(provider, trackId, badgeEl, rowEl) {
      const requestId = crypto.randomUUID();

      // Store the pending play request for ack correlation
      this._pendingPlayRequests = this._pendingPlayRequests || new Map();
      this._pendingPlayRequests.set(requestId, { badge: badgeEl, row: rowEl, provider });

      // Visual feedback: pulse the badge while waiting
      badgeEl.classList.add('loading');

      this.wsSend({
        type: 'search_play',
        provider: provider,
        track_id: trackId,
        request_id: requestId,
      });

      if (this.onPlayRequest) {
        this.onPlayRequest({ provider, track_id: trackId });
      }
    }

    _enqueueTrack(result) {
      this.wsSend({
        type: 'search_enqueue',
        provider: result.provider,
        track_id: result.track_id,
        request_id: crypto.randomUUID(),
      });
    }

    // --- UI State Management ---

    _clearResults() {
      this.results = [];
      this.trackGroups.clear();
      this.resultsEl.innerHTML = '';
    }

    _showLoading(show) {
      this.loadingEl.style.display = show ? '' : 'none';
    }

    _showError(message) {
      this.errorEl.textContent = message;
      this.errorEl.style.display = '';
    }

    _hideError() {
      this.errorEl.style.display = 'none';
      this.errorEl.textContent = '';
    }

    _showNoResults() {
      const msg = document.createElement('div');
      msg.className = 'search-no-results';
      msg.textContent = 'No results found';
      this.resultsEl.appendChild(msg);
    }

    _showEnqueueConfirmation(title, position) {
      const toast = document.createElement('div');
      toast.className = 'search-enqueue-toast';
      toast.textContent = `Added "${title || 'track'}" to queue (#${position || '?'})`;
      this.el.appendChild(toast);
      // Remove after 2 seconds
      setTimeout(() => {
        toast.classList.add('search-enqueue-toast-fade');
        setTimeout(() => toast.remove(), 300);
      }, 2000);
    }

    /**
     * Show a transient play-confirmation overlay on a result row.
     */
    _showRowConfirmation(rowEl, message) {
      if (!rowEl || !rowEl.parentNode) return;
      const confirm = document.createElement('div');
      confirm.className = 'search-result-confirm';
      confirm.textContent = message || '▶ Playing';
      rowEl.style.position = 'relative';
      rowEl.appendChild(confirm);
      // Auto-remove after animation completes (2s per CSS)
      setTimeout(() => confirm.remove(), 2100);
    }

    // --- Context Menu ---

    _buildContextMenu() {
      this.contextMenuEl = document.createElement('div');
      this.contextMenuEl.className = 'search-context-menu';
      this.contextMenuEl.innerHTML = `
        <div class="search-context-menu-item" data-action="enqueue">
          <span class="search-context-menu-item-icon">📋</span>
          Add to Queue
        </div>
      `;
      document.body.appendChild(this.contextMenuEl);

      // Handle menu item clicks
      this.contextMenuEl.addEventListener('click', (e) => {
        const item = e.target.closest('.search-context-menu-item');
        if (!item) return;
        const action = item.dataset.action;
        if (action === 'enqueue' && this._contextMenuTarget) {
          this._enqueueTrack(this._contextMenuTarget);
        }
        this._hideContextMenu();
      });

      // Dismiss context menu on outside click
      document.addEventListener('click', (e) => {
        if (!this.contextMenuEl.contains(e.target)) {
          this._hideContextMenu();
        }
      });

      // Dismiss on Escape
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
          this._hideContextMenu();
        }
      });
    }

    _showContextMenu(event, result) {
      this._contextMenuTarget = result;

      // Position at pointer location
      const x = event.clientX || event.pageX || 0;
      const y = event.clientY || event.pageY || 0;

      this.contextMenuEl.style.left = `${x}px`;
      this.contextMenuEl.style.top = `${y}px`;
      this.contextMenuEl.classList.add('visible');

      // Ensure menu stays within viewport
      requestAnimationFrame(() => {
        const rect = this.contextMenuEl.getBoundingClientRect();
        const vw = window.innerWidth;
        const vh = window.innerHeight;

        if (rect.right > vw) {
          this.contextMenuEl.style.left = `${vw - rect.width - 8}px`;
        }
        if (rect.bottom > vh) {
          this.contextMenuEl.style.top = `${vh - rect.height - 8}px`;
        }
      });
    }

    _hideContextMenu() {
      this.contextMenuEl.classList.remove('visible');
      this._contextMenuTarget = null;
    }

    // --- Queue Section ---

    _buildQueueSection() {
      this.queueEl = document.createElement('div');
      this.queueEl.className = 'search-queue';
      this.queueEl.style.display = 'none';

      // Queue header
      const header = document.createElement('div');
      header.className = 'search-queue-header';
      header.innerHTML = `
        <span>Queue</span>
        <span class="search-queue-count"></span>
      `;
      this.queueCountEl = header.querySelector('.search-queue-count');

      // Queue list (scrollable, holds queue items)
      this.queueListEl = document.createElement('div');
      this.queueListEl.className = 'search-queue-list';
      this.queueListEl.setAttribute('role', 'list');
      this.queueListEl.setAttribute('aria-label', 'Playback queue');

      this.queueEl.appendChild(header);
      this.queueEl.appendChild(this.queueListEl);

      // Append queue section to the search panel (below results)
      this.el.appendChild(this.queueEl);
    }

    _handleQueueUpdate(data) {
      // Full queue state sync from backend
      if (data.queue && Array.isArray(data.queue)) {
        this.queue = data.queue;
        this._renderQueue();
      }
      return true;
    }

    _renderQueue() {
      this.queueListEl.innerHTML = '';

      if (this.queue.length === 0) {
        this.queueEl.style.display = 'none';
        return;
      }

      this.queueEl.style.display = '';
      this.queueCountEl.textContent = `${this.queue.length} track${this.queue.length !== 1 ? 's' : ''}`;

      this.queue.forEach((entry, index) => {
        const item = this._createQueueItem(entry, index);
        this.queueListEl.appendChild(item);
      });
    }

    _createQueueItem(entry, index) {
      const item = document.createElement('div');
      item.className = 'search-queue-item';
      item.setAttribute('role', 'listitem');
      item.setAttribute('draggable', 'true');
      item.dataset.index = index;
      item.dataset.trackId = entry.track_id || '';

      // Drag handle
      const handle = document.createElement('span');
      handle.className = 'drag-handle';
      handle.textContent = '⠿';
      handle.setAttribute('aria-label', 'Drag to reorder');

      // Title
      const title = document.createElement('span');
      title.className = 'queue-item-title';
      title.textContent = entry.title || 'Unknown';

      // Artist
      const artist = document.createElement('span');
      artist.className = 'queue-item-artist';
      artist.textContent = entry.artist || '';

      // Duration
      const duration = document.createElement('span');
      duration.className = 'queue-item-duration';
      duration.textContent = entry.duration_ms ? formatDuration(entry.duration_ms) : '';

      // Provider icon
      const providerIcon = document.createElement('span');
      providerIcon.className = 'queue-item-provider';
      const prov = entry.provider && PROVIDERS[entry.provider];
      providerIcon.textContent = prov ? prov.icon : '';
      if (prov) providerIcon.title = prov.label;

      item.appendChild(handle);
      item.appendChild(title);
      item.appendChild(artist);
      item.appendChild(duration);
      item.appendChild(providerIcon);

      // --- Drag-to-reorder event listeners ---
      item.addEventListener('dragstart', (e) => {
        item.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', String(index));
        // Minimal drag image
        if (e.dataTransfer.setDragImage) {
          e.dataTransfer.setDragImage(item, 20, 20);
        }
      });

      item.addEventListener('dragend', () => {
        item.classList.remove('dragging');
        // Remove all drag-over indicators
        this.queueListEl.querySelectorAll('.drag-over').forEach(el => {
          el.classList.remove('drag-over');
        });
      });

      item.addEventListener('dragover', (e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        // Add visual indicator
        const dragging = this.queueListEl.querySelector('.dragging');
        if (dragging && dragging !== item) {
          item.classList.add('drag-over');
        }
      });

      item.addEventListener('dragleave', () => {
        item.classList.remove('drag-over');
      });

      item.addEventListener('drop', (e) => {
        e.preventDefault();
        item.classList.remove('drag-over');

        const fromIndex = parseInt(e.dataTransfer.getData('text/plain'), 10);
        const toIndex = parseInt(item.dataset.index, 10);

        if (isNaN(fromIndex) || isNaN(toIndex) || fromIndex === toIndex) return;

        // Reorder the local queue array
        const [moved] = this.queue.splice(fromIndex, 1);
        this.queue.splice(toIndex, 0, moved);

        // Re-render
        this._renderQueue();

        // Send updated order to backend via WebSocket
        const order = this.queue.map(entry => entry.track_id);
        this.wsSend({
          type: 'queue_reorder',
          order: order,
        });
      });

      return item;
    }

    // --- Queue Error Handling ---

    _showQueueError(message) {
      // Display transient error on the queue section
      const errorEl = document.createElement('div');
      errorEl.className = 'search-result-confirm';
      errorEl.style.background = 'hsla(0, 60%, 40%, 0.15)';
      errorEl.style.color = '#f87171';
      errorEl.textContent = message || 'Queue operation failed';
      this.queueEl.style.position = 'relative';
      this.queueEl.appendChild(errorEl);
      setTimeout(() => errorEl.remove(), 3000);
    }

    _updateProviderStatus() {
      if (this.pendingProviders.size === 0) {
        this.providerStatusEl.style.display = 'none';
        this.providerStatusEl.innerHTML = '';
        return;
      }

      this.providerStatusEl.style.display = '';
      this.providerStatusEl.innerHTML = '';

      for (const provider of PROVIDER_ORDER) {
        if (!ALL_PROVIDERS.has(provider)) continue;

        const badge = document.createElement('span');
        badge.className = 'search-status-badge';
        badge.style.backgroundColor = PROVIDERS[provider].color;

        if (this.pendingProviders.has(provider)) {
          // Still loading — show spinner
          badge.classList.add('search-status-pending');
          badge.title = `${PROVIDERS[provider].label}: searching...`;
        } else {
          // Done
          badge.classList.add('search-status-done');
          badge.title = `${PROVIDERS[provider].label}: done`;
        }

        this.providerStatusEl.appendChild(badge);
      }
    }
  }

  // Export
  window.SearchPanel = SearchPanel;
})();
