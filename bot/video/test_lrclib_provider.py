"""Unit tests for lrclib_provider.py — LRCLIB.net API client and LRC parser.

Requirements: 1.1, 1.2, 1.3, 8.1, 8.2, 8.3, 8.4, 8.5
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Set up environment so credentials.py can initialize (uses tmp for DB)
os.environ.setdefault("HELLODJ_DB_KEY", "test-key-for-unit-tests-only")
os.environ.setdefault("DATA_DIR", "/tmp/hellodj_test_data")

from video.lrclib_provider import LRCLIBProvider, parse_lrc
from video.lyrics_models import TimedLine, TimedLyrics, TimedWord


@pytest.fixture
def run():
    """Helper to run async functions in tests."""

    def _run(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    return _run


class TestParseLrc:
    """Tests for parse_lrc() LRC format parser."""

    def test_basic_lrc_parsing(self):
        """Parse standard LRC lines with 2-digit centisecond format."""
        lrc = "[00:12.34]First line\n[00:15.67]Second line"
        result = parse_lrc(lrc)
        assert len(result) == 2
        assert result[0].time_ms == 12340
        assert result[0].text == "First line"
        assert result[1].time_ms == 15670
        assert result[1].text == "Second line"

    def test_three_digit_milliseconds(self):
        """Parse LRC lines with 3-digit millisecond format."""
        lrc = "[01:23.456]Hello world"
        result = parse_lrc(lrc)
        assert len(result) == 1
        assert result[0].time_ms == 83456  # 1*60000 + 23*1000 + 456

    def test_two_digit_centiseconds(self):
        """Two-digit centiseconds are multiplied by 10."""
        lrc = "[00:05.50]Half second"
        result = parse_lrc(lrc)
        assert len(result) == 1
        assert result[0].time_ms == 5500  # 0*60000 + 5*1000 + 50*10

    def test_mixed_formats(self):
        """Handle a mix of 2-digit and 3-digit fractional seconds."""
        lrc = "[00:10.00]Line A\n[00:20.500]Line B"
        result = parse_lrc(lrc)
        assert len(result) == 2
        assert result[0].time_ms == 10000  # 00 centisec = 0ms
        assert result[1].time_ms == 20500  # 500ms

    def test_skips_non_lrc_lines(self):
        """Non-LRC lines (metadata, empty) are silently skipped."""
        lrc = "[ti:Song Title]\n[ar:Artist]\n\n[00:05.00]Actual lyrics"
        result = parse_lrc(lrc)
        assert len(result) == 1
        assert result[0].text == "Actual lyrics"

    def test_empty_input(self):
        """Empty string returns empty list."""
        result = parse_lrc("")
        assert result == []

    def test_whitespace_only(self):
        """Whitespace-only string returns empty list."""
        result = parse_lrc("   \n  \n  ")
        assert result == []

    def test_strips_line_text(self):
        """Text after timestamp is stripped of leading/trailing whitespace."""
        lrc = "[00:01.00]  Padded text  "
        result = parse_lrc(lrc)
        assert result[0].text == "Padded text"

    def test_empty_text_after_timestamp(self):
        """Lines with timestamp but no text produce empty string text."""
        lrc = "[00:01.00]\n[00:02.00]Real line"
        result = parse_lrc(lrc)
        assert len(result) == 2
        assert result[0].text == ""
        assert result[1].text == "Real line"

    def test_all_words_none_phase1(self):
        """Lines without word-level timestamps have words=None."""
        lrc = "[00:01.00]Some words here"
        result = parse_lrc(lrc)
        assert result[0].words is None

    def test_word_level_parsing(self):
        """Lines with word-level timestamps produce TimedWord objects."""
        lrc = "[00:12.34]<00:12.34>Hello <00:12.80>world <00:13.10>today"
        result = parse_lrc(lrc)
        assert len(result) == 1
        assert result[0].time_ms == 12340
        assert result[0].text == "Hello world today"
        assert result[0].words is not None
        assert len(result[0].words) == 3
        assert result[0].words[0].time_ms == 12340
        assert result[0].words[0].text == "Hello"
        assert result[0].words[1].time_ms == 12800
        assert result[0].words[1].text == "world"
        assert result[0].words[2].time_ms == 13100
        assert result[0].words[2].text == "today"

    def test_word_level_three_digit_ms(self):
        """Word-level timestamps with 3-digit milliseconds parse correctly."""
        lrc = "[00:05.000]<00:05.123>One <00:05.456>Two"
        result = parse_lrc(lrc)
        assert result[0].words is not None
        assert result[0].words[0].time_ms == 5123
        assert result[0].words[0].text == "One"
        assert result[0].words[1].time_ms == 5456
        assert result[0].words[1].text == "Two"

    def test_mixed_lines_word_and_no_word(self):
        """LRC with some word-level lines and some line-only lines."""
        lrc = (
            "[00:01.00]Plain line\n"
            "[00:05.00]<00:05.00>Word <00:05.50>level"
        )
        result = parse_lrc(lrc)
        assert len(result) == 2
        assert result[0].words is None
        assert result[0].text == "Plain line"
        assert result[1].words is not None
        assert len(result[1].words) == 2
        assert result[1].text == "Word level"

    def test_word_level_text_cleaned(self):
        """Display text removes all <mm:ss.xx> tags for clean display."""
        lrc = "[00:10.00]<00:10.00>The <00:10.20>quick <00:10.40>brown <00:10.60>fox"
        result = parse_lrc(lrc)
        assert result[0].text == "The quick brown fox"

    def test_zero_timestamp(self):
        """Timestamp [00:00.00] produces time_ms=0."""
        lrc = "[00:00.00]Start"
        result = parse_lrc(lrc)
        assert result[0].time_ms == 0

    def test_large_timestamp(self):
        """Large timestamps (5+ minutes) are correctly computed."""
        lrc = "[05:30.25]Late line"
        result = parse_lrc(lrc)
        # 5*60000 + 30*1000 + 25*10 = 330250
        assert result[0].time_ms == 330250

    def test_returns_timed_line_instances(self):
        """All parsed results are TimedLine instances."""
        lrc = "[00:01.00]A\n[00:02.00]B"
        result = parse_lrc(lrc)
        for line in result:
            assert isinstance(line, TimedLine)


class TestLRCLIBProviderFetch:
    """Tests for LRCLIBProvider.fetch() API client method."""

    def _mock_response(self, status=200, json_data=None):
        """Create a mock aiohttp response."""
        resp = AsyncMock()
        resp.status = status
        resp.json = AsyncMock(return_value=json_data or {})
        return resp

    def test_404_returns_none(self, run):
        """HTTP 404 response returns None."""
        provider = LRCLIBProvider()
        mock_resp = self._mock_response(status=404)

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_session_ctx)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.lrclib_provider.aiohttp.ClientSession", return_value=mock_session):
            result = run(provider.fetch("Artist", "Title", 225.0))
        assert result is None

    def test_instrumental_returns_none(self, run):
        """Instrumental track returns None."""
        provider = LRCLIBProvider()
        json_data = {
            "instrumental": True,
            "syncedLyrics": None,
            "plainLyrics": None,
        }
        mock_resp = self._mock_response(status=200, json_data=json_data)

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_session_ctx)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.lrclib_provider.aiohttp.ClientSession", return_value=mock_session):
            result = run(provider.fetch("Artist", "Song", 180.0))
        assert result is None

    def test_synced_lyrics_returns_timed_lyrics(self, run):
        """Response with syncedLyrics returns a TimedLyrics object."""
        provider = LRCLIBProvider()
        json_data = {
            "instrumental": False,
            "syncedLyrics": "[00:12.34]First line\n[00:15.67]Second line",
            "plainLyrics": "First line\nSecond line",
        }
        mock_resp = self._mock_response(status=200, json_data=json_data)

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_session_ctx)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.lrclib_provider.aiohttp.ClientSession", return_value=mock_session):
            result = run(provider.fetch("Artist", "Song", 225.0))

        assert isinstance(result, TimedLyrics)
        assert result.sync_type == "lrc_synced"
        assert result.duration_s == 225.0
        assert result.track_id == "artist:song"
        assert len(result.lines) == 2
        assert result.lines[0].time_ms == 12340
        assert result.lines[0].text == "First line"

    def test_plain_lyrics_only_returns_string(self, run):
        """Response with only plainLyrics returns raw text string."""
        provider = LRCLIBProvider()
        json_data = {
            "instrumental": False,
            "syncedLyrics": None,
            "plainLyrics": "Line one\nLine two\nLine three",
        }
        mock_resp = self._mock_response(status=200, json_data=json_data)

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_session_ctx)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.lrclib_provider.aiohttp.ClientSession", return_value=mock_session):
            result = run(provider.fetch("Artist", "Song", 180.0))

        assert isinstance(result, str)
        assert result == "Line one\nLine two\nLine three"

    def test_timeout_returns_none(self, run):
        """Timeout during HTTP request returns None (no raise)."""
        provider = LRCLIBProvider()

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=TimeoutError("timed out"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.lrclib_provider.aiohttp.ClientSession", return_value=mock_session):
            result = run(provider.fetch("Artist", "Song", 180.0))
        assert result is None

    def test_server_error_returns_none(self, run):
        """Non-200/404 status returns None."""
        provider = LRCLIBProvider()
        mock_resp = self._mock_response(status=500)

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_session_ctx)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.lrclib_provider.aiohttp.ClientSession", return_value=mock_session):
            result = run(provider.fetch("Artist", "Song", 180.0))
        assert result is None

    def test_empty_synced_lyrics_falls_to_plain(self, run):
        """Empty syncedLyrics string with valid plainLyrics returns the plain text."""
        provider = LRCLIBProvider()
        json_data = {
            "instrumental": False,
            "syncedLyrics": "",
            "plainLyrics": "Just plain text",
        }
        mock_resp = self._mock_response(status=200, json_data=json_data)

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_session_ctx)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.lrclib_provider.aiohttp.ClientSession", return_value=mock_session):
            result = run(provider.fetch("Artist", "Song", 120.0))

        assert isinstance(result, str)
        assert result == "Just plain text"

    def test_track_id_is_lowercase_stripped(self, run):
        """track_id in returned TimedLyrics uses lowercase stripped artist:title."""
        provider = LRCLIBProvider()
        json_data = {
            "instrumental": False,
            "syncedLyrics": "[00:01.00]Line",
            "plainLyrics": None,
        }
        mock_resp = self._mock_response(status=200, json_data=json_data)

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_session_ctx)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.lrclib_provider.aiohttp.ClientSession", return_value=mock_session):
            result = run(provider.fetch("  The Artist  ", "  My Song  ", 60.0))

        assert isinstance(result, TimedLyrics)
        assert result.track_id == "the artist:my song"

    def test_word_level_sync_type_lrc_word(self, run):
        """Response with word-level LRC data sets sync_type to 'lrc_word'."""
        provider = LRCLIBProvider()
        json_data = {
            "instrumental": False,
            "syncedLyrics": "[00:12.34]<00:12.34>Hello <00:12.80>world",
            "plainLyrics": None,
        }
        mock_resp = self._mock_response(status=200, json_data=json_data)

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_session_ctx)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.lrclib_provider.aiohttp.ClientSession", return_value=mock_session):
            result = run(provider.fetch("Artist", "Song", 120.0))

        assert isinstance(result, TimedLyrics)
        assert result.sync_type == "lrc_word"
        assert result.lines[0].words is not None
        assert len(result.lines[0].words) == 2
        assert result.lines[0].text == "Hello world"
