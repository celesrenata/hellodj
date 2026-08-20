"""HelloDJ — Source router: classify user input for video source dispatch.

Pure classification logic — no async, no network calls. Determines which
resolver should handle a given user input based on URL patterns, domain
matching, and prefix detection.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from video.sources import is_video_extension

# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

_YOUTUBE_DOMAINS: frozenset[str] = frozenset(
    {"youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"}
)

_TIDAL_DOMAINS: frozenset[str] = frozenset(
    {"tidal.com", "www.tidal.com", "listen.tidal.com"}
)

_TIDAL_VIDEO_PATH_RE: re.Pattern[str] = re.compile(r"/(?:browse/)?video/(\d+)")

_TIDAL_SEARCH_PREFIX: str = "tidal:"

# ---------------------------------------------------------------------------
# Type definitions
# ---------------------------------------------------------------------------

SourceType = Literal[
    "youtube_url",
    "youtube_search",
    "tidal_url",
    "tidal_search",
    "general_url",
    "upload",
]

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_url(text: str) -> bool:
    """Return True if *text* looks like a URL (has http/https scheme and netloc)."""
    parsed = urlparse(text.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def extract_tidal_video_id(url: str) -> int | None:
    """Extract numeric video ID from a Tidal video URL.

    Matches paths like:
        - /video/12345
        - /browse/video/12345

    Returns the integer video ID, or None if the URL is not a recognized
    Tidal video URL.
    """
    parsed = urlparse(url.strip())
    hostname = (parsed.hostname or "").lower()

    if hostname not in _TIDAL_DOMAINS:
        return None

    match = _TIDAL_VIDEO_PATH_RE.search(parsed.path)
    if match is None:
        return None

    return int(match.group(1))


def classify_input(query: str, has_attachment: bool = False) -> SourceType:
    """Classify user input into a source type for routing.

    Priority order:
        1. Attachment present → "upload"
        2. YouTube URL → "youtube_url"
        3. Tidal URL with /video/ path → "tidal_url"
        4. tidal: prefix → "tidal_search"
        5. URL with video extension → "general_url"
        6. URL without video extension → "general_url"
        7. Non-URL text → "youtube_search"

    Args:
        query: The user-provided query string (URL, search text, or prefixed search).
        has_attachment: Whether a Discord file attachment is present.

    Returns:
        The classified SourceType literal value.
    """
    # Priority 1: attachment takes precedence over everything
    if has_attachment:
        return "upload"

    text = query.strip()

    # Priority 2–6: check if input is a URL
    if is_url(text):
        parsed = urlparse(text)
        hostname = (parsed.hostname or "").lower()

        # Priority 2: YouTube URL
        if hostname in _YOUTUBE_DOMAINS:
            return "youtube_url"

        # Priority 3: Tidal URL with /video/ path
        if hostname in _TIDAL_DOMAINS and _TIDAL_VIDEO_PATH_RE.search(parsed.path):
            return "tidal_url"

        # Priority 5/6: general URL (with or without video extension)
        return "general_url"

    # Non-URL text paths

    # Priority 4: tidal: prefix search
    if text.lower().startswith(_TIDAL_SEARCH_PREFIX):
        return "tidal_search"

    # Priority 7: default to YouTube search
    return "youtube_search"
