"""Tests for StickerCatalog — verifies local-disk vs CDN image serving.

The catalog metadata (categories/search/pagination) is always built from a
local manifest.json. Sticker IMAGE bytes are served either from local disk
(on-prem, no CDN) or redirected to a CDN/S3 base URL when
``HELLODJ_STICKER_CDN_URL`` is configured (AWS). These tests lock in that
contract: `base_url` in the catalog response and the redirect-vs-file image
handler behaviour.

The async handler tests drive the aiohttp handlers directly with a minimal
fake request (no aiohttp test-client dependency) — the handlers only read
``request.app`` and ``request.match_info``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web

from video.sticker_catalog import StickerCatalog, handle_sticker_image


def _write_manifest(root: Path) -> None:
    """Create a minimal manifest.json + one on-disk asset under `root`."""
    manifest = {
        "schema_version": 1,
        "sticker_count": 1,
        "animated_count": 0,
        "categories": [{"slug": "blobs", "name": "Blobs"}],
        "stickers": [
            {
                "id": "blob-1",
                "file": "assets/blobs/blob-1.webp",
                "name": "Happy Blob",
                "animated": False,
                "search_text": "happy blob smile",
                "category": "blobs",
            }
        ],
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    asset_dir = root / "assets" / "blobs"
    asset_dir.mkdir(parents=True)
    (asset_dir / "blob-1.webp").write_bytes(b"RIFF....WEBP-fake-bytes")


def _fake_request(catalog: StickerCatalog, category: str, filename: str):
    """A stand-in for aiohttp's Request that the image handler reads from."""
    return SimpleNamespace(
        app={"sticker_catalog": catalog},
        match_info={"category": category, "filename": filename},
    )


# ── catalog metadata ──────────────────────────────────────────────────


def test_load_builds_index(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    cat = StickerCatalog(tmp_path)
    cat.load()

    data = cat.get_catalog()
    assert data["total"] == 1
    assert data["categories"][0]["slug"] == "blobs"
    assert data["categories"][0]["count"] == 1


def test_catalog_base_url_empty_without_cdn(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    cat = StickerCatalog(tmp_path)
    cat.load()
    # No CDN configured → relative serving (empty base_url).
    assert cat.get_catalog()["base_url"] == ""
    assert cat.cdn_base_url is None


def test_catalog_base_url_reports_cdn(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    cat = StickerCatalog(tmp_path, cdn_base_url="https://cdn.example.com/stickers/")
    cat.load()
    # Trailing slash stripped; base_url surfaced for the frontend.
    assert cat.cdn_base_url == "https://cdn.example.com/stickers"
    assert cat.get_catalog()["base_url"] == "https://cdn.example.com/stickers"


def test_image_url_join(tmp_path: Path) -> None:
    cat = StickerCatalog(tmp_path, cdn_base_url="https://cdn.example.com/stickers")
    assert (
        cat.image_url("blobs", "blob-1.webp")
        == "https://cdn.example.com/stickers/blobs/blob-1.webp"
    )


# ── image handler: disk vs CDN redirect ───────────────────────────────


@pytest.mark.asyncio
async def test_image_served_from_disk_without_cdn(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    cat = StickerCatalog(tmp_path)
    cat.load()

    resp = await handle_sticker_image(_fake_request(cat, "blobs", "blob-1.webp"))
    assert isinstance(resp, web.FileResponse)
    assert resp.headers["Content-Type"] == "image/webp"


@pytest.mark.asyncio
async def test_missing_image_is_404_without_cdn(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    cat = StickerCatalog(tmp_path)
    cat.load()

    with pytest.raises(web.HTTPNotFound):
        await handle_sticker_image(_fake_request(cat, "blobs", "nope.webp"))


@pytest.mark.asyncio
async def test_image_redirects_to_cdn(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    cat = StickerCatalog(tmp_path, cdn_base_url="https://cdn.example.com/stickers")
    cat.load()

    with pytest.raises(web.HTTPFound) as exc:
        await handle_sticker_image(_fake_request(cat, "blobs", "blob-1.webp"))
    assert (
        exc.value.location == "https://cdn.example.com/stickers/blobs/blob-1.webp"
    )


@pytest.mark.asyncio
async def test_image_rejects_path_traversal(tmp_path: Path) -> None:
    _write_manifest(tmp_path)
    cat = StickerCatalog(tmp_path, cdn_base_url="https://cdn.example.com/stickers")
    cat.load()

    # Traversal is rejected before any redirect, even in CDN mode.
    with pytest.raises(web.HTTPNotFound):
        await handle_sticker_image(_fake_request(cat, "blobs", "../secret"))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
