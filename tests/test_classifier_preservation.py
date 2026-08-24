"""Preservation property-based tests for classifier.py.

These tests capture EXISTING correct behavior that must be preserved
through the bug fix. They run on UNFIXED code and must PASS.

**Validates: Requirements 3.3, 3.4, 3.5, 3.8, 3.9**
"""

from __future__ import annotations

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from playback.classifier import ContentType, ClassificationResult, classify


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Safe path segments that won't accidentally produce video extensions
_SAFE_PATH_SEGMENTS = st.from_regex(r"[a-z0-9_\-]{1,20}", fullmatch=True)

_safe_paths = st.lists(_SAFE_PATH_SEGMENTS, min_size=1, max_size=4).map(
    lambda parts: "/" + "/".join(parts)
)

# Spotify hosts
_SPOTIFY_HOSTS = ["open.spotify.com"]

# Tidal hosts
_TIDAL_HOSTS = ["tidal.com", "www.tidal.com", "listen.tidal.com"]

# SoundCloud hosts
_SOUNDCLOUD_HOSTS = ["soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"]

# Video extensions
_VIDEO_EXTENSIONS = ["mp4", "webm", "mkv", "avi", "mov", "m4v"]

# Tidal video paths: /video/<digits> or /browse/video/<digits>
_tidal_video_ids = st.integers(min_value=1, max_value=999999999)
_tidal_video_paths = st.one_of(
    _tidal_video_ids.map(lambda n: f"/video/{n}"),
    _tidal_video_ids.map(lambda n: f"/browse/video/{n}"),
)

# Tidal non-video paths (must NOT match /video/<id> or /browse/video/<id>)
_tidal_audio_paths = st.sampled_from([
    "/track/12345",
    "/album/67890",
    "/playlist/abcdef",
    "/artist/999",
    "/browse/tracks",
    "/browse/albums",
    "/",
    "/mix/abc123",
])

# Arbitrary queries (for mode override tests)
_arbitrary_queries = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Zs")),
    min_size=1,
    max_size=80,
)

# Plain text queries (no URL scheme, no recognized prefix)
_plain_text_queries = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "Zs"),
        blacklist_characters=":"
    ),
    min_size=1,
    max_size=100,
).filter(
    lambda s: not s.strip().startswith(("http://", "https://", "ftp://", "spsearch:", "tdsearch:"))
)

# Unrecognized domains for video extension test
_unrecognized_domains = st.from_regex(
    r"[a-z]{3,12}\.(org|net|io|dev|xyz|co)", fullmatch=True
).filter(
    lambda d: d not in (
        "youtube.com", "youtu.be", "soundcloud.com", "tidal.com", "spotify.com",
    )
    and not d.startswith("music.")
    and not d.startswith("open.")
    and not d.startswith("www.")
    and not d.startswith("m.")
    and not d.startswith("listen.")
)


# ---------------------------------------------------------------------------
# Preservation Property: Spotify URLs → AUDIO definite, source_hint "spotify"
# ---------------------------------------------------------------------------


class TestPreservationSpotify:
    """For all Spotify URLs (open.spotify.com/*), classify returns AUDIO definite
    with source_hint "spotify".

    **Validates: Requirements 3.3**
    """

    @settings(max_examples=100)
    @given(path=_safe_paths)
    def test_spotify_urls_audio_definite(self, path: str) -> None:
        url = f"https://open.spotify.com{path}"
        result = classify(url)
        assert result.content_type == ContentType.AUDIO
        assert result.confidence == "definite"
        assert result.source_hint == "spotify"


# ---------------------------------------------------------------------------
# Preservation Property: Tidal video URLs → VIDEO definite, source_hint "tidal_video"
# ---------------------------------------------------------------------------


class TestPreservationTidalVideo:
    """For all Tidal video URLs (tidal.com/(browse/)?video/\\d+), classify returns
    VIDEO definite with source_hint "tidal_video".

    **Validates: Requirements 3.4**
    """

    @settings(max_examples=100)
    @given(
        host=st.sampled_from(_TIDAL_HOSTS),
        path=_tidal_video_paths,
    )
    def test_tidal_video_urls_video_definite(self, host: str, path: str) -> None:
        url = f"https://{host}{path}"
        result = classify(url)
        assert result.content_type == ContentType.VIDEO
        assert result.confidence == "definite"
        assert result.source_hint == "tidal_video"


# ---------------------------------------------------------------------------
# Preservation Property: Tidal non-video URLs → AUDIO definite, source_hint "tidal"
# ---------------------------------------------------------------------------


class TestPreservationTidalAudio:
    """For all Tidal non-video URLs, classify returns AUDIO definite with
    source_hint "tidal".

    **Validates: Requirements 3.4**
    """

    @settings(max_examples=100)
    @given(
        host=st.sampled_from(_TIDAL_HOSTS),
        path=_tidal_audio_paths,
    )
    def test_tidal_audio_urls_audio_definite(self, host: str, path: str) -> None:
        url = f"https://{host}{path}"
        result = classify(url)
        assert result.content_type == ContentType.AUDIO
        assert result.confidence == "definite"
        assert result.source_hint == "tidal"


