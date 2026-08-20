"""Tests for TidalResolver.search() — Tidal music video search functionality."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Set up environment so credentials.py can initialize (uses tmp for DB)
os.environ.setdefault("HELLODJ_DB_KEY", "test-key-for-unit-tests-only")
os.environ.setdefault("DATA_DIR", "/tmp/hellodj_test_data")

from video.tidal_resolver import TidalResolver, TidalResolverError


@pytest.fixture
def resolver(tmp_path):
    """Create a TidalResolver with a temp download dir."""
    return TidalResolver(download_dir=tmp_path)


class TestSearchValidation:
    """Tests for search() query validation."""

    @pytest.mark.asyncio
    async def test_empty_query_raises(self, resolver):
        """Empty string raises TidalResolverError."""
        with pytest.raises(TidalResolverError, match="search query is required"):
            await resolver.search("")

    @pytest.mark.asyncio
    async def test_whitespace_only_query_raises(self, resolver):
        """Whitespace-only string raises TidalResolverError."""
        with pytest.raises(TidalResolverError, match="search query is required"):
            await resolver.search("   \t\n  ")

    @pytest.mark.asyncio
    async def test_long_query_truncated_not_rejected(self, resolver):
        """Query > 200 chars is truncated, not rejected."""
        long_query = "a" * 250

        with patch.object(resolver, "_ensure_token", new_callable=AsyncMock) as mock_token:
            mock_token.return_value = "test_token"
            with patch.object(resolver, "_search_videos", new_callable=AsyncMock) as mock_search:
                mock_search.return_value = 12345
                with patch.object(resolver, "_fetch_video_metadata", new_callable=AsyncMock) as mock_meta:
                    mock_meta.return_value = {"title": "Test", "duration": 180, "artist": "Artist"}
                    with patch.object(resolver, "_fetch_stream_url", new_callable=AsyncMock) as mock_stream:
                        mock_stream.return_value = "https://stream.tidal.com/video.mp4"
                        with patch.object(resolver, "_download_video", new_callable=AsyncMock) as mock_dl:
                            mock_dl.return_value = "/tmp/video.mp4"
                            await resolver.search(long_query)

                            # Verify _search_videos received truncated query (200 chars)
                            call_args = mock_search.call_args[0]
                            assert len(call_args[0]) == 200


class TestSearchFlow:
    """Tests for the full search resolution flow."""

    @pytest.mark.asyncio
    async def test_successful_search_returns_videosource(self, resolver):
        """Successful search produces VideoSource with correct fields."""
        with patch.object(resolver, "_ensure_token", new_callable=AsyncMock) as mock_token:
            mock_token.return_value = "test_token"
            with patch.object(resolver, "_search_videos", new_callable=AsyncMock) as mock_search:
                mock_search.return_value = 99999
                with patch.object(resolver, "_fetch_video_metadata", new_callable=AsyncMock) as mock_meta:
                    mock_meta.return_value = {
                        "title": "Around the World",
                        "duration": 210,
                        "artist": "Daft Punk",
                    }
                    with patch.object(resolver, "_fetch_stream_url", new_callable=AsyncMock) as mock_stream:
                        mock_stream.return_value = "https://stream.tidal.com/v.mp4"
                        with patch.object(resolver, "_download_video", new_callable=AsyncMock) as mock_dl:
                            mock_dl.return_value = "/tmp/downloaded.mp4"

                            result = await resolver.search("daft punk around the world")

        assert result.source_type == "tidal"
        assert result.title == "Daft Punk — Around the World"
        assert result.duration_seconds == 210.0
        assert result.metadata["artist"] == "Daft Punk"
        assert result.metadata["track_title"] == "Around the World"
        assert result.metadata["video_id"] == 99999
        assert result.metadata["tidal_url"] == "https://tidal.com/browse/video/99999"
        assert result.cleanup_on_finish is True

    @pytest.mark.asyncio
    async def test_search_no_artist_title_format(self, resolver):
        """When artist is empty, title is just the track title."""
        with patch.object(resolver, "_ensure_token", new_callable=AsyncMock) as mock_token:
            mock_token.return_value = "test_token"
            with patch.object(resolver, "_search_videos", new_callable=AsyncMock) as mock_search:
                mock_search.return_value = 11111
                with patch.object(resolver, "_fetch_video_metadata", new_callable=AsyncMock) as mock_meta:
                    mock_meta.return_value = {
                        "title": "Mystery Video",
                        "duration": 120,
                        "artist": "",
                    }
                    with patch.object(resolver, "_fetch_stream_url", new_callable=AsyncMock) as mock_stream:
                        mock_stream.return_value = "https://stream.tidal.com/v.mp4"
                        with patch.object(resolver, "_download_video", new_callable=AsyncMock) as mock_dl:
                            mock_dl.return_value = "/tmp/downloaded.mp4"

                            result = await resolver.search("mystery video")

        assert result.title == "Mystery Video"

    @pytest.mark.asyncio
    async def test_no_credentials_raises(self, resolver):
        """When _ensure_token fails (no credentials), error propagates."""
        with patch.object(resolver, "_ensure_token", new_callable=AsyncMock) as mock_token:
            mock_token.side_effect = TidalResolverError(
                "Tidal is not connected — use the web UI to authenticate"
            )
            with pytest.raises(TidalResolverError, match="not connected"):
                await resolver.search("some query")

    @pytest.mark.asyncio
    async def test_no_results_raises(self, resolver):
        """When search returns no results, raises appropriate error."""
        with patch.object(resolver, "_ensure_token", new_callable=AsyncMock) as mock_token:
            mock_token.return_value = "test_token"
            with patch.object(resolver, "_search_videos", new_callable=AsyncMock) as mock_search:
                mock_search.side_effect = TidalResolverError(
                    "No Tidal music videos matched your search"
                )
                with pytest.raises(TidalResolverError, match="No Tidal music videos"):
                    await resolver.search("xyznonexistent")

    @pytest.mark.asyncio
    async def test_unavailable_video_raises(self, resolver):
        """When stream fetch fails for the result, raises unavailable error."""
        with patch.object(resolver, "_ensure_token", new_callable=AsyncMock) as mock_token:
            mock_token.return_value = "test_token"
            with patch.object(resolver, "_search_videos", new_callable=AsyncMock) as mock_search:
                mock_search.return_value = 77777
                with patch.object(resolver, "_fetch_video_metadata", new_callable=AsyncMock) as mock_meta:
                    mock_meta.return_value = {"title": "Restricted", "duration": 180, "artist": "Artist"}
                    with patch.object(resolver, "_fetch_stream_url", new_callable=AsyncMock) as mock_stream:
                        mock_stream.side_effect = TidalResolverError(
                            "This track has no music video available"
                        )
                        with pytest.raises(TidalResolverError, match="This video is unavailable"):
                            await resolver.search("restricted video")


class TestSearchVideosAPI:
    """Tests for _search_videos() — the Tidal search API call."""

    @pytest.mark.asyncio
    async def test_successful_search_returns_video_id(self, resolver):
        """Successful search returns the first result's video ID."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "items": [{"id": 12345, "title": "Test Video", "artists": []}],
            "totalNumberOfItems": 1,
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.tidal_resolver.aiohttp.ClientSession", return_value=mock_session):
            result = await resolver._search_videos("test query", "token123")

        assert result == 12345

    @pytest.mark.asyncio
    async def test_empty_items_raises(self, resolver):
        """Empty items list raises 'no results' error."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "items": [],
            "totalNumberOfItems": 0,
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.tidal_resolver.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(TidalResolverError, match="No Tidal music videos"):
                await resolver._search_videos("nonexistent", "token123")

    @pytest.mark.asyncio
    async def test_http_401_retries_with_refresh(self, resolver):
        """HTTP 401 triggers token refresh and retry."""
        # First response: 401
        mock_response_401 = AsyncMock()
        mock_response_401.status = 401
        mock_response_401.__aenter__ = AsyncMock(return_value=mock_response_401)
        mock_response_401.__aexit__ = AsyncMock(return_value=False)

        # Retry response: 200
        mock_response_200 = AsyncMock()
        mock_response_200.status = 200
        mock_response_200.json = AsyncMock(return_value={
            "items": [{"id": 55555, "title": "After Refresh"}],
            "totalNumberOfItems": 1,
        })
        mock_response_200.__aenter__ = AsyncMock(return_value=mock_response_200)
        mock_response_200.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=[mock_response_401, mock_response_200])
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.tidal_resolver.aiohttp.ClientSession", return_value=mock_session):
            with patch.object(resolver, "_refresh_token", new_callable=AsyncMock) as mock_refresh:
                mock_refresh.return_value = "new_token"
                result = await resolver._search_videos("test", "old_token")

        assert result == 55555
        mock_refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_http_500_raises_recoverable(self, resolver):
        """HTTP 500 raises a recoverable error."""
        mock_response = AsyncMock()
        mock_response.status = 500
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.tidal_resolver.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(TidalResolverError, match="search failed") as exc_info:
                await resolver._search_videos("test", "token")
            assert exc_info.value.recoverable is True

    @pytest.mark.asyncio
    async def test_network_error_raises_recoverable(self, resolver):
        """Network error raises a recoverable error."""
        import aiohttp

        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=aiohttp.ClientError("connection reset"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.tidal_resolver.aiohttp.ClientSession", return_value=mock_session):
            with pytest.raises(TidalResolverError, match="try again later") as exc_info:
                await resolver._search_videos("test", "token")
            assert exc_info.value.recoverable is True
