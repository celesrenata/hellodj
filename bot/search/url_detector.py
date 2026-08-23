"""URL detection for recognized music platform links.

Identifies platform URLs and bypasses search entirely, returning
the platform name and original URL for direct resolution.
"""

from __future__ import annotations

import re


# Patterns match the domain + required path prefix.
# Each tuple: (compiled regex, platform_name)
_PLATFORM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Spotify: open.spotify.com/track/... or spotify.com/track/...
    (re.compile(
        r"https?://(?:open\.)?spotify\.com/track/",
        re.IGNORECASE,
    ), "Spotify"),
    # Tidal: tidal.com/browse/track/... (must check before tidal.com/track/)
    (re.compile(
        r"https?://(?:www\.|listen\.)?tidal\.com/browse/track/",
        re.IGNORECASE,
    ), "Tidal"),
    # Tidal: tidal.com/track/...
    (re.compile(
        r"https?://(?:www\.|listen\.)?tidal\.com/track/",
        re.IGNORECASE,
    ), "Tidal"),
    # YouTube: youtube.com/watch...
    (re.compile(
        r"https?://(?:www\.|m\.)?youtube\.com/watch",
        re.IGNORECASE,
    ), "YouTube"),
    # YouTube short: youtu.be/...
    (re.compile(
        r"https?://(?:www\.)?youtu\.be/",
        re.IGNORECASE,
    ), "YouTube"),
    # SoundCloud: soundcloud.com/...
    (re.compile(
        r"https?://(?:www\.|m\.)?soundcloud\.com/",
        re.IGNORECASE,
    ), "SoundCloud"),
]

# Quick scheme check to avoid regex on non-URL strings
_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)


class URLDetector:
    """Detects recognized music platform URLs in search queries."""

    @staticmethod
    def detect(query: str) -> tuple[str, str] | None:
        """Returns (platform_name, url) if query is a recognized platform URL, else None.

        Recognized patterns:
        - spotify.com/track/...
        - tidal.com/track/... or tidal.com/browse/track/...
        - youtube.com/watch?... or youtu.be/...
        - soundcloud.com/...

        Handles URLs with query parameters, fragments, and additional path segments.
        The scheme check is case-insensitive (HTTP:// and https:// both work).
        """
        stripped = query.strip()
        if not _SCHEME_RE.match(stripped):
            return None

        for pattern, platform_name in _PLATFORM_PATTERNS:
            if pattern.search(stripped):
                return (platform_name, stripped)

        return None
