"""HelloDJ — Spotify Web API client for album search.

Uses the client_credentials OAuth2 flow (no user login required).
Provides a single function ``search_albums()`` that calls Spotify's
Search endpoint with ``type=album`` — one API call, returns up to 10
album results with accurate ``total_tracks`` values.

This avoids the inaccurate track-count grouping from spsearch: (which
returns individual tracks and guesses the album size from the sample).
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from config import cfg

log = logging.getLogger(__name__)

__all__ = ["search_albums"]

_AUTH_URL = "https://accounts.spotify.com/api/token"
_API_BASE = "https://api.spotify.com/v1"
_TIMEOUT = aiohttp.ClientTimeout(total=12)

# Module-level token cache (single-process bot)
_access_token: str | None = None
_expires_at: float = 0.0
_token_lock = asyncio.Lock()


async def _ensure_token() -> str:
    """Return a valid client_credentials access token, refreshing if needed."""
    global _access_token, _expires_at

    if _access_token and time.monotonic() < _expires_at:
        return _access_token

    async with _token_lock:
        # Double-check after lock
        if _access_token and time.monotonic() < _expires_at:
            return _access_token

        client_id = cfg("spotify.client_id", "")
        client_secret = cfg("spotify.client_secret", "")
        if not client_id or not client_secret:
            log.debug("spotify: search_albums — no credentials configured")
            raise RuntimeError("Spotify credentials not configured")

        data = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }

        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(
                _AUTH_URL,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning("spotify: token request failed status=%s body=%s", resp.status, body[:200])
                    raise RuntimeError(f"Spotify auth failed (HTTP {resp.status})")
                payload = await resp.json()
                token = payload.get("access_token", "")
                expires_in = int(payload.get("expires_in", 3600))
                if not token:
                    raise RuntimeError("Spotify auth response missing access_token")
                _access_token = token
                _expires_at = time.monotonic() + max(expires_in - 60, 0)
                log.info("spotify: acquired access token (expires_in=%ss)", expires_in)
                return token


async def search_albums(query: str, *, limit: int = 10, market: str = "US") -> list[dict]:
    """Search Spotify for albums matching *query*.

    Uses GET /search?type=album — a single API call that returns
    SimplifiedAlbumObjects with accurate ``total_tracks``.

    Returns a list of dicts compatible with AlbumSelectView:
        {name, artist, url, track_count, total_duration, year, source}

    total_duration is 0 (Spotify search doesn't return per-album duration —
    it would require loading each album's tracks which is expensive).

    Returns an empty list on failure (graceful degradation).
    """
    try:
        token = await _ensure_token()
    except RuntimeError as exc:
        log.debug("spotify: search_albums skipped — %s", exc)
        return []

    from urllib.parse import quote

    params = f"q={quote(query)}&type=album&limit={min(limit, 10)}&market={market}"
    url = f"{_API_BASE}/search?{params}"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 401:
                    # Token expired — invalidate and retry once
                    global _access_token, _expires_at
                    _access_token = None
                    _expires_at = 0.0
                    try:
                        token = await _ensure_token()
                    except RuntimeError:
                        return []
                    headers = {"Authorization": f"Bearer {token}"}
                    async with session.get(url, headers=headers) as retry_resp:
                        if retry_resp.status != 200:
                            log.warning("spotify: album search retry failed status=%s", retry_resp.status)
                            return []
                        data = await retry_resp.json()
                elif resp.status != 200:
                    body = await resp.text()
                    log.warning("spotify: album search failed status=%s body=%s", resp.status, body[:200])
                    return []
                else:
                    data = await resp.json()
    except Exception as exc:
        log.warning("spotify: album search failed: %s", exc)
        return []

    albums_data = data.get("albums", {})
    items = albums_data.get("items") or []

    results = []
    for album in items:
        name = album.get("name", "")
        if not name:
            continue

        # Artist names
        artists = album.get("artists") or []
        artist = ", ".join(a.get("name", "") for a in artists if a.get("name"))

        # Track count (accurate from Spotify API)
        track_count = album.get("total_tracks") or 0

        # Release year
        release_date = album.get("release_date", "")
        year = release_date[:4] if release_date else ""

        # Album URL (external Spotify URL)
        external_urls = album.get("external_urls") or {}
        album_url = external_urls.get("spotify", "")

        # Spotify URI (used by Lavalink/LavasRC for loading)
        uri = album.get("uri", "")

        results.append({
            "name": name,
            "artist": artist,
            "url": album_url or uri,
            "track_count": track_count,
            "total_duration": 0,  # Not available from search — loaded on pick
            "year": year,
            "source": "spotify",
            "album_type": album.get("album_type", ""),
        })

    log.info("spotify: album search %r returned %d album(s)", query, len(results))
    return results
