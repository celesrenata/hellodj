"""Content classifier for unified playback routing.

Pure, synchronous module — no I/O, no async, no imports beyond stdlib.
Determines whether user input should be routed to audio or video backend.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal
from urllib.parse import urlparse

__all__ = ["ContentType", "ClassificationResult", "classify"]

# Maximum URL length before truncation
_MAX_URL_LENGTH = 2000

# Video file extensions (lowercase, without dot)
_VIDEO_EXTENSIONS = frozenset({"mp4", "webm", "mkv", "avi", "mov", "m4v"})

# Pattern for Tidal video paths: /video/<id> or /browse/video/<id>
_TIDAL_VIDEO_PATH_RE = re.compile(r"^/(browse/)?video/\d+", re.IGNORECASE)

# Pattern to detect URLs (has a scheme with ://)
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://")

# Exact YouTube hostnames matched by Rule 9
_YOUTUBE_EXACT_HOSTS = frozenset({
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
})


def _is_youtube_domain(hostname: str) -> bool:
    """Return True if *hostname* is a YouTube domain variant.

    Matches:
    - Exact hosts: youtube.com, www.youtube.com, m.youtube.com, youtu.be,
      www.youtu.be, youtube-nocookie.com, www.youtube-nocookie.com
    - Suffix: any hostname ending in .youtube.com (catches gaming.youtube.com,
      consent.youtube.com, future subdomains)

    Note: music.youtube.com is handled by Rule 3 (higher priority) so it
    never reaches Rule 9.
    """
    if hostname in _YOUTUBE_EXACT_HOSTS:
        return True
    if hostname.endswith(".youtube.com"):
        return True
    return False


class ContentType(Enum):
    """Playback content type."""

    AUDIO = "audio"
    VIDEO = "video"


@dataclass(frozen=True)
class ClassificationResult:
    """Output of classify()."""

    content_type: ContentType
    source_hint: str
    confidence: Literal["definite", "default"]


def classify(
    query: str,
    *,
    mode: Literal["auto", "audio", "video"] = "auto",
    attachment_content_type: str | None = None,
) -> ClassificationResult:
    """Classify input into audio or video.

    Rules (in priority order):
    1. Explicit mode override ("audio" or "video") → use that type directly.
    2. Attachment with video/ MIME type → VIDEO (definite).
    3. YouTube Music URL (music.youtube.com) → AUDIO (definite).
    4. Spotify URL (open.spotify.com) or spsearch: prefix → AUDIO (definite).
    5. Tidal URL with /video/<id> or /browse/video/<id> path → VIDEO (definite).
    6. Tidal URL (tidal.com) without /video/ path or tdsearch: prefix → AUDIO (definite).
    7. SoundCloud URL (soundcloud.com) → AUDIO (definite).
    8. URL ending in video extension (.mp4, .webm, .mkv, .avi, .mov, .m4v) → VIDEO (definite).
    9. YouTube domain URL (youtube.com, youtu.be, youtube-nocookie.com, *.youtube.com) → AUDIO (default).
    10. Unrecognized URL (has scheme, no known audio domain, no video ext) → AUDIO (default).
        Defaults to AUDIO since /play's primary intent is audio playback;
        explicit mode:video exists for video requests.
    11. Plain text query (no URL detected) → AUDIO (default).
    """
    # Rule 1: Explicit mode override
    if mode == "audio":
        return ClassificationResult(
            content_type=ContentType.AUDIO,
            source_hint="mode_override",
            confidence="definite",
        )
    if mode == "video":
        return ClassificationResult(
            content_type=ContentType.VIDEO,
            source_hint="mode_override",
            confidence="definite",
        )

    # Rule 2: Attachment with video/ MIME type
    if attachment_content_type is not None:
        if attachment_content_type.lower().startswith("video/"):
            return ClassificationResult(
                content_type=ContentType.VIDEO,
                source_hint="attachment",
                confidence="definite",
            )

    # Normalize query: strip whitespace
    q = query.strip()

    # Edge case: empty or whitespace-only → default audio search
    if not q:
        return ClassificationResult(
            content_type=ContentType.AUDIO,
            source_hint="search",
            confidence="default",
        )

    # Check for spsearch: prefix (before URL parsing)
    if q.lower().startswith("spsearch:"):
        return ClassificationResult(
            content_type=ContentType.AUDIO,
            source_hint="spotify",
            confidence="definite",
        )

    # Check for tdsearch: prefix (before URL parsing)
    if q.lower().startswith("tdsearch:"):
        return ClassificationResult(
            content_type=ContentType.AUDIO,
            source_hint="tidal",
            confidence="definite",
        )

    # Determine if input looks like a URL
    if not _URL_SCHEME_RE.match(q):
        # Rule 11: Plain text query
        return ClassificationResult(
            content_type=ContentType.AUDIO,
            source_hint="search",
            confidence="default",
        )

    # It's a URL — truncate if extremely long
    url_str = q[:_MAX_URL_LENGTH] if len(q) > _MAX_URL_LENGTH else q

    # Parse the URL
    parsed = urlparse(url_str)
    hostname = (parsed.hostname or "").lower()

    # Rule 3: YouTube Music URL
    if hostname == "music.youtube.com":
        return ClassificationResult(
            content_type=ContentType.AUDIO,
            source_hint="youtube_music",
            confidence="definite",
        )

    # Rule 4: Spotify URL
    if hostname == "open.spotify.com":
        return ClassificationResult(
            content_type=ContentType.AUDIO,
            source_hint="spotify",
            confidence="definite",
        )

    # Rules 5 & 6: Tidal URL
    if hostname in ("tidal.com", "www.tidal.com", "listen.tidal.com"):
        # Rule 5: Tidal video path
        if _TIDAL_VIDEO_PATH_RE.match(parsed.path):
            return ClassificationResult(
                content_type=ContentType.VIDEO,
                source_hint="tidal_video",
                confidence="definite",
            )
        # Rule 6: Tidal audio (no video path)
        return ClassificationResult(
            content_type=ContentType.AUDIO,
            source_hint="tidal",
            confidence="definite",
        )

    # Rule 7: SoundCloud URL
    if hostname in ("soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"):
        return ClassificationResult(
            content_type=ContentType.AUDIO,
            source_hint="soundcloud",
            confidence="definite",
        )

    # Rule 8: URL ending in video extension
    # Extract the path's file extension (ignoring query params and fragments)
    path_lower = parsed.path.lower()
    # Get the last segment's extension
    dot_idx = path_lower.rfind(".")
    if dot_idx != -1:
        ext = path_lower[dot_idx + 1 :]
        if ext in _VIDEO_EXTENSIONS:
            return ClassificationResult(
                content_type=ContentType.VIDEO,
                source_hint="direct_video",
                confidence="definite",
            )

    # Rule 9: YouTube video URL (broad hostname matching)
    if _is_youtube_domain(hostname):
        return ClassificationResult(
            content_type=ContentType.AUDIO,
            source_hint="youtube",
            confidence="default",
        )

    # Rule 10: Unrecognized URL — defaults to AUDIO since /play's primary
    # intent is audio playback; explicit mode:video exists for video requests.
    return ClassificationResult(
        content_type=ContentType.AUDIO,
        source_hint="unknown_url",
        confidence="default",
    )
