"""Sticker catalog for Video Activity whiteboard.

Discovers and serves sticker images from zip archives in the stickers/
directory. Each zip file becomes a category. Category names are derived
from zip filenames with trailing timestamp suffixes stripped.
"""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

from aiohttp import web

_SUPPORTED_EXTENSIONS = {".png", ".gif", ".webp"}

logger = logging.getLogger(__name__)


class StickerCatalog:
    """Discovers and serves sticker images from zip archives.

    Each zip file in the stickers/ directory becomes a category.
    Category name is derived from the zip filename (minus the
    trailing hash/timestamp suffix, e.g. "Stickers - Christmas 2022").
    """

    def __init__(self, stickers_dir: Path) -> None:
        self._stickers_dir = stickers_dir
        # category_slug → {filename: bytes}
        self._cache: dict[str, dict[str, bytes]] = {}
        # category_slug → display_name
        self._categories: dict[str, str] = {}

    def load(self) -> None:
        """Scan stickers/ directory, extract all valid zips into memory cache."""
        if not self._stickers_dir.is_dir():
            logger.warning("Stickers directory not found: %s", self._stickers_dir)
            return

        for zip_path in sorted(self._stickers_dir.glob("*.zip")):
            try:
                self._load_zip(zip_path)
            except (zipfile.BadZipFile, OSError) as exc:
                logger.warning("Skipping corrupt/unreadable zip %s: %s", zip_path.name, exc)

    def _load_zip(self, zip_path: Path) -> None:
        """Extract supported images from a single zip into cache."""
        images: dict[str, bytes] = {}

        with zipfile.ZipFile(zip_path, "r") as zf:
            for entry in zf.namelist():
                # Skip directories and macOS metadata
                if entry.endswith("/") or "/__MACOSX" in entry or entry.startswith("__MACOSX"):
                    continue
                ext = Path(entry).suffix.lower()
                if ext not in _SUPPORTED_EXTENSIONS:
                    continue
                # Use just the filename (no subdirectory path)
                filename = Path(entry).name
                if filename and filename not in images:
                    images[filename] = zf.read(entry)

        if not images:
            logger.warning("Zip %s contains no supported images, skipping", zip_path.name)
            return

        # Derive category name from zip filename
        # Strip the trailing timestamp suffix (e.g. "-20260820T133942Z-1-001")
        raw_name = zip_path.stem
        display_name = re.sub(r"-\d{8}T\d{6}Z(-\d+)*$", "", raw_name).strip()
        slug = self._slugify(display_name)

        self._cache[slug] = images
        self._categories[slug] = display_name

    @staticmethod
    def _slugify(name: str) -> str:
        """Convert display name to URL-safe slug."""
        slug = name.lower().strip()
        slug = re.sub(r"[^a-z0-9]+", "-", slug)
        return slug.strip("-")

    def get_catalog(self) -> dict:
        """Return the full catalog as a JSON-serializable dict."""
        return {
            "categories": [
                {
                    "slug": slug,
                    "name": self._categories[slug],
                    "images": sorted(self._cache[slug].keys()),
                }
                for slug in sorted(self._categories.keys())
            ]
        }

    def get_image(self, category_slug: str, filename: str) -> bytes | None:
        """Return image bytes or None if not found."""
        cat = self._cache.get(category_slug)
        if cat is None:
            return None
        return cat.get(filename)

    def get_content_type(self, filename: str) -> str:
        """Infer content type from filename extension."""
        ext = Path(filename).suffix.lower()
        return {
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }.get(ext, "application/octet-stream")


# HTTP route handlers (registered in activity_backend.py)


async def handle_sticker_catalog(request: web.Request) -> web.Response:
    """GET /activity/stickers/catalog → JSON catalog of all categories."""
    catalog: StickerCatalog = request.app["sticker_catalog"]
    return web.json_response(catalog.get_catalog())


async def handle_sticker_image(request: web.Request) -> web.Response:
    """GET /activity/stickers/{category}/{filename} → sticker image file."""
    catalog: StickerCatalog = request.app["sticker_catalog"]
    category = request.match_info["category"]
    filename = request.match_info["filename"]

    image_data = catalog.get_image(category, filename)
    if image_data is None:
        raise web.HTTPNotFound(text="Sticker not found")

    content_type = catalog.get_content_type(filename)
    return web.Response(
        body=image_data,
        content_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
