"""End-to-end tests for /remote metadata matching per source.

Verifies that _track_entry(), _split_title(), and both embed builders
produce correct metadata (song, artist, album, artwork, source label)
for every supported source: YouTube, Spotify (direct + LavasRC), Tidal
(direct + LavasRC), and SoundCloud.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

import player


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_track(
    *,
    title: str = "Unknown",
    author: str = "",
    uri: str | None = None,
    length: int = 0,
    source: str = "unknown",
    artwork: str | None = None,
    extras: dict | None = None,
    raw_data: dict | None = None,
    album_name: str | None = None,
) -> MagicMock:
    """Build a mock wavelink.Playable with the given fields."""
    track = MagicMock()
    track.title = title
    track.author = author
    track.uri = uri
    track.length = length
    track.source = source
    track.artwork = artwork

    # extras (LavasRC plugin info)
    if extras is not None:
        track.extras = extras
    else:
        track.extras = None

    # raw_data (fallback for plugin info)
    if raw_data is not None:
        track.raw_data = raw_data
    else:
        track.raw_data = None

    # album object (wavelink native)
    if album_name:
        album = MagicMock()
        album.name = album_name
        track.album = album
    else:
        track.album = None

    return track


# ---------------------------------------------------------------------------
# Tests: _track_entry per source
# ---------------------------------------------------------------------------


class TestTrackEntryYouTube:
    """YouTube tracks: title is 'Artist - Song (Official Video)', author is channel."""

    def test_standard_youtube_track(self):
        track = _mock_track(
            title="Queen - Bohemian Rhapsody (Official Video)",
            author="Queen Official",
            uri="https://www.youtube.com/watch?v=fJ9rUzIMcZQ",
            length=354000,
            source="youtube",
            artwork="https://i.ytimg.com/vi/fJ9rUzIMcZQ/maxresdefault.jpg",
        )
        entry = player._track_entry(track, provider="youtube")

        assert entry["title"] == "Queen - Bohemian Rhapsody (Official Video)"
        assert entry["author"] == "Queen Official"
        assert entry["source"] == "youtube"
        assert entry["duration"] == 354000
        assert entry["artwork_url"] == "https://i.ytimg.com/vi/fJ9rUzIMcZQ/maxresdefault.jpg"

    def test_youtube_topic_channel(self):
        """YouTube Music auto-generated tracks have ' - Topic' author."""
        track = _mock_track(
            title="Bohemian Rhapsody",
            author="Queen - Topic",
            uri="https://music.youtube.com/watch?v=fJ9rUzIMcZQ",
            length=354000,
            source="youtube",
            artwork="https://lh3.googleusercontent.com/abc123",
        )
        entry = player._track_entry(track, provider="youtube_music")

        assert entry["title"] == "Bohemian Rhapsody"
        assert entry["author"] == "Queen - Topic"
        assert entry["artwork_url"] == "https://lh3.googleusercontent.com/abc123"


class TestTrackEntrySpotify:
    """Spotify tracks via LavasRC: clean title, correct author, extras with artwork."""

    def test_spotify_lavasrc_track(self):
        track = _mock_track(
            title="Bohemian Rhapsody - Remastered 2011",
            author="Queen",
            uri="https://open.spotify.com/track/7tFiyTwD0nx5a1eklYtX2J",
            length=354947,
            source="spotify",
            artwork="https://i.scdn.co/image/abc123",
            extras={"albumName": "Greatest Hits", "artworkUrl": "https://i.scdn.co/image/abc123"},
        )
        entry = player._track_entry(track, provider="spotify")

        assert entry["title"] == "Bohemian Rhapsody - Remastered 2011"
        assert entry["author"] == "Queen"
        assert entry["source"] == "spotify"
        assert entry["album"] == "Greatest Hits"
        assert entry["artwork_url"] == "https://i.scdn.co/image/abc123"

    def test_spotify_direct_stream_entry(self):
        """Direct stream sidecars — entry created from the original search result."""
        track = _mock_track(
            title="Levitating",
            author="Dua Lipa",
            uri="https://open.spotify.com/track/39LLxExYz6ewLAo9CGCLIH",
            length=203064,
            source="spotify",
            artwork="https://i.scdn.co/image/xyz789",
            extras={"albumName": "Future Nostalgia", "artworkUrl": "https://i.scdn.co/image/xyz789"},
        )
        entry = player._track_entry(track, provider="spotify")

        assert entry["title"] == "Levitating"
        assert entry["author"] == "Dua Lipa"
        assert entry["album"] == "Future Nostalgia"
        assert entry["artwork_url"] == "https://i.scdn.co/image/xyz789"


class TestTrackEntryTidal:
    """Tidal tracks via LavasRC."""

    def test_tidal_lavasrc_track(self):
        track = _mock_track(
            title="Blinding Lights",
            author="The Weeknd",
            uri="https://tidal.com/browse/track/123456789",
            length=200040,
            source="tidal",
            artwork="https://resources.tidal.com/images/abc/320x320.jpg",
            extras={"albumName": "After Hours", "artworkUrl": "https://resources.tidal.com/images/abc/320x320.jpg"},
        )
        entry = player._track_entry(track, provider="tidal")

        assert entry["title"] == "Blinding Lights"
        assert entry["author"] == "The Weeknd"
        assert entry["source"] == "tidal"
        assert entry["album"] == "After Hours"
        assert entry["artwork_url"] == "https://resources.tidal.com/images/abc/320x320.jpg"

    def test_tidal_track_with_dash_in_title(self):
        """Tidal title with ' - ' should NOT be split (it's a subtitle, not artist)."""
        track = _mock_track(
            title="Hey Ya! - Radio Mix / Club Mix",
            author="Outkast",
            uri="https://tidal.com/browse/track/987654321",
            length=234000,
            source="tidal",
            artwork="https://resources.tidal.com/images/def/320x320.jpg",
            extras={"albumName": "Speakerboxxx/The Love Below"},
        )
        entry = player._track_entry(track, provider="tidal")

        assert entry["title"] == "Hey Ya! - Radio Mix / Club Mix"
        assert entry["author"] == "Outkast"
        assert entry["album"] == "Speakerboxxx/The Love Below"


class TestTrackEntrySoundCloud:
    """SoundCloud tracks: title may or may not contain artist."""

    def test_soundcloud_with_artist_in_title(self):
        track = _mock_track(
            title="Flume - Never Be Like You feat. Kai",
            author="Flume",
            uri="https://soundcloud.com/flume/never-be-like-you",
            length=234000,
            source="soundcloud",
            artwork="https://i1.sndcdn.com/artworks-abc-large.jpg",
        )
        entry = player._track_entry(track, provider="soundcloud")

        assert entry["title"] == "Flume - Never Be Like You feat. Kai"
        assert entry["source"] == "soundcloud"
        assert entry["artwork_url"] == "https://i1.sndcdn.com/artworks-abc-large.jpg"

    def test_soundcloud_clean_title(self):
        track = _mock_track(
            title="Never Be Like You",
            author="Flume",
            uri="https://soundcloud.com/flume/never-be-like-you",
            length=234000,
            source="soundcloud",
            artwork="https://i1.sndcdn.com/artworks-def-large.jpg",
        )
        entry = player._track_entry(track, provider="soundcloud")

        assert entry["title"] == "Never Be Like You"
        assert entry["author"] == "Flume"


# ---------------------------------------------------------------------------
# Tests: _split_title source-aware behaviour
# ---------------------------------------------------------------------------


class TestSplitTitleSourceAware:
    """_split_title must respect source context to avoid mangling."""

    def test_youtube_splits_correctly(self):
        """YouTube: 'Artist - Song' format should split."""
        song, artist = player._split_title(
            "Queen - Bohemian Rhapsody", "QueenVEVO", source="youtube"
        )
        assert song == "Bohemian Rhapsody"
        assert artist == "Queen"

    def test_youtube_topic_stripped(self):
        """YouTube Music: ' - Topic' suffix on author should be stripped."""
        song, artist = player._split_title(
            "Bohemian Rhapsody", "Queen - Topic", source="youtube"
        )
        assert song == "Bohemian Rhapsody"
        assert artist == "Queen"

    def test_spotify_no_split_on_dash(self):
        """Spotify: title with ' - ' is a subtitle/version, NOT artist separator."""
        song, artist = player._split_title(
            "Bohemian Rhapsody - Remastered 2011", "Queen", source="spotify"
        )
        assert song == "Bohemian Rhapsody - Remastered 2011"
        assert artist == "Queen"

    def test_tidal_no_split_on_dash(self):
        """Tidal: same as Spotify — don't split on dash."""
        song, artist = player._split_title(
            "Hey Ya! - Radio Mix / Club Mix", "Outkast", source="tidal"
        )
        assert song == "Hey Ya! - Radio Mix / Club Mix"
        assert artist == "Outkast"

    def test_soundcloud_splits_like_youtube(self):
        """SoundCloud titles often use 'Artist - Song' format."""
        song, artist = player._split_title(
            "Flume - Never Be Like You", "Flume", source="soundcloud"
        )
        assert song == "Never Be Like You"
        assert artist == "Flume"

    def test_no_source_defaults_to_splitting(self):
        """When source is unknown/None, apply YouTube-style splitting."""
        song, artist = player._split_title(
            "Artist - Song Title", "SomeChannel", source=None
        )
        assert song == "Song Title"
        assert artist == "Artist"

    def test_spotify_plain_title_uses_author(self):
        """Spotify: plain title without dash uses author as artist."""
        song, artist = player._split_title(
            "Levitating", "Dua Lipa", source="spotify"
        )
        assert song == "Levitating"
        assert artist == "Dua Lipa"


# ---------------------------------------------------------------------------
# Tests: build_now_playing_embed_from_entry (used by /remote)
# ---------------------------------------------------------------------------


class TestBuildNowPlayingEmbedFromEntry:
    """Tests for the entry-based embed builder used by /remote."""

    def test_spotify_entry_shows_correct_fields(self):
        entry = {
            "title": "Levitating",
            "author": "Dua Lipa",
            "album": "Future Nostalgia",
            "duration": 203064,
            "source": "spotify",
            "webpage_url": "https://open.spotify.com/track/39LLxExYz6ewLAo9CGCLIH",
            "artwork_url": "https://i.scdn.co/image/xyz789",
        }
        embed = player.build_now_playing_embed_from_entry(entry)

        fields = {f.name: f.value for f in embed.fields}
        assert fields["Song"] == "Levitating"
        assert fields["Artist"] == "Dua Lipa"
        assert fields["Source"] == "Spotify"
        assert "Album" in fields
        assert fields["Album"] == "Future Nostalgia"
        assert embed.thumbnail.url == "https://i.scdn.co/image/xyz789"

    def test_tidal_entry_no_mangling(self):
        """Tidal entry with dash in title should NOT split into wrong artist."""
        entry = {
            "title": "Hey Ya! - Radio Mix / Club Mix",
            "author": "Outkast",
            "album": "Speakerboxxx/The Love Below",
            "duration": 234000,
            "source": "tidal",
            "webpage_url": "https://tidal.com/browse/track/987654321",
            "artwork_url": "https://resources.tidal.com/images/def/320x320.jpg",
        }
        embed = player.build_now_playing_embed_from_entry(entry)

        fields = {f.name: f.value for f in embed.fields}
        assert fields["Song"] == "Hey Ya! - Radio Mix / Club Mix"
        assert fields["Artist"] == "Outkast"
        assert fields["Source"] == "Tidal"
        assert fields["Album"] == "Speakerboxxx/The Love Below"
        assert embed.thumbnail.url == "https://resources.tidal.com/images/def/320x320.jpg"

    def test_youtube_entry_splits_title(self):
        """YouTube entry: 'Artist - Song' title should be split."""
        entry = {
            "title": "Queen - Bohemian Rhapsody (Official Video)",
            "author": "Queen Official",
            "duration": 354000,
            "source": "youtube",
            "webpage_url": "https://www.youtube.com/watch?v=fJ9rUzIMcZQ",
            "artwork_url": "https://i.ytimg.com/vi/fJ9rUzIMcZQ/maxresdefault.jpg",
        }
        embed = player.build_now_playing_embed_from_entry(entry)

        fields = {f.name: f.value for f in embed.fields}
        assert fields["Song"] == "Bohemian Rhapsody (Official Video)"
        assert fields["Artist"] == "Queen"
        assert fields["Source"] == "Youtube"
        assert embed.thumbnail.url == "https://i.ytimg.com/vi/fJ9rUzIMcZQ/maxresdefault.jpg"

    def test_soundcloud_entry(self):
        entry = {
            "title": "Flume - Never Be Like You feat. Kai",
            "author": "Flume",
            "duration": 234000,
            "source": "soundcloud",
            "webpage_url": "https://soundcloud.com/flume/never-be-like-you",
            "artwork_url": "https://i1.sndcdn.com/artworks-abc-large.jpg",
        }
        embed = player.build_now_playing_embed_from_entry(entry)

        fields = {f.name: f.value for f in embed.fields}
        assert fields["Song"] == "Never Be Like You feat. Kai"
        assert fields["Artist"] == "Flume"
        assert fields["Source"] == "Soundcloud"
        assert embed.thumbnail.url == "https://i1.sndcdn.com/artworks-abc-large.jpg"

    def test_http_source_mapped_to_spotify(self):
        """HTTP source with Spotify URL should show 'Spotify' label."""
        entry = {
            "title": "Levitating",
            "author": "Dua Lipa",
            "duration": 203064,
            "source": "http",
            "webpage_url": "https://open.spotify.com/track/39LLxExYz6ewLAo9CGCLIH",
        }
        embed = player.build_now_playing_embed_from_entry(entry)

        fields = {f.name: f.value for f in embed.fields}
        assert fields["Source"] == "Spotify"

    def test_http_source_mapped_to_tidal(self):
        """HTTP source with Tidal URL should show 'Tidal' label."""
        entry = {
            "title": "Blinding Lights",
            "author": "The Weeknd",
            "duration": 200040,
            "source": "http",
            "webpage_url": "https://tidal.com/browse/track/123456789",
        }
        embed = player.build_now_playing_embed_from_entry(entry)

        fields = {f.name: f.value for f in embed.fields}
        assert fields["Source"] == "Tidal"

    def test_missing_artwork_no_crash(self):
        """Entry without artwork_url should still build cleanly."""
        entry = {
            "title": "Some Song",
            "author": "Some Artist",
            "duration": 180000,
            "source": "youtube",
        }
        embed = player.build_now_playing_embed_from_entry(entry)

        assert embed.thumbnail is None or embed.thumbnail.url is None
        fields = {f.name: f.value for f in embed.fields}
        assert fields["Song"] is not None


# ---------------------------------------------------------------------------
# Tests: _build_now_playing_embed (live track object)
# ---------------------------------------------------------------------------


class TestBuildNowPlayingEmbedFromTrack:
    """Tests for the wavelink track-based embed builder."""

    def test_spotify_track_with_artwork(self):
        track = _mock_track(
            title="Levitating",
            author="Dua Lipa",
            uri="https://open.spotify.com/track/39LLxExYz6ewLAo9CGCLIH",
            length=203064,
            source="spotify",
            artwork="https://i.scdn.co/image/xyz789",
            album_name="Future Nostalgia",
        )
        embed = player._build_now_playing_embed(track)

        fields = {f.name: f.value for f in embed.fields}
        assert fields["Song"] == "Levitating"
        assert fields["Artist"] == "Dua Lipa"
        assert fields["Source"] == "Spotify"
        assert fields.get("Album") == "Future Nostalgia"
        assert embed.thumbnail.url == "https://i.scdn.co/image/xyz789"

    def test_tidal_track_no_title_mangling(self):
        """Tidal track with dash in title should NOT mangle song/artist."""
        track = _mock_track(
            title="Hey Ya! - Radio Mix / Club Mix",
            author="Outkast",
            uri="https://tidal.com/browse/track/987654321",
            length=234000,
            source="tidal",
            artwork="https://resources.tidal.com/images/def/320x320.jpg",
            album_name="Speakerboxxx/The Love Below",
        )
        embed = player._build_now_playing_embed(track)

        fields = {f.name: f.value for f in embed.fields}
        assert fields["Song"] == "Hey Ya! - Radio Mix / Club Mix"
        assert fields["Artist"] == "Outkast"

    def test_youtube_track_splits_title(self):
        track = _mock_track(
            title="Queen - Bohemian Rhapsody (Official Video)",
            author="Queen Official",
            uri="https://www.youtube.com/watch?v=fJ9rUzIMcZQ",
            length=354000,
            source="youtube",
            artwork="https://i.ytimg.com/vi/fJ9rUzIMcZQ/maxresdefault.jpg",
        )
        embed = player._build_now_playing_embed(track)

        fields = {f.name: f.value for f in embed.fields}
        assert fields["Song"] == "Bohemian Rhapsody (Official Video)"
        assert fields["Artist"] == "Queen"
        assert embed.thumbnail.url == "https://i.ytimg.com/vi/fJ9rUzIMcZQ/maxresdefault.jpg"

    def test_http_source_spotify_sidecar(self):
        """HTTP source with port 8802 in URI → Spotify label."""
        track = _mock_track(
            title="Unknown title",
            author="",
            uri="http://localhost:8802/stream/abc123",
            length=9223372036854775807,  # Long.MAX_VALUE
            source="http",
        )
        entry = {
            "title": "Levitating",
            "author": "Dua Lipa",
            "duration": 203064,
            "source": "spotify",
        }
        embed = player._build_now_playing_embed(track, entry)

        fields = {f.name: f.value for f in embed.fields}
        assert fields["Source"] == "Spotify"
        # Entry metadata should override garbage from HTTP source
        assert fields["Song"] == "Levitating"
        assert fields["Artist"] == "Dua Lipa"

    def test_http_source_tidal_sidecar(self):
        """HTTP source with port 8801 in URI → Tidal label."""
        track = _mock_track(
            title="Unknown title",
            author="",
            uri="http://localhost:8801/stream/123456",
            length=9223372036854775807,
            source="http",
        )
        entry = {
            "title": "Blinding Lights",
            "author": "The Weeknd",
            "duration": 200040,
            "source": "tidal",
        }
        embed = player._build_now_playing_embed(track, entry)

        fields = {f.name: f.value for f in embed.fields}
        assert fields["Source"] == "Tidal"
        assert fields["Song"] == "Blinding Lights"
        assert fields["Artist"] == "The Weeknd"
