"""Unit tests for MusicVideoResolver youtube_direct and youtube_music resolution paths."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Ensure the bot directory is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

# Set a test encryption key before importing anything that touches creds
os.environ.setdefault("HELLODJ_DB_KEY", "test-key-for-music-video-resolver-tests")

from video.music_video_resolver import (
    MusicVideoClassification,
    MusicVideoResolver,
    MusicVideoResolverError,
    MusicVideoSourceType,
)


@dataclass
class _FakeVideoSource:
    """Minimal stand-in for VideoSource in tests."""
    source_type: str = "youtube"
    file_path: str = "/tmp/fake.mp4"
    title: str = "Test Video"
    duration_seconds: float = 180.0


class _FakeYouTubeResolverError(Exception):
    """Stand-in for YouTubeResolverError to avoid yt-dlp dependency in tests."""
    def __init__(self, message: str, category: str = "unknown") -> None:
        super().__init__(message)
        self.category = category


@pytest.fixture
def mock_youtube():
    """Create a mock YouTubeResolver."""
    return AsyncMock()


@pytest.fixture
def mock_tidal():
    """Create a mock TidalResolver."""
    return AsyncMock()


@pytest.fixture
def mock_spotify():
    """Create a mock SpotifyMetadataExtractor."""
    return AsyncMock()


@pytest.fixture
def resolver(mock_youtube, mock_tidal, mock_spotify):
    """Create a MusicVideoResolver with mocked sub-resolvers."""
    with patch("video.music_video_resolver.MusicVideoResolver.__init__", lambda self, **kw: None):
        r = MusicVideoResolver.__new__(MusicVideoResolver)
        r._youtube = mock_youtube
        r._tidal = mock_tidal
        r._spotify = mock_spotify
    return r


# ---------------------------------------------------------------------------
# youtube_direct tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_youtube_direct_passes_url_to_resolver(resolver, mock_youtube):
    """youtube_direct passes the full original URL to YouTubeResolver.resolve()."""
    expected_source = _FakeVideoSource(title="Rick Astley")
    mock_youtube.resolve.return_value = expected_source

    classification = MusicVideoClassification(
        source_type=MusicVideoSourceType.YOUTUBE_DIRECT,
        original_query="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    )

    result = await resolver._resolve_youtube_direct(classification)

    mock_youtube.resolve.assert_awaited_once_with(
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )
    assert result == expected_source


@pytest.mark.asyncio
async def test_youtube_direct_wraps_resolver_error(resolver, mock_youtube):
    """youtube_direct wraps YouTubeResolverError into MusicVideoResolverError."""
    from video.sources import YouTubeResolverError

    mock_youtube.resolve.side_effect = YouTubeResolverError("Video unavailable")

    classification = MusicVideoClassification(
        source_type=MusicVideoSourceType.YOUTUBE_DIRECT,
        original_query="https://www.youtube.com/watch?v=invalid123",
    )

    with pytest.raises(MusicVideoResolverError) as exc_info:
        await resolver._resolve_youtube_direct(classification)

    assert "unavailable" in exc_info.value.user_message.lower() or "could not" in exc_info.value.user_message.lower()
    assert exc_info.value.__cause__ is not None


# ---------------------------------------------------------------------------
# youtube_music tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_youtube_music_constructs_url_from_id(resolver, mock_youtube):
    """youtube_music constructs https://youtube.com/watch?v={id} and resolves."""
    expected_source = _FakeVideoSource(title="Music Video")
    mock_youtube.resolve.return_value = expected_source

    classification = MusicVideoClassification(
        source_type=MusicVideoSourceType.YOUTUBE_MUSIC,
        original_query="https://music.youtube.com/watch?v=abc123XYZ_-",
        extracted_id="abc123XYZ_-",
    )

    result = await resolver._resolve_youtube_music(classification)

    mock_youtube.resolve.assert_awaited_once_with(
        "https://youtube.com/watch?v=abc123XYZ_-"
    )
    assert result == expected_source


@pytest.mark.asyncio
async def test_youtube_music_no_video_id_raises_error(resolver):
    """youtube_music with no extracted video ID raises MusicVideoResolverError."""
    classification = MusicVideoClassification(
        source_type=MusicVideoSourceType.YOUTUBE_MUSIC,
        original_query="https://music.youtube.com/watch",
        extracted_id=None,
    )

    with pytest.raises(MusicVideoResolverError) as exc_info:
        await resolver._resolve_youtube_music(classification)

    assert "Could not extract video ID from YouTube Music URL" in exc_info.value.user_message


@pytest.mark.asyncio
async def test_youtube_music_wraps_resolver_error(resolver, mock_youtube):
    """youtube_music wraps YouTubeResolverError into MusicVideoResolverError."""
    from video.sources import YouTubeResolverError

    mock_youtube.resolve.side_effect = YouTubeResolverError("Network timeout")

    classification = MusicVideoClassification(
        source_type=MusicVideoSourceType.YOUTUBE_MUSIC,
        original_query="https://music.youtube.com/watch?v=abc123XYZ_-",
        extracted_id="abc123XYZ_-",
    )

    with pytest.raises(MusicVideoResolverError) as exc_info:
        await resolver._resolve_youtube_music(classification)

    assert exc_info.value.user_message is not None
    assert exc_info.value.__cause__ is not None
