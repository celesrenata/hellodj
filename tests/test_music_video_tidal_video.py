"""Unit tests for MusicVideoResolver._resolve_tidal_video resolution path.

Validates Requirements 4.1, 4.2, 4.3, 4.4:
- Tidal video URL resolves via TidalResolver.resolve_url
- Non-recoverable TidalResolverError raises MusicVideoResolverError
- Recoverable TidalResolverError falls back to YouTube search
- YouTube fallback failure raises MusicVideoResolverError
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest

import sys
from pathlib import Path

# Add bot/ to path so imports resolve correctly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from video.music_video_resolver import (
    MusicVideoClassification,
    MusicVideoResolver,
    MusicVideoResolverError,
    MusicVideoSourceType,
)
from video.tidal_resolver import TidalResolverError
from video.sources import YouTubeResolverError


@dataclass
class FakeVideoSource:
    """Minimal VideoSource stand-in for testing."""
    source_type: str = "tidal"
    file_path: str = "/tmp/test.mp4"
    title: str = "Test Video"
    duration_seconds: float = 180.0


def _make_classification(url: str = "https://tidal.com/browse/video/12345") -> MusicVideoClassification:
    return MusicVideoClassification(
        source_type=MusicVideoSourceType.TIDAL_VIDEO,
        original_query=url,
        extracted_id=None,
    )


@pytest.fixture
def resolver():
    """Create a MusicVideoResolver with mocked sub-resolvers."""
    youtube = AsyncMock()
    tidal = AsyncMock()
    spotify = AsyncMock()

    with patch("video.music_video_resolver.MusicVideoResolver.__init__", lambda self, **kw: None):
        r = MusicVideoResolver()
        r._youtube = youtube
        r._tidal = tidal
        r._spotify = spotify
    return r


# --- Requirement 4.1: Successful resolution via TidalResolver ---

@pytest.mark.asyncio
async def test_tidal_video_success(resolver):
    """Tidal video URL resolves successfully via TidalResolver.resolve_url."""
    expected = FakeVideoSource()
    resolver._tidal.resolve_url = AsyncMock(return_value=expected)

    classification = _make_classification()
    result = await resolver._resolve_tidal_video(classification)

    assert result is expected
    resolver._tidal.resolve_url.assert_awaited_once_with(classification.original_query)


# --- Requirement 4.3: Non-recoverable error raises MusicVideoResolverError ---

@pytest.mark.asyncio
async def test_tidal_video_non_recoverable_error(resolver):
    """Non-recoverable TidalResolverError raises MusicVideoResolverError."""
    resolver._tidal.resolve_url = AsyncMock(
        side_effect=TidalResolverError("Video removed", recoverable=False)
    )

    classification = _make_classification()
    with pytest.raises(MusicVideoResolverError) as exc_info:
        await resolver._resolve_tidal_video(classification)

    assert exc_info.value.user_message == "Tidal video is unavailable."


# --- Requirement 4.4: Recoverable error falls back to YouTube ---

@pytest.mark.asyncio
async def test_tidal_video_recoverable_falls_back_to_youtube(resolver):
    """Recoverable TidalResolverError falls back to YouTube search."""
    resolver._tidal.resolve_url = AsyncMock(
        side_effect=TidalResolverError("Artist - Song Title", recoverable=True)
    )
    yt_result = FakeVideoSource(source_type="youtube", title="Artist - Song Title")
    resolver._youtube.resolve = AsyncMock(return_value=yt_result)

    classification = _make_classification()
    result = await resolver._resolve_tidal_video(classification)

    assert result is yt_result
    # The YouTube search query should include the error message context
    resolver._youtube.resolve.assert_awaited_once()
    search_query = resolver._youtube.resolve.call_args[0][0]
    assert "official music video" in search_query
    assert "Artist - Song Title" in search_query


# --- Requirement 4.4: YouTube fallback also fails ---

@pytest.mark.asyncio
async def test_tidal_video_youtube_fallback_also_fails(resolver):
    """When YouTube fallback also fails, raises MusicVideoResolverError."""
    resolver._tidal.resolve_url = AsyncMock(
        side_effect=TidalResolverError("Some video title", recoverable=True)
    )
    resolver._youtube.resolve = AsyncMock(
        side_effect=YouTubeResolverError("No results found")
    )

    classification = _make_classification()
    with pytest.raises(MusicVideoResolverError) as exc_info:
        await resolver._resolve_tidal_video(classification)

    assert exc_info.value.user_message == "No music video found for that query."


# --- Edge case: Recoverable error with empty message uses URL-derived query ---

@pytest.mark.asyncio
async def test_tidal_video_recoverable_empty_message_uses_url(resolver):
    """Recoverable error with short/empty message derives search from URL."""
    resolver._tidal.resolve_url = AsyncMock(
        side_effect=TidalResolverError("", recoverable=True)
    )
    yt_result = FakeVideoSource(source_type="youtube")
    resolver._youtube.resolve = AsyncMock(return_value=yt_result)

    classification = _make_classification("https://tidal.com/browse/video/98765")
    result = await resolver._resolve_tidal_video(classification)

    assert result is yt_result
    search_query = resolver._youtube.resolve.call_args[0][0]
    # Should use something derived from the URL rather than empty string
    assert "official music video" in search_query
    assert len(search_query) > len("official music video")
