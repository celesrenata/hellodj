"""Property-based tests for ContentClassifier.

Covers Properties 3–6 from the unified-playback design:
- Property 3: Audio domain classification
- Property 4: Video indicator classification
- Property 5: Default audio classification for ambiguous and text inputs
- Property 6: Unknown URL defaults to video
"""

from __future__ import annotations

from hypothesis import given, settings, assume
from hypothesis import strategies as st

from playback.classifier import ContentType, classify


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Audio domains recognized by the classifier
_YOUTUBE_MUSIC_HOSTS = ["music.youtube.com"]
_SPOTIFY_HOSTS = ["open.spotify.com"]
_SOUNDCLOUD_HOSTS = ["soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"]
_TIDAL_HOSTS = ["tidal.com", "www.tidal.com", "listen.tidal.com"]

# Video extensions recognized by the classifier
_VIDEO_EXTENSIONS = ["mp4", "webm", "mkv", "avi", "mov", "m4v"]

# Non-video path segments (avoid accidentally generating video extensions)
_SAFE_PATH_SEGMENTS = st.from_regex(r"[a-z0-9_\-]{1,20}", fullmatch=True)

# Generate a random path that does NOT end in a video extension
_safe_paths = st.lists(_SAFE_PATH_SEGMENTS, min_size=1, max_size=4).map(
    lambda parts: "/" + "/".join(parts)
)

# Tidal non-video paths (anything that doesn't match /video/<id> or /browse/video/<id>)
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

# Tidal video paths: /video/<digits> or /browse/video/<digits>
_tidal_video_ids = st.integers(min_value=1, max_value=999999999)
_tidal_video_paths = st.one_of(
    _tidal_video_ids.map(lambda n: f"/video/{n}"),
    _tidal_video_ids.map(lambda n: f"/browse/video/{n}"),
)

# YouTube watch-style URLs
_youtube_watch_hosts = ["youtube.com", "www.youtube.com", "m.youtube.com"]
_youtube_short_hosts = ["youtu.be", "www.youtu.be"]

_video_ids = st.from_regex(r"[A-Za-z0-9_\-]{11}", fullmatch=True)

# Random text queries (no URL scheme, no recognized prefix)
_plain_text_queries = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N", "P", "Zs"),
        blacklist_characters=":"
    ),
    min_size=1,
    max_size=100,
).filter(lambda s: not s.strip().startswith(("http://", "https://", "ftp://", "spsearch:", "tdsearch:")))

# Domains that are NOT recognized audio platforms or YouTube
_unrecognized_domains = st.from_regex(
    r"[a-z]{3,12}\.(org|net|io|dev|xyz|co)", fullmatch=True
).filter(
    lambda d: d not in (
        "youtube.com", "youtu.be", "soundcloud.com", "tidal.com",
        "spotify.com",
    )
    and not d.startswith("music.")
    and not d.startswith("open.")
    and not d.startswith("www.")
    and not d.startswith("m.")
    and not d.startswith("listen.")
)

# MIME types starting with "video/"
_video_mime_subtypes = st.sampled_from([
    "mp4", "webm", "x-matroska", "avi", "quicktime", "x-msvideo",
    "ogg", "3gpp", "mpeg", "x-flv",
])
_video_mime_types = _video_mime_subtypes.map(lambda sub: f"video/{sub}")


# ---------------------------------------------------------------------------
# Property 3: Audio domain classification
# ---------------------------------------------------------------------------

# Feature: unified-playback, Property 3: Audio domain classification


class TestProperty3AudioDomainClassification:
    """For any URL whose hostname belongs to a recognized audio platform
    (music.youtube.com, open.spotify.com, soundcloud.com, tidal.com without
    /video/ path) or query with a recognized audio prefix (spsearch:, tdsearch:),
    the ContentClassifier SHALL return content_type=AUDIO with confidence="definite".

    **Validates: Requirements 3.1, 3.2, 3.3, 3.4**
    """

    @settings(max_examples=100)
    @given(path=_safe_paths)
    def test_youtube_music_urls(self, path: str) -> None:
        """YouTube Music URLs → AUDIO (definite)."""
        url = f"https://music.youtube.com{path}"
        result = classify(url)
        assert result.content_type == ContentType.AUDIO
        assert result.confidence == "definite"

    @settings(max_examples=100)
    @given(path=_safe_paths)
    def test_spotify_urls(self, path: str) -> None:
        """Spotify URLs → AUDIO (definite)."""
        url = f"https://open.spotify.com{path}"
        result = classify(url)
        assert result.content_type == ContentType.AUDIO
        assert result.confidence == "definite"

    @settings(max_examples=100)
    @given(
        host=st.sampled_from(_SOUNDCLOUD_HOSTS),
        path=_safe_paths,
    )
    def test_soundcloud_urls(self, host: str, path: str) -> None:
        """SoundCloud URLs → AUDIO (definite)."""
        url = f"https://{host}{path}"
        result = classify(url)
        assert result.content_type == ContentType.AUDIO
        assert result.confidence == "definite"

    @settings(max_examples=100)
    @given(
        host=st.sampled_from(_TIDAL_HOSTS),
        path=_tidal_audio_paths,
    )
    def test_tidal_audio_urls(self, host: str, path: str) -> None:
        """Tidal URLs without /video/ path → AUDIO (definite)."""
        url = f"https://{host}{path}"
        result = classify(url)
        assert result.content_type == ContentType.AUDIO
        assert result.confidence == "definite"

    @settings(max_examples=100)
    @given(query=st.text(min_size=1, max_size=80))
    def test_spsearch_prefix(self, query: str) -> None:
        """spsearch: prefixed queries → AUDIO (definite)."""
        result = classify(f"spsearch:{query}")
        assert result.content_type == ContentType.AUDIO
        assert result.confidence == "definite"

    @settings(max_examples=100)
    @given(query=st.text(min_size=1, max_size=80))
    def test_tdsearch_prefix(self, query: str) -> None:
        """tdsearch: prefixed queries → AUDIO (definite)."""
        result = classify(f"tdsearch:{query}")
        assert result.content_type == ContentType.AUDIO
        assert result.confidence == "definite"


