"""Sticker catalog for Video Activity whiteboard.

Loads stickers from a manifest.json file produced by prepare-hellodj-stickers.py.
The manifest describes categories, sticker metadata (tags, search text, animated
flag), and file paths relative to the stickers/ directory.

Sticker IMAGE bytes are served one of two ways:

  * From a CDN / object store (AWS S3 behind CloudFront) when a base URL is
    configured via ``HELLODJ_STICKER_CDN_URL``. The ~55 MiB of curated webp
    assets then live in S3 instead of being baked into the bot image, and the
    frontend fetches them directly from the (geo-local, cacheable) CDN. The
    catalog/page/search JSON responses carry a ``base_url`` field so the client
    knows where to load images from, and the legacy image route 302-redirects
    to the CDN so already-serialized whiteboard strokes keep resolving.
  * Directly from local disk (the original behaviour) when no CDN is
    configured — the on-prem deployment bakes ``stickers/`` into the image and
    serves the bytes itself. This fallback keeps both deployment targets
    working from one code path.

The manifest.json metadata is ALWAYS read locally at startup (it is tiny JSON,
not image bytes) to build the in-memory category/search index. Only the image
bytes move to the CDN.

Supports pagination for lazy loading in the frontend picker.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

# Default page size for paginated sticker lists
_DEFAULT_PAGE_SIZE = 30
# Maximum search results to return per query
_MAX_SEARCH_RESULTS = 50


class StickerCatalog:
    """Manifest-based sticker catalog.

    Loads a manifest.json describing all stickers, their categories,
    and metadata. Serves images from disk (no in-memory caching of
    image bytes — let the OS page cache handle that).
    """

    def __init__(self, stickers_dir: Path, cdn_base_url: str | None = None) -> None:
        self._stickers_dir = stickers_dir
        # Optional CDN/object-store base URL for sticker images (e.g.
        # "https://stickers.us-east-1.hellodj.bot" or a CloudFront domain).
        # When set, images are served from here instead of local disk. A
        # trailing slash is stripped so joins are consistent.
        self._cdn_base_url = cdn_base_url.rstrip("/") if cdn_base_url else None
        # category_slug → { name, stickers: [{ id, file, name, animated, ... }] }
        self._categories: dict[str, dict] = {}
        # All stickers flat list for search
        self._all_stickers: list[dict] = []
        # Manifest metadata
        self._meta: dict = {}

    @property
    def cdn_base_url(self) -> str | None:
        """The configured CDN base URL for sticker images, or None (local disk)."""
        return self._cdn_base_url

    def image_url(self, category_slug: str, filename: str) -> str:
        """Return the absolute CDN URL for a sticker image.

        Only meaningful when a CDN base URL is configured. The on-disk layout
        `assets/<category>/<file>` is mirrored under the CDN prefix.
        """
        base = self._cdn_base_url or ""
        return f"{base}/{category_slug}/{filename}"

    def load(self) -> None:
        """Load manifest.json from the stickers directory."""
        manifest_path = self._stickers_dir / "manifest.json"

        if not manifest_path.is_file():
            logger.warning("Sticker manifest not found: %s", manifest_path)
            return

        try:
            with manifest_path.open("r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load sticker manifest: %s", exc)
            return

        if manifest.get("schema_version") != 1:
            logger.warning(
                "Unsupported sticker manifest schema version: %s",
                manifest.get("schema_version"),
            )
            return

        self._meta = {
            "sticker_count": manifest.get("sticker_count", 0),
            "animated_count": manifest.get("animated_count", 0),
        }

        # Index categories
        categories_meta = {c["slug"]: c["name"] for c in manifest.get("categories", [])}

        # Build per-category sticker lists
        cat_stickers: dict[str, list[dict]] = {slug: [] for slug in categories_meta}

        for sticker in manifest.get("stickers", []):
            cat = sticker.get("category")
            if cat not in cat_stickers:
                continue

            entry = {
                "id": sticker["id"],
                "file": sticker["file"],  # e.g. "assets/blobs/blobmoji-blobmoji14-0-1.webp"
                "filename": Path(sticker["file"]).name,
                "name": sticker.get("name", ""),
                "animated": sticker.get("animated", False),
                "search_text": sticker.get("search_text", "").lower(),
                "category": cat,
            }
            cat_stickers[cat].append(entry)
            self._all_stickers.append(entry)

        for slug, name in categories_meta.items():
            stickers = cat_stickers.get(slug, [])
            if stickers:
                self._categories[slug] = {
                    "name": name,
                    "stickers": stickers,
                }

        logger.info(
            "Sticker catalog loaded: %d stickers (%d animated) in %d categories",
            len(self._all_stickers),
            sum(1 for s in self._all_stickers if s["animated"]),
            len(self._categories),
        )

    def get_catalog(self) -> dict:
        """Return lightweight catalog metadata (no sticker lists).

        The frontend uses this to render category tabs. Actual sticker
        data is fetched per-category via the paginated endpoint.
        """
        return {
            "categories": [
                {
                    "slug": slug,
                    "name": data["name"],
                    "count": len(data["stickers"]),
                }
                for slug, data in sorted(self._categories.items())
            ],
            "total": len(self._all_stickers),
            "animated_total": sum(1 for s in self._all_stickers if s["animated"]),
            # Where the frontend should load sticker images from. Empty string
            # means "relative to the Activity base" (local-disk serving); a
            # non-empty value is the CDN/S3 prefix. The client joins this with
            # "/<category>/<filename>".
            "base_url": self._cdn_base_url or "",
        }

    def get_category_page(
        self, category_slug: str, offset: int = 0, limit: int = _DEFAULT_PAGE_SIZE
    ) -> dict | None:
        """Return a page of stickers for a category.

        Returns None if category doesn't exist. Otherwise returns:
        { stickers: [...], has_more: bool, offset: int, total: int }
        """
        cat = self._categories.get(category_slug)
        if cat is None:
            return None

        all_stickers = cat["stickers"]
        total = len(all_stickers)
        page = all_stickers[offset : offset + limit]
        has_more = (offset + limit) < total

        return {
            "stickers": [
                {
                    "filename": s["filename"],
                    "animated": s["animated"],
                    "name": s["name"],
                }
                for s in page
            ],
            "has_more": has_more,
            "offset": offset,
            "total": total,
        }

    def search(self, query: str, limit: int = _MAX_SEARCH_RESULTS) -> list[dict]:
        """Search stickers by query string against search_text field.

        Returns list of { filename, category, name, animated } dicts.
        """
        if not query or not query.strip():
            return []

        terms = query.lower().split()
        results = []

        for sticker in self._all_stickers:
            text = sticker["search_text"]
            if all(term in text for term in terms):
                results.append({
                    "filename": sticker["filename"],
                    "category": sticker["category"],
                    "name": sticker["name"],
                    "animated": sticker["animated"],
                })
                if len(results) >= limit:
                    break

        return results

    def get_image_path(self, category_slug: str, filename: str) -> Path | None:
        """Return the filesystem path for a sticker image, or None if not found."""
        # Validate category exists
        if category_slug not in self._categories:
            return None

        # Security: prevent path traversal
        if "/" in filename or "\\" in filename or ".." in filename:
            return None

        image_path = self._stickers_dir / "assets" / category_slug / filename
        if image_path.is_file():
            return image_path
        return None

    @staticmethod
    def get_content_type(filename: str) -> str:
        """Infer content type from filename extension."""
        ext = Path(filename).suffix.lower()
        return {
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".svg": "image/svg+xml",
        }.get(ext, "application/octet-stream")


# HTTP route handlers (registered in activity_backend.py)


async def handle_sticker_catalog(request: web.Request) -> web.Response:
    """GET /activity/stickers/catalog → lightweight category list."""
    catalog: StickerCatalog = request.app["sticker_catalog"]
    return web.json_response(catalog.get_catalog())


async def handle_sticker_page(request: web.Request) -> web.Response:
    """GET /activity/stickers/category/{slug}?offset=0&limit=30 → paginated sticker list."""
    catalog: StickerCatalog = request.app["sticker_catalog"]
    slug = request.match_info["slug"]

    try:
        offset = int(request.query.get("offset", "0"))
        limit = int(request.query.get("limit", str(_DEFAULT_PAGE_SIZE)))
    except ValueError:
        raise web.HTTPBadRequest(text="offset and limit must be integers")

    # Clamp limit to reasonable range
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    result = catalog.get_category_page(slug, offset=offset, limit=limit)
    if result is None:
        raise web.HTTPNotFound(text="Category not found")

    return web.json_response(result)


async def handle_sticker_search(request: web.Request) -> web.Response:
    """GET /activity/stickers/search?q=term → search results."""
    catalog: StickerCatalog = request.app["sticker_catalog"]
    query = request.query.get("q", "")
    results = catalog.search(query)
    return web.json_response({"results": results, "query": query})


async def handle_sticker_image(request: web.Request) -> web.Response:
    """GET /activity/stickers/{category}/{filename} → sticker image.

    When a CDN base URL is configured the image bytes live in S3/CloudFront, so
    this route 302-redirects there. Up-to-date clients read `base_url` from the
    catalog and fetch the CDN directly (never hitting this route), but the
    redirect keeps legacy clients and already-serialized whiteboard strokes
    (which stored the relative `stickers/<cat>/<file>` URL) resolving. With no
    CDN configured it serves the file from local disk as before.
    """
    catalog: StickerCatalog = request.app["sticker_catalog"]
    category = request.match_info["category"]
    filename = request.match_info["filename"]

    # Security: reject path traversal regardless of serving mode.
    if "/" in filename or "\\" in filename or ".." in filename:
        raise web.HTTPNotFound(text="Sticker not found")

    if catalog.cdn_base_url:
        # Images are served by the CDN; redirect there.
        raise web.HTTPFound(catalog.image_url(category, filename))

    image_path = catalog.get_image_path(category, filename)
    if image_path is None:
        raise web.HTTPNotFound(text="Sticker not found")

    content_type = catalog.get_content_type(filename)
    return web.FileResponse(
        path=image_path,
        headers={
            "Content-Type": content_type,
            "Cache-Control": "public, max-age=604800, immutable",
        },
    )
