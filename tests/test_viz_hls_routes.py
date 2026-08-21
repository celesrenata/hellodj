"""Tests for the visualizer HLS routes in ActivityBackend.

Covers:
- GET /activity/stream/{guild_id}/viz/playlist.m3u8
- GET /activity/stream/{guild_id}/viz/{segment}
- Authentication, path traversal protection, and 404 handling
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the bot directory is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))


# Valid token for guild 12345, channel 67890
VALID_TOKEN = "i-abc123-gc-12345-67890"
# Valid token for a different guild
WRONG_GUILD_TOKEN = "i-abc123-gc-99999-67890"


def _make_backend():
    """Create an ActivityBackend instance with mocked dependencies."""
    mock_registry = MagicMock()
    mock_registry.get.return_value = None
    mock_registry.get_by_guild.return_value = []

    with patch("video.activity_backend.StickerCatalog") as mock_catalog_cls:
        mock_catalog = MagicMock()
        mock_catalog_cls.return_value = mock_catalog
        from video.activity_backend import ActivityBackend

        backend = ActivityBackend(mock_registry)
    return backend


def _make_request(
    guild_id: str = "12345",
    token: str | None = None,
    segment: str | None = None,
    query_token: str | None = None,
) -> MagicMock:
    """Create a mock aiohttp Request with match_info and headers."""
    request = MagicMock()
    match_info = {"guild_id": guild_id}
    if segment is not None:
        match_info["segment"] = segment

    # Use a MagicMock that supports both dict access and .get()
    mock_match_info = MagicMock()
    mock_match_info.__getitem__ = lambda self, k: match_info[k]
    mock_match_info.__contains__ = lambda self, k: k in match_info
    mock_match_info.get = lambda k, d="": match_info.get(k, d)
    request.match_info = mock_match_info

    # Set up headers
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    mock_headers = MagicMock()
    mock_headers.get = lambda k, d="": headers.get(k, d)
    request.headers = mock_headers

    # Set up query params
    query = {}
    if query_token:
        query["token"] = query_token
    mock_query = MagicMock()
    mock_query.get = lambda k, d="": query.get(k, d)
    request.query = mock_query

    return request


# ---------------------------------------------------------------------------
# handle_viz_playlist tests
# ---------------------------------------------------------------------------


class TestVizPlaylist:
    """Tests for handle_viz_playlist handler logic."""

    @pytest.mark.asyncio
    async def test_missing_token_returns_401(self):
        backend = _make_backend()
        request = _make_request()
        resp = await backend.handle_viz_playlist(request)
        assert resp.status == 401
        assert b"Missing authentication token" in resp.body

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(self):
        backend = _make_backend()
        request = _make_request(token="invalid-token-format")
        resp = await backend.handle_viz_playlist(request)
        assert resp.status == 401
        assert b"Invalid authentication token" in resp.body

    @pytest.mark.asyncio
    async def test_wrong_guild_token_returns_403(self):
        backend = _make_backend()
        request = _make_request(token=WRONG_GUILD_TOKEN)
        resp = await backend.handle_viz_playlist(request)
        assert resp.status == 403
        assert b"not authorized" in resp.body

    @pytest.mark.asyncio
    async def test_no_playlist_file_returns_404(self):
        backend = _make_backend()
        request = _make_request(token=VALID_TOKEN)
        resp = await backend.handle_viz_playlist(request)
        assert resp.status == 404
        assert b"No active visualizer stream" in resp.body

    @pytest.mark.asyncio
    async def test_serves_playlist_when_exists(self, tmp_path):
        """Test serving a playlist from a real temp directory."""
        backend = _make_backend()

        viz_dir = tmp_path / "12345" / "viz"
        viz_dir.mkdir(parents=True)
        playlist = viz_dir / "playlist.m3u8"
        playlist.write_text("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n")

        request = _make_request(token=VALID_TOKEN)

        # Patch Path in the module so the handler finds our temp file
        original_path = Path

        def patched_path(p):
            if isinstance(p, str) and p.startswith("/tmp/hellodj_hls/"):
                return original_path(str(tmp_path) + p[len("/tmp/hellodj_hls"):])
            return original_path(p)

        with patch("video.activity_backend.Path", side_effect=patched_path):
            resp = await backend.handle_viz_playlist(request)

        assert resp.status == 200
        assert resp.content_type == "application/vnd.apple.mpegurl"

    @pytest.mark.asyncio
    async def test_invalid_guild_id_returns_404(self):
        backend = _make_backend()
        request = _make_request(guild_id="not-a-number", token=VALID_TOKEN)
        resp = await backend.handle_viz_playlist(request)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_token_via_query_param(self):
        """Auth via ?token= query param works too."""
        backend = _make_backend()
        request = _make_request(query_token=VALID_TOKEN)
        resp = await backend.handle_viz_playlist(request)
        # Should get 404 (no file) rather than 401 (auth passes)
        assert resp.status == 404
        assert b"No active visualizer stream" in resp.body


# ---------------------------------------------------------------------------
# handle_viz_segment tests
# ---------------------------------------------------------------------------


class TestVizSegment:
    """Tests for handle_viz_segment handler logic."""

    @pytest.mark.asyncio
    async def test_missing_token_returns_401(self):
        backend = _make_backend()
        request = _make_request(segment="seg00001.ts")
        resp = await backend.handle_viz_segment(request)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(self):
        backend = _make_backend()
        request = _make_request(segment="seg00001.ts", token="bad")
        resp = await backend.handle_viz_segment(request)
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_wrong_guild_returns_403(self):
        backend = _make_backend()
        request = _make_request(segment="seg00001.ts", token=WRONG_GUILD_TOKEN)
        resp = await backend.handle_viz_segment(request)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_path_traversal_dots_rejected(self):
        """Filenames with .. are rejected."""
        backend = _make_backend()
        request = _make_request(segment="../etc/passwd", token=VALID_TOKEN)
        resp = await backend.handle_viz_segment(request)
        assert resp.status == 404
        assert b"Invalid segment name" in resp.body

    @pytest.mark.asyncio
    async def test_path_traversal_backslash_rejected(self):
        """Filenames with backslash are rejected."""
        backend = _make_backend()
        request = _make_request(segment="..\\etc\\passwd", token=VALID_TOKEN)
        resp = await backend.handle_viz_segment(request)
        assert resp.status == 404
        assert b"Invalid segment name" in resp.body

    @pytest.mark.asyncio
    async def test_segment_not_found_returns_404(self):
        backend = _make_backend()
        request = _make_request(segment="seg00001.ts", token=VALID_TOKEN)
        resp = await backend.handle_viz_segment(request)
        assert resp.status == 404
        assert b"Segment not found" in resp.body

    @pytest.mark.asyncio
    async def test_serves_segment_when_exists(self, tmp_path):
        """Test serving a segment file from the viz directory."""
        backend = _make_backend()

        viz_dir = tmp_path / "12345" / "viz"
        viz_dir.mkdir(parents=True)
        segment = viz_dir / "seg00001.ts"
        segment.write_bytes(b"\x47" * 188)

        request = _make_request(segment="seg00001.ts", token=VALID_TOKEN)

        original_path = Path

        def patched_path(p):
            if isinstance(p, str) and p.startswith("/tmp/hellodj_hls/"):
                return original_path(str(tmp_path) + p[len("/tmp/hellodj_hls"):])
            return original_path(p)

        with patch("video.activity_backend.Path", side_effect=patched_path):
            resp = await backend.handle_viz_segment(request)

        assert resp.status == 200
        assert resp.content_type == "video/mp2t"

    @pytest.mark.asyncio
    async def test_special_chars_in_segment_rejected(self):
        """Segment names with special characters are rejected."""
        backend = _make_backend()
        request = _make_request(segment="seg;rm -rf.ts", token=VALID_TOKEN)
        resp = await backend.handle_viz_segment(request)
        assert resp.status == 404
        assert b"Invalid segment name" in resp.body

    @pytest.mark.asyncio
    async def test_empty_segment_rejected(self):
        """Empty segment names are rejected."""
        backend = _make_backend()
        request = _make_request(segment="", token=VALID_TOKEN)
        resp = await backend.handle_viz_segment(request)
        assert resp.status == 404
        assert b"Invalid segment name" in resp.body


# ---------------------------------------------------------------------------
# Route registration tests
# ---------------------------------------------------------------------------


class TestVizRouteRegistration:
    """Verify the viz routes are registered in the app router."""

    def test_viz_playlist_route_registered(self):
        backend = _make_backend()
        resources = [r.get_info().get("formatter", "") for r in backend.app.router.resources()]
        assert "/activity/stream/{guild_id}/viz/playlist.m3u8" in resources

    def test_viz_segment_route_registered(self):
        backend = _make_backend()
        resources = [r.get_info().get("formatter", "") for r in backend.app.router.resources()]
        assert "/activity/stream/{guild_id}/viz/{segment}" in resources

    def test_viz_routes_before_variant_routes(self):
        """Viz routes must precede {variant}.m3u8 to avoid 'viz' being captured as variant."""
        backend = _make_backend()
        resources = [r.get_info().get("formatter", "") for r in backend.app.router.resources()]
        viz_idx = resources.index("/activity/stream/{guild_id}/viz/playlist.m3u8")
        variant_idx = resources.index("/activity/stream/{guild_id}/{variant}.m3u8")
        assert viz_idx < variant_idx