# ---------------------------------------------------------------------------
# Property 4: Video indicator classification
# ---------------------------------------------------------------------------

# Feature: unified-playback, Property 4: Video indicator classification


class TestProperty4VideoIndicatorClassification:
    """For any input that is either (a) an attachment with a MIME type starting
    with "video/" or (b) a URL ending in a video extension (.mp4, .webm, .mkv,
    .avi, .mov, .m4v) or (c) a Tidal URL whose path matches /video/<id> or
    /browse/video/<id>, the ContentClassifier SHALL return content_type=VIDEO
    with confidence="definite".

    **Validates: Requirements 3.5, 3.6, 3.9**
    """

    @settings(max_examples=100)
    @given(mime_type=_video_mime_types)
    def test_video_attachment_mime_type(self, mime_type: str) -> None:
        """Attachment with video/ MIME type → VIDEO (definite)."""
        result = classify("some search query", attachment_content_type=mime_type)
        assert result.content_type == ContentType.VIDEO
        assert result.confidence == "definite"

    @settings(max_examples=100)
    @given(
        domain=_unrecognized_domains,
        path_prefix=_SAFE_PATH_SEGMENTS,
        ext=st.sampled_from(_VIDEO_EXTENSIONS),
    )
    def test_url_with_video_extension(self, domain: str, path_prefix: str, ext: str) -> None:
        """URL ending in video extension → VIDEO (definite)."""
        url = f"https://{domain}/{path_prefix}/file.{ext}"
        result = classify(url)
        assert result.content_type == ContentType.VIDEO
        assert result.confidence == "definite"

    @settings(max_examples=100)
    @given(
        host=st.sampled_from(_TIDAL_HOSTS),
        path=_tidal_video_paths,
    )
    def test_tidal_video_urls(self, host: str, path: str) -> None:
        """Tidal URL with /video/<id> or /browse/video/<id> → VIDEO (definite)."""
        url = f"https://{host}{path}"
        result = classify(url)
        assert result.content_type == ContentType.VIDEO
        assert result.confidence == "definite"


# ---------------------------------------------------------------------------
# Property 5: Default audio classification for ambiguous and text inputs
# ---------------------------------------------------------------------------

# Feature: unified-playback, Property 5: Default audio classification for ambiguous and text inputs


class TestProperty5DefaultAudioClassification:
    """For any YouTube video URL (youtube.com/watch, youtu.be) or plain text
    search query without URL or recognized prefix, the ContentClassifier SHALL
    return content_type=AUDIO with confidence="default".

    **Validates: Requirements 3.7, 3.8**
    """

    @settings(max_examples=100)
    @given(
        host=st.sampled_from(_youtube_watch_hosts),
        video_id=_video_ids,
    )
    def test_youtube_watch_urls(self, host: str, video_id: str) -> None:
        """YouTube watch URLs → AUDIO (default)."""
        url = f"https://{host}/watch?v={video_id}"
        result = classify(url)
        assert result.content_type == ContentType.AUDIO
        assert result.confidence == "default"

    @settings(max_examples=100)
    @given(video_id=_video_ids)
    def test_youtu_be_urls(self, video_id: str) -> None:
        """youtu.be short URLs → AUDIO (default)."""
        url = f"https://youtu.be/{video_id}"
        result = classify(url)
        assert result.content_type == ContentType.AUDIO
        assert result.confidence == "default"

    @settings(max_examples=100)
    @given(query=_plain_text_queries)
    def test_plain_text_queries(self, query: str) -> None:
        """Plain text search queries → AUDIO (default)."""
        assume(query.strip())  # Skip empty/whitespace-only
        assume(not query.strip().lower().startswith("spsearch:"))
        assume(not query.strip().lower().startswith("tdsearch:"))
        result = classify(query)
        assert result.content_type == ContentType.AUDIO
        assert result.confidence == "default"


# ---------------------------------------------------------------------------
# Property 6: Unknown URL defaults to video
# ---------------------------------------------------------------------------

# Feature: unified-playback, Property 6: Unknown URL defaults to video


class TestProperty6UnknownUrlDefaultsToAudio:
    """For any URL that does not match a recognized audio domain (YouTube,
    Spotify, SoundCloud, Tidal) and does not end in a recognized video extension,
    the ContentClassifier SHALL return content_type=AUDIO with confidence="default".

    **Validates: Requirements 3.10**
    """

    @settings(max_examples=100)
    @given(
        domain=_unrecognized_domains,
        path=_safe_paths,
    )
    def test_unrecognized_url_no_video_extension(self, domain: str, path: str) -> None:
        """Unrecognized domain + no video extension → AUDIO (default)."""
        # Ensure path doesn't accidentally end with a video extension
        url = f"https://{domain}{path}"
        # Double-check our generator didn't produce a video extension ending
        lower_path = path.lower()
        dot_idx = lower_path.rfind(".")
        if dot_idx != -1:
            ext = lower_path[dot_idx + 1:]
            assume(ext not in {"mp4", "webm", "mkv", "avi", "mov", "m4v"})

        result = classify(url)
        assert result.content_type == ContentType.AUDIO
        assert result.confidence == "default"
