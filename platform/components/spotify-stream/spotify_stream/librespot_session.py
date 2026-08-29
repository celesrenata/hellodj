"""librespot Session build + per-user track loading/transcode (Spotify).

This module holds ALL librespot-specific logic for the multi-tenant Spotify data
plane (multi-tenant-source-streaming task 2.3), so the generic pool
(:mod:`spotify_stream.session_pool`) stays free of the native dependency:

* :func:`build_session_from_blob` builds a librespot ``Session`` NON-INTERACTIVELY
  from the reusable-credentials JSON object ``{username, credentials, type}`` the
  web-ui captured once at connect time (task 2.1 spike / task 2.2). It uses
  ``Session.Builder(conf).stored(<base64 JSON>).create()`` — no OAuth at stream
  time (R3.3). A bad/invalid blob raises, mapping to per-``sub``
  ``failed(session_create_failed)``; a non-Premium account authenticates but
  fails at track-load, mapping to ``failed(not_premium)`` (R3.5).
* :func:`load_track` reads a track from a user's session and transcodes Spotify's
  non-standard OGG Vorbis to MP3 via ffmpeg (lavaplayer's native decoder chokes
  on the raw headers), returning ``(audio_bytes, codec)``.

The librespot import is module-local (done lazily inside functions) so this
module is importable in environments without the native ``librespot`` package,
letting the pool and its factory be unit-tested with a fake session builder.

Token material (the reusable blob) is never logged.

Requirements: 3.2, 3.3, 3.5
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
from typing import Any

__all__ = [
    "NON_PREMIUM_REASON",
    "SESSION_CREATE_FAILED_REASON",
    "LibrespotSessionError",
    "NotPremiumError",
    "build_session_from_blob",
    "encode_stored_blob",
    "load_track",
]

log = logging.getLogger(__name__)

#: Per-``sub`` failure reason for a non-Premium (or otherwise stream-incapable)
#: account — detected at first track-load, not at session build (R3.5).
NON_PREMIUM_REASON = "not_premium"

#: Per-``sub`` failure reason for a missing/invalid/undecodable credential blob.
SESSION_CREATE_FAILED_REASON = "session_create_failed"

#: ffmpeg transcode timeout, seconds.
_TRANSCODE_TIMEOUT = 30

#: Read chunk size when draining a librespot audio stream.
_READ_CHUNK = 131072


class LibrespotSessionError(RuntimeError):
    """A librespot session could not be built from the stored credential (R3.5).

    Carries a non-secret reason (:data:`SESSION_CREATE_FAILED_REASON`); it never
    carries the reusable credential blob.
    """

    def __init__(self, reason: str = SESSION_CREATE_FAILED_REASON) -> None:
        super().__init__(reason)
        self.reason = reason


class NotPremiumError(RuntimeError):
    """A track could not be loaded because the account is not Premium (R3.5).

    librespot authenticates a Free account but fails at track-load; this is the
    signal to mark that user's session ``failed(not_premium)``.
    """

    def __init__(self, reason: str = NON_PREMIUM_REASON) -> None:
        super().__init__(reason)
        self.reason = reason


def encode_stored_blob(blob: dict[str, Any]) -> str:
    """Encode a reusable-credentials object as librespot's base64 JSON string.

    librespot's ``Session.Builder.stored(str)`` expects a base64-encoded JSON
    string of ``{username, credentials, type}`` (task 2.1 spike). The web-ui
    stores that exact object under ``extra.librespot_credentials``; the resolver
    flattens ``extra`` so the pool sees it as ``tokens["librespot_credentials"]``.
    Token material is never logged.
    """
    return base64.b64encode(json.dumps(blob).encode("utf-8")).decode("ascii")


def build_session_from_blob(blob: dict[str, Any], *, cache_dir: str):
    """Build a librespot ``Session`` from a stored reusable-credentials blob.

    NON-INTERACTIVE (R3.3): no OAuth, no ``client_id``/``secret`` — the blob IS
    the login material. ``cache_dir`` scopes librespot's per-user credential
    cache file so users' caches never mix (R9.3).

    Raises:
        LibrespotSessionError: The blob is missing/invalid or the session did
            not become valid (mapped to ``failed(session_create_failed)``).
    """
    import os

    from librespot.core import Session

    if not blob:
        raise LibrespotSessionError()

    os.makedirs(cache_dir, exist_ok=True)
    stored_file = os.path.join(cache_dir, "spotify-credentials.json")

    conf = Session.Configuration.Builder()
    conf.set_store_credentials(True)
    conf.set_stored_credential_file(stored_file)

    try:
        session = (
            Session.Builder(conf=conf.build())
            .stored(encode_stored_blob(blob))
            .create()
        )
    except Exception as exc:  # noqa: BLE001 - any build failure → typed reason
        log.warning(
            "spotify-stream: librespot session build failed (%s)",
            type(exc).__name__,
        )
        raise LibrespotSessionError() from exc

    if not session.is_valid():
        raise LibrespotSessionError()
    return session


def _preferred_file(files):
    """Select the preferred audio file (highest-quality OGG, then MP3, ...)."""
    from librespot.audio import Metadata

    preferred_order = [
        Metadata.AudioFile.OGG_VORBIS_320,
        Metadata.AudioFile.OGG_VORBIS_160,
        Metadata.AudioFile.OGG_VORBIS_96,
        Metadata.AudioFile.MP3_320,
        Metadata.AudioFile.MP3_256,
        Metadata.AudioFile.MP3_160,
        Metadata.AudioFile.MP3_96,
        Metadata.AudioFile.FLAC_FLAC,
        Metadata.AudioFile.AAC_48,
        Metadata.AudioFile.AAC_24,
    ]
    if not files:
        return None
    for fmt in preferred_order:
        for f in files:
            if f.format == fmt:
                return f
    return files[0]


class _QualityPicker:
    """librespot ``AudioQualityPicker`` selecting the highest available quality."""

    def get_file(self, files):
        """Return the preferred audio file from ``files`` (or ``None``)."""
        return _preferred_file(files)


def load_track(track_id_str: str, session):
    """Load + transcode a track from a user's session. Returns ``(bytes, codec)``.

    Reads the whole track into memory and transcodes Spotify's non-standard OGG
    Vorbis to MP3 via ffmpeg. A track-load failure on a non-Premium account is
    surfaced as :class:`NotPremiumError` so the pool records
    ``failed(not_premium)`` for that user (R3.5).

    Raises:
        NotPremiumError: The account cannot load the track (non-Premium).
        LibrespotSessionError: The track loaded but transcoding failed.
    """
    from librespot.audio import SuperAudioFormat
    from librespot.metadata import TrackId

    track_id = TrackId.from_base62(track_id_str)
    content_feeder = session.content_feeder()
    try:
        loaded = content_feeder.load_track(track_id, _QualityPicker(), False, None)
    except Exception as exc:  # noqa: BLE001 - Free accounts fail at track-load
        log.info(
            "spotify-stream: track %s load failed (%s) — treating as not_premium",
            track_id_str, type(exc).__name__,
        )
        raise NotPremiumError() from exc

    stream_impl = loaded.input_stream.stream()
    total_size = stream_impl.size()

    data = bytearray()
    while len(data) < total_size:
        chunk = stream_impl.read(min(_READ_CHUNK, total_size - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    raw_audio = bytes(data)

    audio_data = _transcode_to_mp3(raw_audio, track_id_str)
    if audio_data is None:
        raise LibrespotSessionError("transcode_failed")
    return audio_data, SuperAudioFormat.MP3


def _transcode_to_mp3(raw_audio: bytes, track_id_str: str) -> bytes | None:
    """Transcode raw OGG Vorbis to MP3 via ffmpeg; return ``None`` on failure."""
    try:
        proc = subprocess.run(  # noqa: S603,S607 - fixed ffmpeg argv, no shell
            ["ffmpeg", "-i", "pipe:0", "-f", "mp3", "-ab", "320k",
             "-v", "quiet", "pipe:1"],
            input=raw_audio,
            capture_output=True,
            timeout=_TRANSCODE_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - transcode subprocess error
        log.error("spotify-stream: ffmpeg transcode error for %s: %s",
                  track_id_str, type(exc).__name__)
        return None
    if proc.returncode == 0 and proc.stdout:
        log.info("spotify-stream: transcoded track %s: %d -> %d bytes (ogg->mp3)",
                 track_id_str, len(raw_audio), len(proc.stdout))
        return proc.stdout
    log.error("spotify-stream: ffmpeg transcode failed for %s (rc=%d)",
              track_id_str, proc.returncode)
    return None
