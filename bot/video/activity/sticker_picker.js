/**
 * StickerPicker — UI panel for browsing and selecting sticker images.
 *
 * Responsibilities:
 * - Fetch sticker catalog from stickers/catalog (cached after first fetch)
 * - Render category tabs with clickable navigation
 * - Render thumbnail grid for the selected category (max 64×64px, aspect ratio preserved)
 * - Notify StickerTool via onSelect callback when a sticker is clicked
 * - Handle fetch failures gracefully with retry on next activation
 */

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

    /** @type {{ categories: Array<{ slug: string, name: string, images: string[] }> } | null} */
    this.catalog = null;
    /** @type {string|null} */
    this.selectedCategory = null;
    /** @type {{ category: string, filename: string } | null} */
    this.selectedSticker = null;

    /** @type {HTMLElement} */
    this._categoriesContainer = container.querySelector('#sticker-picker-categories');
    /** @type {HTMLElement} */
    this._gridContainer = container.querySelector('#sticker-picker-grid');
    /** @type {HTMLElement} */
    this._closeButton = container.querySelector('#sticker-picker-close');

    // Wire close button
    if (this._closeButton) {
      this._closeButton.addEventListener('click', () => this.hide());
    }
  }

  /**
   * Show the picker panel and fetch catalog if not cached.
   * On fetch failure, displays an error message and allows retry on next show().
   */
  async show() {
    if (!this.catalog) {
      try {
        const resp = await fetch('stickers/catalog');
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        this.catalog = await resp.json();
      } catch (_err) {
        // Show unavailable message, allow retry on next activation
        this._gridContainer.innerHTML = '';
        this._categoriesContainer.innerHTML = '';
        this._gridContainer.textContent = 'Stickers unavailable';
        this.container.style.display = 'block';
        return;
      }
    }

    // Default to first category if none selected or selection no longer valid
    if (!this.selectedCategory || !this._findCategory(this.selectedCategory)) {
      const first = this.catalog.categories[0];
      this.selectedCategory = first ? first.slug : null;
    }

    this.renderCategories();
    this.container.style.display = 'block';
  }

  /**
   * Hide the picker panel.
   */
  hide() {
    this.container.style.display = 'none';
  }

  /**
   * Render category tabs as clickable buttons.
   * Highlights the currently selected category.
   * Selecting a category renders its thumbnails.
   */
  renderCategories() {
    this._categoriesContainer.innerHTML = '';

    if (!this.catalog) return;

    for (const category of this.catalog.categories) {
      const btn = document.createElement('button');
      btn.className = 'sticker-category-btn';
      btn.textContent = category.name;
      btn.dataset.slug = category.slug;

      if (category.slug === this.selectedCategory) {
        btn.classList.add('active');
      }

      btn.addEventListener('click', () => {
        this.selectedCategory = category.slug;
        this.renderCategories();
      });

      this._categoriesContainer.appendChild(btn);
    }

    // Render thumbnails for the selected category
    const selected = this._findCategory(this.selectedCategory);
    if (selected) {
      this.renderThumbnails(selected.slug, selected.images);
    }
  }

  /**
   * Render thumbnail grid for a category.
   * Each thumbnail is max 64×64 CSS pixels, preserving aspect ratio.
   * Image src: stickers/{category_slug}/{filename}
   * On click: set selectedSticker and call onSelect callback.
   *
   * @param {string} categorySlug
   * @param {string[]} images
   */
  renderThumbnails(categorySlug, images) {
    this._gridContainer.innerHTML = '';

    for (const filename of images) {
      const img = document.createElement('img');
      img.src = `stickers/${categorySlug}/${filename}`;
      img.alt = filename;
      img.className = 'sticker-thumbnail';
      img.style.maxWidth = '64px';
      img.style.maxHeight = '64px';
      img.style.objectFit = 'contain';
      img.style.cursor = 'pointer';

      // Highlight if this is the currently selected sticker
      if (
        this.selectedSticker &&
        this.selectedSticker.category === categorySlug &&
        this.selectedSticker.filename === filename
      ) {
        img.classList.add('selected');
      }

      img.addEventListener('click', () => {
        this.selectedSticker = { category: categorySlug, filename };
        this.onSelect(categorySlug, filename);
        // Update highlight
        this.renderThumbnails(categorySlug, images);
      });

      this._gridContainer.appendChild(img);
    }
  }

  /**
   * Find a category in the catalog by slug.
   * @param {string|null} slug
   * @returns {{ slug: string, name: string, images: string[] } | undefined}
   */
  _findCategory(slug) {
    if (!this.catalog || !slug) return undefined;
    return this.catalog.categories.find((c) => c.slug === slug);
  }
}
