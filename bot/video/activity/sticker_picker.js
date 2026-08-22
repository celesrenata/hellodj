/**
 * StickerPicker — UI panel for browsing and selecting sticker images.
 *
 * Lazy-loads sticker thumbnails in pages of 30, using IntersectionObserver
 * to trigger loading more when the user scrolls near the bottom of the grid.
 *
 * Responsibilities:
 * - Fetch lightweight catalog (category names + counts) on first show
 * - Paginate sticker thumbnails per category (GET stickers/category/{slug}?offset=N)
 * - Search stickers across all categories via GET stickers/search?q=term
 * - Show animated badge on animated stickers
 * - Notify StickerTool via onSelect callback when a sticker is clicked
 * - Handle fetch failures gracefully with retry on next activation
 */

const PAGE_SIZE = 30;

export class StickerPicker {
  /**
   * @param {object} options
   * @param {HTMLElement} options.container - The #sticker-picker DOM element
   * @param {(category: string, filename: string) => void} options.onSelect - Callback when a sticker is selected
   */
  constructor({ container, onSelect }) {
    /** @type {HTMLElement} */
    this.container = container;
    /** @type {(category: string, filename: string) => void} */
    this.onSelect = onSelect;

    /** @type {{ categories: Array<{ slug: string, name: string, count: number }>, total: number } | null} */
    this.catalog = null;
    /** @type {string|null} */
    this.selectedCategory = null;
    /** @type {{ category: string, filename: string } | null} */
    this.selectedSticker = null;

    // Pagination state for the current category view
    /** @type {number} */
    this._offset = 0;
    /** @type {boolean} */
    this._hasMore = false;
    /** @type {boolean} */
    this._loadingPage = false;

    // Search state
    /** @type {boolean} */
    this._searching = false;
    /** @type {number|null} */
    this._searchDebounce = null;

    /** @type {IntersectionObserver|null} */
    this._scrollObserver = null;
    /** @type {HTMLElement|null} */
    this._sentinel = null;

    /** @type {HTMLElement} */
    this._searchInput = container.querySelector('#sticker-picker-search');
    /** @type {HTMLElement} */
    this._categoriesContainer = container.querySelector('#sticker-picker-categories');
    /** @type {HTMLElement} */
    this._gridContainer = container.querySelector('#sticker-picker-grid');
    /** @type {HTMLElement} */
    this._closeButton = container.querySelector('#sticker-picker-close');
    /** @type {HTMLElement} */
    this._countLabel = container.querySelector('#sticker-picker-count');

    // Wire close button
    if (this._closeButton) {
      this._closeButton.addEventListener('click', () => this.hide());
    }

    // Wire search input
    if (this._searchInput) {
      this._searchInput.addEventListener('input', () => this._onSearchInput());
    }

    // Set up IntersectionObserver for infinite scroll
    this._scrollObserver = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting && this._hasMore && !this._loadingPage && !this._searching) {
            this._loadNextPage();
          }
        }
      },
      { root: this._gridContainer, rootMargin: '100px' }
    );
  }

  /**
   * Show the picker panel and fetch catalog if not cached.
   */
  async show() {
    if (!this.catalog) {
      try {
        const resp = await fetch('stickers/catalog');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        this.catalog = await resp.json();
      } catch (_err) {
        this._gridContainer.innerHTML = '';
        this._categoriesContainer.innerHTML = '';
        this._gridContainer.textContent = 'Stickers unavailable';
        this.container.style.display = 'flex';
        return;
      }
    }

    // Update count label
    if (this._countLabel && this.catalog) {
      this._countLabel.textContent = `${this.catalog.total} stickers`;
    }

    // Default to first category
    if (!this.selectedCategory || !this._findCategory(this.selectedCategory)) {
      const first = this.catalog.categories[0];
      this.selectedCategory = first ? first.slug : null;
    }

    // Clear search
    if (this._searchInput) this._searchInput.value = '';
    this._searching = false;

    this._renderCategories();
    this._loadCategory(this.selectedCategory);
    this.container.style.display = 'flex';
  }

  /**
   * Hide the picker panel.
   */
  hide() {
    this.container.style.display = 'none';
  }

  // ------------------------------------------------------------------
  // Search
  // ------------------------------------------------------------------

  _onSearchInput() {
    if (this._searchDebounce) clearTimeout(this._searchDebounce);

    const query = this._searchInput.value.trim();
    if (!query) {
      this._searching = false;
      this._loadCategory(this.selectedCategory);
      this._highlightActiveTab();
      return;
    }

    this._searchDebounce = setTimeout(() => this._performSearch(query), 250);
  }

  async _performSearch(query) {
    this._searching = true;
    this._clearGrid();
    this._highlightActiveTab();

    try {
      const resp = await fetch(`stickers/search?q=${encodeURIComponent(query)}`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      this._renderSearchResults(data.results);
    } catch (_err) {
      this._gridContainer.innerHTML =
        '<span style="color: var(--text-muted); font-size: 0.75rem;">Search failed</span>';
    }
  }

  _renderSearchResults(results) {
    this._clearGrid();
    if (results.length === 0) {
      this._gridContainer.innerHTML =
        '<span style="color: var(--text-muted); font-size: 0.75rem;">No stickers found</span>';
      return;
    }
    for (const s of results) {
      this._appendThumbnail(s.category, s.filename, s.animated, s.name);
    }
  }

  // ------------------------------------------------------------------
  // Category browsing (paginated)
  // ------------------------------------------------------------------

  /**
   * Load the first page of a category, resetting pagination state.
   * @param {string|null} slug
   */
  _loadCategory(slug) {
    if (!slug) return;
    this.selectedCategory = slug;
    this._offset = 0;
    this._hasMore = false;
    this._clearGrid();
    this._loadNextPage();
  }

  /**
   * Fetch the next page of stickers for the current category.
   */
  async _loadNextPage() {
    if (this._loadingPage || !this.selectedCategory) return;
    this._loadingPage = true;

    // Capture current category to detect stale responses after tab switch
    const requestCategory = this.selectedCategory;

    try {
      const url = `stickers/category/${requestCategory}?offset=${this._offset}&limit=${PAGE_SIZE}`;
      const resp = await fetch(url);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();

      // Discard response if category changed while request was in flight
      if (this.selectedCategory !== requestCategory) return;

      // Remove old sentinel before appending new items
      this._removeSentinel();

      for (const s of data.stickers) {
        this._appendThumbnail(requestCategory, s.filename, s.animated, s.name);
      }

      this._offset += data.stickers.length;
      this._hasMore = data.has_more;

      // Add sentinel for next page trigger
      if (this._hasMore) {
        this._addSentinel();
      }
    } catch (_err) {
      // Silently fail — user can scroll up and retry
    } finally {
      this._loadingPage = false;
    }
  }

  // ------------------------------------------------------------------
  // Rendering helpers
  // ------------------------------------------------------------------

  _renderCategories() {
    this._categoriesContainer.innerHTML = '';
    if (!this.catalog) return;

    for (const category of this.catalog.categories) {
      const btn = document.createElement('button');
      btn.className = 'sticker-category-btn';
      btn.textContent = category.name;
      btn.dataset.slug = category.slug;
      btn.title = `${category.name} (${category.count})`;

      if (category.slug === this.selectedCategory && !this._searching) {
        btn.classList.add('active');
      }

      btn.addEventListener('click', () => {
        this._searching = false;
        if (this._searchInput) this._searchInput.value = '';
        this._loadCategory(category.slug);
        this._highlightActiveTab();
      });

      this._categoriesContainer.appendChild(btn);
    }
  }

  _highlightActiveTab() {
    for (const btn of this._categoriesContainer.querySelectorAll('.sticker-category-btn')) {
      btn.classList.toggle('active', btn.dataset.slug === this.selectedCategory && !this._searching);
    }
  }

  /**
   * Append a single sticker thumbnail to the grid.
   */
  _appendThumbnail(categorySlug, filename, animated, name) {
    const wrapper = document.createElement('div');
    wrapper.className = 'sticker-thumb-wrapper';

    const img = document.createElement('img');
    img.dataset.src = `stickers/${categorySlug}/${filename}`;
    img.alt = name || filename;
    img.className = 'sticker-thumbnail';
    img.title = name || filename;

    // Use IntersectionObserver for image lazy loading (don't set src until visible)
    this._observeImage(img);

    // Highlight if currently selected
    if (
      this.selectedSticker &&
      this.selectedSticker.category === categorySlug &&
      this.selectedSticker.filename === filename
    ) {
      wrapper.classList.add('selected');
    }

    wrapper.appendChild(img);

    if (animated) {
      const badge = document.createElement('span');
      badge.className = 'sticker-animated-badge';
      badge.textContent = '●';
      badge.title = 'Animated';
      wrapper.appendChild(badge);
    }

    wrapper.addEventListener('click', () => {
      this.selectedSticker = { category: categorySlug, filename };
      this.onSelect(categorySlug, filename);
      for (const el of this._gridContainer.querySelectorAll('.sticker-thumb-wrapper.selected')) {
        el.classList.remove('selected');
      }
      wrapper.classList.add('selected');
    });

    this._gridContainer.appendChild(wrapper);
  }

  /**
   * Lazy-load individual images using a shared IntersectionObserver.
   * Images only get their `src` set when they enter the viewport of the grid.
   */
  _observeImage(img) {
    if (!this._imageObserver) {
      this._imageObserver = new IntersectionObserver(
        (entries, observer) => {
          for (const entry of entries) {
            if (entry.isIntersecting) {
              const el = entry.target;
              if (el.dataset.src) {
                el.src = el.dataset.src;
                delete el.dataset.src;
              }
              observer.unobserve(el);
            }
          }
        },
        { root: this._gridContainer, rootMargin: '200px' }
      );
    }
    this._imageObserver.observe(img);
  }

  /**
   * Add a sentinel div at the end of the grid that triggers loading the next page.
   */
  _addSentinel() {
    this._removeSentinel();
    this._sentinel = document.createElement('div');
    this._sentinel.className = 'sticker-sentinel';
    this._sentinel.style.height = '1px';
    this._sentinel.style.gridColumn = '1 / -1';
    this._gridContainer.appendChild(this._sentinel);
    this._scrollObserver.observe(this._sentinel);
  }

  _removeSentinel() {
    if (this._sentinel) {
      this._scrollObserver.unobserve(this._sentinel);
      this._sentinel.remove();
      this._sentinel = null;
    }
  }

  _clearGrid() {
    this._removeSentinel();
    this._gridContainer.innerHTML = '';
  }

  /**
   * Find a category in the catalog by slug.
   */
  _findCategory(slug) {
    if (!this.catalog || !slug) return undefined;
    return this.catalog.categories.find((c) => c.slug === slug);
  }
}
