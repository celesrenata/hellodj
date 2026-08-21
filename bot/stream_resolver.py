"""HelloDJ — Stream resolver for direct Tidal/Spotify playback.

Calls the tidal-stream and spotify-stream sidecar services to get direct audio
URLs, bypassing the broken YouTube mirroring path in LavaSrc.

Usage in player.py:
    from stream_resolver import resolve_direct_stream
    url = await resolve_direct_stream(source_provider, track_url_or_id)
    if url:
        tracks = await Playable.search(url)  # Lavalink loads HTTP audio natively
"""

from __future__ import annotations

import logging
import os
import re
import time

import aiohttp

from config import cfg

log = logging.getLogger(__name__)

# ── Service URLs (in-cluster) ──────────────────────────────────────────────────

TIDAL_STREAM_URL = cfg("stream.tidal_url", "http://localhost:8801")
SPOTIFY_STREAM_URL = cfg("stream.spotify_url", "http://localhost:8802")

# Timeout for stream service calls (they should be fast — just API lookups)
RESOLVE_TIMEOUT = aiohttp.ClientTimeout(total=10)


# ── ID extraction ──────────────────────────────────────────────────────────────

_SPOTIFY_TRACK_RE = re.compile(
    r"(?:https?://open\.spotify\.com/track/|spotify:track:)([a-zA-Z0-9]+)"
)
_TIDAL_TRACK_RE = re.compile(
    r"(?:https?://(?:www\.)?tidal\.com/(?:browse/)?track/|tidal://track/)(\d+)"
)


def extract_spotify_id(url_or_id: str) -> str | None:
    """Extract a Spotify track ID from a URL or return the raw ID."""
    m = _SPOTIFY_TRACK_RE.search(url_or_id)
    if m:
        return m.group(1)
    # If it's just a bare base62 ID (22 chars, alphanumeric)
    if re.fullmatch(r"[a-zA-Z0-9]{22}", url_or_id):
        return url_or_id
    return None


def extract_tidal_id(url_or_id: str) -> str | None:
    """Extract a Tidal track ID from a URL or return the raw ID."""
    m = _TIDAL_TRACK_RE.search(url_or_id)
    if m:
        return m.group(1)
    # If it's just a bare numeric ID
    if re.fullmatch(r"\d+", url_or_id):
        return url_or_id
    return None


# ── Resolver ───────────────────────────────────────────────────────────────────

async def resolve_tidal_stream(track_url_or_id: str) -> str | None:
    """Resolve a Tidal track to a direct audio URL via the tidal-stream service.

    Returns the direct CDN URL on success, None on failure.
    """
    track_id = extract_tidal_id(track_url_or_id)
    if not track_id:
        log.debug("stream_resolver: could not extract Tidal track ID from %r", track_url_or_id)
        return None

    url = f"{TIDAL_STREAM_URL}/stream/{track_id}"
    t0 = time.monotonic()

    try:
        async with aiohttp.ClientSession(timeout=RESOLVE_TIMEOUT) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning(
                        "stream_resolver: tidal-stream returned %d for track %s: %s",
                        resp.status, track_id, body[:200],
                    )
                    return None
                data = await resp.json()
                stream_url = data.get("url")
                if stream_url and stream_url.startswith("http"):
                    log.info(
                        "stream_resolver: resolved Tidal track %s → direct URL "
                        "(quality=%s, codec=%s, elapsed=%.0fms)",
                        track_id, data.get("quality"), data.get("codec"),
                        (time.monotonic() - t0) * 1000,
                    )
                    return stream_url
                # HLS manifest case (hi-res)
                manifest = data.get("manifest")
                if manifest:
                    log.info(
                        "stream_resolver: Tidal track %s returned HLS manifest "
                        "(quality=%s, codec=%s)",
                        track_id, data.get("quality"), data.get("codec"),
                    )
                    # TODO: Lavalink may need the manifest served as a URL
                    # For now, fall back to None and let LavaSrc handle it
                    return None
                log.warning("stream_resolver: tidal-stream response had no url for track %s", track_id)
                return None
    except Exception as exc:
        log.warning("stream_resolver: tidal-stream call failed for track %s: %s", track_id, exc)
        return None


async def resolve_spotify_stream(track_url_or_id: str) -> str | None:
    """Resolve a Spotify track to a streaming proxy URL via the spotify-stream service.

    Returns the proxy URL immediately — the spotify-stream sidecar handles
    loading on-demand when Lavalink fetches the stream. No preload needed.
    """
    track_id = extract_spotify_id(track_url_or_id)
    if not track_id:
        log.debug("stream_resolver: could not extract Spotify track ID from %r", track_url_or_id)
        return None

    # Quick health check (should be <50ms)
    health_url = f"{SPOTIFY_STREAM_URL}/health"
    t0 = time.monotonic()

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as session:
            async with session.get(health_url) as resp:
                if resp.status != 200:
                    log.warning("stream_resolver: spotify-stream health check failed (%d)", resp.status)
                    return None
                data = await resp.json()
                if data.get("status") != "ok":
                    log.warning("stream_resolver: spotify-stream not ready: %s", data.get("status"))
                    return None
    except Exception as exc:
        log.warning("stream_resolver: spotify-stream health check failed: %s", exc)
        return None

    # Return the streaming proxy URL immediately — no preload wait
    # The spotify-stream sidecar loads on-demand when Lavalink fetches /stream
    stream_url = f"{SPOTIFY_STREAM_URL}/stream/{track_id}"
    log.info(
        "stream_resolver: resolved Spotify track %s → proxy URL (elapsed=%.0fms)",
        track_id, (time.monotonic() - t0) * 1000,
    )
    return stream_url


async def resolve_direct_stream(source_provider: str, track_url_or_id: str) -> str | None:
    """Attempt to resolve a track to a direct stream URL.

    Args:
        source_provider: "spotify" or "tidal"
        track_url_or_id: Spotify/Tidal URL or track ID

    Returns:
        Direct audio URL on success, None if the service is unavailable or
        the track can't be resolved (caller should fall back to old path).
    """
    if source_provider == "tidal":
        return await resolve_tidal_stream(track_url_or_id)
    elif source_provider == "spotify":
        return await resolve_spotify_stream(track_url_or_id)
    return None
