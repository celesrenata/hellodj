"""Content classifier for unified playback routing.

Pure, synchronous module — no I/O, no async, only the standard library.
Determines whether a play request should be routed to the audio backend, the
video backend, or the radio (live-stream) backend.

The audio/video rules are ported from the legacy on-prem classifier and
extended with a **radio** class (design: "classifier.py (audio/video/radio
classification)"). Radio is detected from an explicit ``mode="radio"``, a
``radio:`` query prefix, or a live-stream playlist/manifest URL (``.m3u``,
``.m3u8``, ``.pls``, ``icecast``/``shoutcast`` hosts).

Requirements: 6.1, 6.4
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Literal
from urllib.parse import urlparse

__all__ = ["ContentType", "ClassificationResult", "classify"]

#: Maximum URL length inspected before truncation.
_MAX_URL_LENGTH = 2000

#: Video file extensions (lowercase, without the leading dot).
_VIDEO_EXTENSIONS = frozenset({"mp4", "webm", "mkv", "avi", "mov", "m4v"})

#: Live-stream / radio playlist and manifest extensions.
_RADIO_EXTENSIONS = frozenset({"m3u", "m3u8", "pls", "asx", "xspf"})

#: Substrings in a hostname that indicate an internet-radio streaming server.
_RADIO_HOST_HINTS = ("icecast", "shoutcast", "radio.", "somafm", "streamtheworld")

#: Pattern for Tidal video paths: ``/video/<id>`` or ``/browse/video/<id>``.
_TIDAL_VIDEO_PATH_RE = re.compile(r"^/(browse/)?video/\d+", re.IGNORECASE)

#: Pattern to detect a URL (a scheme followed by ``://``).
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://")

#: Exact YouTube hostnames matched by the YouTube rule.
_YOUTUBE_EXACT_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
        "www.youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)

Mode = Literal["auto", "audio", "video", "radio"]
Confidence = Literal["definite", "default"]


class ContentType(Enum):
    """Playback content type produced by :func:`classify`."""

    AUDIO = "audio"
    VIDEO = "video"
    RADIO = "radio"


@dataclass(frozen=True)
class ClassificationResult:
    """The outcome of :func:`classify`.

    Attributes:
        content_type: The routed backend class (audio / video / radio).
        source_hint: A short tag naming the rule/source that matched.
        confidence: ``"definite"`` when a specific rule matched, ``"default"``
            when the fallback applied.
    """

    content_type: ContentType
    source_hint: str
    confidence: Confidence


def _is_youtube_domain(hostname: str) -> bool:
    """Return whether ``hostname`` is a YouTube domain variant.

    Matches the exact YouTube hosts plus any ``*.youtube.com`` subdomain.
    ``music.youtube.com`` is handled by a higher-priority rule and never
    reaches this check.
    """
    if hostname in _YOUTUBE_EXACT_HOSTS:
        return True
    return hostname.endswith(".youtube.com")


def _is_radio_host(hostname: str) -> bool:
    """Return whether ``hostname`` looks like an internet-radio server."""
    return any(hint in hostname for hint in _RADIO_HOST_HINTS)


def _path_extension(path: str) -> str | None:
    """Return the lowercase file extension of ``path`` or ``None``."""
    path_lower = path.lower()
    dot_idx = path_lower.rfind(".")
    if dot_idx == -1:
        return None
    return path_lower[dot_idx + 1 :]


def _result(content_type: ContentType, hint: str, confidence: Confidence) -> ClassificationResult:
    """Construct a :class:`ClassificationResult` (small readability helper)."""
    return ClassificationResult(
        content_type=content_type,
        source_hint=hint,
        confidence=confidence,
    )


def _classify_mode_override(mode: Mode) -> ClassificationResult | None:
    """Return an explicit-mode result, or ``None`` when ``mode == "auto"``."""
    if mode == "audio":
        return _result(ContentType.AUDIO, "mode_override", "definite")
    if mode == "video":
        return _result(ContentType.VIDEO, "mode_override", "definite")
    if mode == "radio":
        return _result(ContentType.RADIO, "mode_override", "definite")
    return None


def _classify_prefix(query_lower: str) -> ClassificationResult | None:
    """Classify by a recognized ``scheme:``-style query prefix."""
    if query_lower.startswith("radio:"):
        return _result(ContentType.RADIO, "radio", "definite")
    if query_lower.startswith("spsearch:"):
        return _result(ContentType.AUDIO, "spotify", "definite")
    if query_lower.startswith("tdsearch:"):
        return _result(ContentType.AUDIO, "tidal", "definite")
    return None


def _classify_known_host(hostname: str, path: str) -> ClassificationResult | None:
    """Classify a URL by its known streaming-service hostname."""
    if hostname == "music.youtube.com":
        return _result(ContentType.AUDIO, "youtube_music", "definite")
    if hostname == "open.spotify.com":
        return _result(ContentType.AUDIO, "spotify", "definite")
    if hostname in ("tidal.com", "www.tidal.com", "listen.tidal.com"):
        if _TIDAL_VIDEO_PATH_RE.match(path):
            return _result(ContentType.VIDEO, "tidal_video", "definite")
        return _result(ContentType.AUDIO, "tidal", "definite")
    if hostname in ("soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"):
        return _result(ContentType.AUDIO, "soundcloud", "definite")
    return None


def _classify_url(url_str: str) -> ClassificationResult:
    """Classify a value already known to be a URL."""
    parsed = urlparse(url_str)
    hostname = (parsed.hostname or "").lower()
    path = parsed.path

    known = _classify_known_host(hostname, path)
    if known is not None:
        return known

    extension = _path_extension(path)
    if extension in _RADIO_EXTENSIONS:
        return _result(ContentType.RADIO, "stream_manifest", "definite")
    if _is_radio_host(hostname):
        return _result(ContentType.RADIO, "radio_host", "definite")
    if extension in _VIDEO_EXTENSIONS:
        return _result(ContentType.VIDEO, "direct_video", "definite")
    if _is_youtube_domain(hostname):
        return _result(ContentType.AUDIO, "youtube", "default")

    # Unrecognized URL: default to audio, matching /play's primary intent.
    return _result(ContentType.AUDIO, "unknown_url", "default")


def classify(
    query: str,
    *,
    mode: Mode = "auto",
    attachment_content_type: str | None = None,
) -> ClassificationResult:
    """Classify a play request into audio, video, or radio.

    Rules are applied in priority order:

    1. Explicit ``mode`` override (``audio`` / ``video`` / ``radio``).
    2. Attachment with a ``video/`` or ``audio/`` MIME type.
    3. A recognized query prefix (``radio:`` / ``spsearch:`` / ``tdsearch:``).
    4. Empty/whitespace query → audio search default.
    5. Plain-text (non-URL) query → audio search default.
    6. URL rules: known hosts, radio manifests/hosts, video extensions,
       YouTube, then an unknown-URL audio default.

    Args:
        query: The raw play request (URL or free-text search).
        mode: Optional caller-forced content class.
        attachment_content_type: MIME type of an attached file, if any.

    Returns:
        The :class:`ClassificationResult` describing the routed backend.
    """
    override = _classify_mode_override(mode)
    if override is not None:
        return override

    if attachment_content_type is not None:
        mime = attachment_content_type.lower()
        if mime.startswith("video/"):
            return _result(ContentType.VIDEO, "attachment", "definite")
        if mime.startswith("audio/"):
            return _result(ContentType.AUDIO, "attachment", "definite")

    stripped = query.strip()

    prefix = _classify_prefix(stripped.lower())
    if prefix is not None:
        return prefix

    if not stripped:
        return _result(ContentType.AUDIO, "search", "default")

    if not _URL_SCHEME_RE.match(stripped):
        return _result(ContentType.AUDIO, "search", "default")

    url_str = stripped[:_MAX_URL_LENGTH] if len(stripped) > _MAX_URL_LENGTH else stripped
    return _classify_url(url_str)