# ---------------------------------------------------------------------------
# Preservation Property: SoundCloud URLs → AUDIO definite, source_hint "soundcloud"
# ---------------------------------------------------------------------------


class TestPreservationSoundCloud:
    """For all SoundCloud URLs (soundcloud.com/*), classify returns AUDIO definite
    with source_hint "soundcloud".

    **Validates: Requirements 3.3**
    """

    @settings(max_examples=100)
    @given(
        host=st.sampled_from(_SOUNDCLOUD_HOSTS),
        path=_safe_paths,
    )
    def test_soundcloud_urls_audio_definite(self, host: str, path: str) -> None:
        url = f"https://{host}{path}"
        result = classify(url)
        assert result.content_type == ContentType.AUDIO
        assert result.confidence == "definite"
        assert result.source_hint == "soundcloud"


# ---------------------------------------------------------------------------
# Preservation Property: Video extension URLs → VIDEO definite, source_hint "direct_video"
# ---------------------------------------------------------------------------


class TestPreservationVideoExtension:
    """For all URLs ending in video extensions (.mp4, .webm, .mkv, .avi, .mov, .m4v),
    classify returns VIDEO definite with source_hint "direct_video".

    **Validates: Requirements 3.9**
    """

    @settings(max_examples=100)
    @given(
        domain=_unrecognized_domains,
        path_prefix=_SAFE_PATH_SEGMENTS,
        ext=st.sampled_from(_VIDEO_EXTENSIONS),
    )
    def test_video_extension_urls_video_definite(self, domain: str, path_prefix: str, ext: str) -> None:
        url = f"https://{domain}/{path_prefix}/file.{ext}"
        result = classify(url)
        assert result.content_type == ContentType.VIDEO
        assert result.confidence == "definite"
        assert result.source_hint == "direct_video"


# ---------------------------------------------------------------------------
# Preservation Property: mode="video" → VIDEO definite, source_hint "mode_override"
# ---------------------------------------------------------------------------


class TestPreservationModeVideo:
    """For any query with mode="video", classify returns VIDEO definite with
    source_hint "mode_override".

    **Validates: Requirements 3.5**
    """

    @settings(max_examples=100)
    @given(query=_arbitrary_queries)
    def test_mode_video_override(self, query: str) -> None:
        result = classify(query, mode="video")
        assert result.content_type == ContentType.VIDEO
        assert result.confidence == "definite"
        assert result.source_hint == "mode_override"


# ---------------------------------------------------------------------------
# Preservation Property: mode="audio" → AUDIO definite, source_hint "mode_override"
# ---------------------------------------------------------------------------


class TestPreservationModeAudio:
    """For any query with mode="audio", classify returns AUDIO definite with
    source_hint "mode_override".

    **Validates: Requirements 3.5**
    """

    @settings(max_examples=100)
    @given(query=_arbitrary_queries)
    def test_mode_audio_override(self, query: str) -> None:
        result = classify(query, mode="audio")
        assert result.content_type == ContentType.AUDIO
        assert result.confidence == "definite"
        assert result.source_hint == "mode_override"


# ---------------------------------------------------------------------------
# Preservation Property: Plain text queries → AUDIO default, source_hint "search"
# ---------------------------------------------------------------------------


class TestPreservationPlainText:
    """For all plain text queries (no URL scheme), classify returns AUDIO default
    with source_hint "search".

    **Validates: Requirements 3.8**
    """

    @settings(max_examples=100)
    @given(query=_plain_text_queries)
    def test_plain_text_audio_default(self, query: str) -> None:
        assume(query.strip())  # Skip empty/whitespace-only
        assume(not query.strip().lower().startswith("spsearch:"))
        assume(not query.strip().lower().startswith("tdsearch:"))
        result = classify(query)
        assert result.content_type == ContentType.AUDIO
        assert result.confidence == "default"
        assert result.source_hint == "search"


# ---------------------------------------------------------------------------
# Preservation Property: music.youtube.com → AUDIO definite, source_hint "youtube_music"
# ---------------------------------------------------------------------------


class TestPreservationYouTubeMusic:
    """For music.youtube.com URLs, classify returns AUDIO definite with
    source_hint "youtube_music".

    **Validates: Requirements 3.3**
    """

    @settings(max_examples=100)
    @given(path=_safe_paths)
    def test_youtube_music_urls_audio_definite(self, path: str) -> None:
        url = f"https://music.youtube.com{path}"
        result = classify(url)
        assert result.content_type == ContentType.AUDIO
        assert result.confidence == "definite"
        assert result.source_hint == "youtube_music"
