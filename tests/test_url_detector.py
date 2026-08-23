"""Unit tests for URLDetector.detect() — task 5.1.

Validates Requirements 8.1, 8.2, 8.3:
- Recognized platform URL patterns
- Returns (platform_name, url) tuple or None
- Handles query parameters, fragments, additional path segments
"""

from __future__ import annotations

import pytest

from search.url_detector import URLDetector


class TestSpotifyURLs:
    """Spotify track URL detection."""

    def test_basic_track_url(self):
        result = URLDetector.detect("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC")
        assert result == ("Spotify", "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC")

    def test_track_url_with_query_params(self):
        url = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=abc123def"
        result = URLDetector.detect(url)
        assert result == ("Spotify", url)

    def test_track_url_without_open_prefix(self):
        url = "https://spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
        result = URLDetector.detect(url)
        assert result == ("Spotify", url)

    def test_http_scheme(self):
        url = "http://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
        result = URLDetector.detect(url)
        assert result == ("Spotify", url)

    def test_case_insensitive_scheme(self):
        url = "HTTPS://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"
        result = URLDetector.detect(url)
        assert result == ("Spotify", url)


class TestTidalURLs:
    """Tidal track URL detection."""

    def test_direct_track_url(self):
        url = "https://tidal.com/track/123456789"
        result = URLDetector.detect(url)
        assert result == ("Tidal", url)

    def test_browse_track_url(self):
        url = "https://tidal.com/browse/track/123456789"
        result = URLDetector.detect(url)
        assert result == ("Tidal", url)

    def test_www_prefix(self):
        url = "https://www.tidal.com/track/123456789"
        result = URLDetector.detect(url)
        assert result == ("Tidal", url)

    def test_listen_prefix(self):
        url = "https://listen.tidal.com/track/123456789"
        result = URLDetector.detect(url)
        assert result == ("Tidal", url)

    def test_browse_with_listen_prefix(self):
        url = "https://listen.tidal.com/browse/track/987654"
        result = URLDetector.detect(url)
        assert result == ("Tidal", url)

    def test_track_with_fragment(self):
        url = "https://tidal.com/track/123456789#section"
        result = URLDetector.detect(url)
        assert result == ("Tidal", url)


class TestYouTubeURLs:
    """YouTube watch and short URL detection."""

    def test_watch_url(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        result = URLDetector.detect(url)
        assert result == ("YouTube", url)

    def test_watch_url_without_www(self):
        url = "https://youtube.com/watch?v=dQw4w9WgXcQ"
        result = URLDetector.detect(url)
        assert result == ("YouTube", url)

    def test_mobile_watch_url(self):
        url = "https://m.youtube.com/watch?v=dQw4w9WgXcQ"
        result = URLDetector.detect(url)
        assert result == ("YouTube", url)

    def test_short_url(self):
        url = "https://youtu.be/dQw4w9WgXcQ"
        result = URLDetector.detect(url)
        assert result == ("YouTube", url)

    def test_short_url_with_params(self):
        url = "https://youtu.be/dQw4w9WgXcQ?t=42"
        result = URLDetector.detect(url)
        assert result == ("YouTube", url)

    def test_watch_url_with_multiple_params(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf&index=2"
        result = URLDetector.detect(url)
        assert result == ("YouTube", url)


class TestSoundCloudURLs:
    """SoundCloud URL detection."""

    def test_artist_track_url(self):
        url = "https://soundcloud.com/artist-name/track-name"
        result = URLDetector.detect(url)
        assert result == ("SoundCloud", url)

    def test_www_prefix(self):
        url = "https://www.soundcloud.com/artist/track"
        result = URLDetector.detect(url)
        assert result == ("SoundCloud", url)

    def test_mobile_prefix(self):
        url = "https://m.soundcloud.com/artist/track"
        result = URLDetector.detect(url)
        assert result == ("SoundCloud", url)

    def test_url_with_additional_path(self):
        url = "https://soundcloud.com/artist/sets/playlist-name"
        result = URLDetector.detect(url)
        assert result == ("SoundCloud", url)


class TestNonMatchingInputs:
    """Inputs that should return None."""

    def test_plain_text_query(self):
        assert URLDetector.detect("bohemian rhapsody") is None

    def test_empty_string(self):
        assert URLDetector.detect("") is None

    def test_whitespace_only(self):
        assert URLDetector.detect("   ") is None

    def test_unrecognized_domain(self):
        assert URLDetector.detect("https://example.com/track/123") is None

    def test_spotify_non_track_url(self):
        # Only spotify.com/track/ is matched — albums, playlists, etc. are not
        # (requirement says spotify.com/track specifically)
        assert URLDetector.detect("https://open.spotify.com/album/12345") is None

    def test_no_scheme(self):
        assert URLDetector.detect("spotify.com/track/123") is None

    def test_ftp_scheme(self):
        assert URLDetector.detect("ftp://spotify.com/track/123") is None

    def test_youtube_non_watch_url(self):
        # youtube.com/playlist or youtube.com/channel should not match
        assert URLDetector.detect("https://youtube.com/playlist?list=abc") is None

    def test_leading_whitespace_still_works(self):
        url = "  https://open.spotify.com/track/abc123"
        result = URLDetector.detect(url)
        assert result == ("Spotify", url.strip())
