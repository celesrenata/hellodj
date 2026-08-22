"""HelloDJ — Tidal API client for music-video streaming.

This module provides a lightweight, async Tidal API client built directly on
``aiohttp``. It implements the OAuth 2.0 **client-credentials** flow (the only
flow that works for a server-side bot — Tidal does not expose per-user OAuth to
bots) and the small set of endpoints needed to:

* search for tracks, and
* fetch the video stream URL for a track that has an official music video.

Why a custom client instead of the third-party ``tidalapi`` package?
-------------------------------------------------------------------
``tidalapi`` pulls in ``requests`` (a blocking HTTP client) and heavier
dependency trees. This bot already runs on ``aiohttp`` everywhere, so a
purpose-built async client keeps the dependency surface small and matches the
codebase's async style. The endpoints below mirror the public Tidal Web API
reference (https://tidal-music.github.io/tidal-api-reference/) and the
``tidalapi`` library's own calls.

DISCORD API LIMITATION (read this before extending)
---------------------------------------------------
Discord does **NOT** support a bot "screensharing" video into a voice channel.
The Discord API offers no endpoint for a bot to broadcast video into voice —
streaming/GoLive is a user-guild feature, and bots can only set
self_mute/self_deafen voice state. The realistic way to "stream music videos to
the channel" is therefore to embed the video in a **text channel** (Discord
auto-embeds video links/attachments posted as messages), which is what the
``cogs/stream.py`` command does. This module only fetches the Tidal video URL;
the actual delivery-to-Discord is handled by the cog.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import aiohttp

from debug import get_debug_logger

log = logging.getLogger(__name__)
dbg = get_debug_logger("tidal")

# ── Tidal API endpoints (verified against the Tidal Web API reference) ─────
AUTH_URL = "https://auth.tidal.com/v1/oauth2/token"
API_BASE = "https://api.tidal.com/v1"
SEARCH_URL = f"{API_BASE}/search"
TRACK_URL = f"{API_BASE}/tracks/{{track_id}}"
TRACK_VIDEO_URL = f"{API_BASE}/tracks/{{track_id}}/video"
VIDEO_STREAM_URL = f"{API_BASE}/videos/{{video_id}}/stream"

# OAuth client-credentials grant. Tidal requires the client_id/client_secret to
# be sent as Basic auth (base64(client_id:client_secret)) with grant_type
# client_credentials in the form body.
AUTH_GRANT = "client_credentials"


class TidalError(Exception):
    """Raised when a Tidal API call fails in a non-recoverable way."""


class TidalUnconfigured(TidalError):
    """Raised when Tidal credentials are missing from the environment."""


class TidalClient:
    """Async Tidal API client (OAuth client-credentials).

    The token is fetched lazily and cached until it is within a small safety
    margin of expiry, then refreshed on the next call. All public methods are
    coroutines and safe to call from the bot's asyncio loop.
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        *,
        token_ttl_buffer: int = 60,
    ) -> None:
        # Primary names are TD_CLIENT_ID / TD_CLIENT_SECRET (task spec). The
        # deployed bot also exposes TIDAL_CLIENT_ID / TIDAL_CLIENT_SECRET (used
        # by Lavalink + web-ui), so accept those as aliases too.
        from config import cfg
        self.client_id = client_id or cfg("tidal.td_client_id", "") or cfg("tidal.client_id", "")
        self.client_secret = client_secret or cfg("tidal.td_client_secret", "") or cfg("tidal.client_secret", "")
        self.token_ttl_buffer = token_ttl_buffer
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._session: aiohttp.ClientSession | None = None
        self._token_lock = asyncio.Lock()

    # ── lifecycle ──────────────────────────────────────────

    async def close(self) -> None:
        """Close the internal aiohttp session if one was created."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _session_or_new(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    # ── OAuth token ────────────────────────────────────────

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def _ensure_token(self) -> str:
        """Return a valid access token, fetching/refreshing as needed."""
        # Fast path: a cached token that still has a safety margin of life.
        if self._access_token and time.monotonic() < self._expires_at:
            return self._access_token

        async with self._token_lock:
            # Double-checked after acquiring the lock (another coroutine may
            # have refreshed while we waited).
            if self._access_token and time.monotonic() < self._expires_at:
                return self._access_token
            token, expires_in = await self._fetch_token()
            self._access_token = token
            self._expires_at = time.monotonic() + max(expires_in - self.token_ttl_buffer, 0)
            log.info("tidal: acquired access token (expires_in=%ss)", expires_in)
            return token

    async def _fetch_token(self) -> tuple[str, int]:
        """POST the client-credentials grant and return (token, expires_in)."""
        if not self.configured:
            raise TidalUnconfigured(
                "Tidal is not configured. Set TD_CLIENT_ID and TD_CLIENT_SECRET "
                "in the environment (see bot/.env.example)."
            )

        import base64

        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode("utf-8")
        ).decode("ascii")
        data = {"grant_type": AUTH_GRANT, "client_id": self.client_id}
        headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        session = self._session_or_new()
        async with session.post(
            AUTH_URL,
            data=data,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                log.error("tidal: token request failed status=%s body=%r", resp.status, body)
                raise TidalError(
                    f"Tidal token request failed (HTTP {resp.status}). Check TD_CLIENT_ID/"
                    f"TD_CLIENT_SECRET — they must be a valid Tidal client app pair."
                )
            payload = await resp.json()
            token = payload.get("access_token")
            if not token:
                raise TidalError("Tidal token response had no access_token.")
            expires_in = int(payload.get("expires_in", 3600) or 3600)
            return token, expires_in

    def _headers(self) -> dict[str, str]:
        """Return the Bearer authorization header for API calls."""
        return {"Authorization": f"Bearer {self._access_token}"}

    async def _get(self, url: str, params: dict | None = None) -> dict:
        """Authenticated GET against the Tidal API with one token-retry.

        On a 401 (expired/revoked token) we clear the cache and retry once with
        a freshly fetched token, so a token that died mid-session self-heals.
        """
        token = await self._ensure_token()
        session = self._session_or_new()
        headers = self._headers()

        for attempt in (1, 2):
            async with session.get(
                url,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 401 and attempt == 1:
                    log.info("tidal: got 401 on %s — refreshing token and retrying", url)
                    self._access_token = None
                    self._expires_at = 0.0
                    token = await self._ensure_token()
                    headers = self._headers()
                    continue
                if resp.status == 404:
                    # Endpoint returned no resource (e.g. track has no video).
                    raise TidalError(f"Tidal resource not found: {url}")
                if resp.status != 200:
                    body = await resp.text()
                    log.error("tidal: GET %s failed status=%s body=%r", url, resp.status, body)
                    raise TidalError(f"Tidal API request failed (HTTP {resp.status}).")
                try:
                    return await resp.json()
                except Exception as exc:
                    raise TidalError(f"Tidal response was not valid JSON: {exc}") from exc

        raise TidalError(f"Tidal API request failed twice with 401: {url}")

    # ── search ─────────────────────────────────────────────

    async def search(self, query: str, limit: int = 10) -> list[dict]:
        """Search Tidal for tracks matching ``query``.

        Returns a list of normalized track dicts:
        ``{id, title, artist, album, duration, url, thumbnail}``.
        Raises ``TidalError`` on API failure; never returns a partial list.
        """
        dbg.event("search_start", query=query, limit=limit)
        t0 = time.monotonic()
        params = {
            "query": query,
            "types": "TRACKS",
            "limit": str(max(1, min(limit, 50))),
            "countryCode": "US",
        }
        payload = await self._get(SEARCH_URL, params=params)
        items = payload.get("tracks", {}).get("items", []) if isinstance(
            payload.get("tracks"), dict
        ) else []
        results = []
        for item in items:
            norm = self._normalize_track(item)
            if norm:
                results.append(norm)
        dbg.event("search_complete", query=query, results=len(results),
                  elapsed_ms=(time.monotonic() - t0) * 1000)
        log.info("tidal: search %r returned %d track(s)", query, len(results))
        return results

    async def get_track(self, track_id: str | int) -> dict | None:
        """Fetch full track metadata by Tidal track id."""
        payload = await self._get(TRACK_URL.format(track_id=track_id))
        return self._normalize_track(payload)

    # ── video lookup ───────────────────────────────────────

    async def get_video_url(self, track_id: str | int) -> str | None:
        """Return the Tidal video stream URL for a track, or None if unavailable.

        Not every track has an official music video. Tidal returns a 404 when a
        track has no video resource; that is treated as "no video" (None), not
        as a hard error, so the cog can fall back to audio playback + a YouTube
        link.
        """
        dbg.event("video_lookup_start", track_id=track_id)
        t0 = time.monotonic()
        try:
            video = await self._get(TRACK_VIDEO_URL.format(track_id=track_id))
        except TidalError as exc:
            if "not found" in str(exc).lower() or "404" in str(exc):
                dbg.info("track %s has no video resource (404)", track_id)
                log.info("tidal: track %s has no video resource — no video available", track_id)
                return None
            log.warning("tidal: video lookup for track %s failed: %s", track_id, exc)
            raise

        video_id = video.get("id")
        if not video_id:
            dbg.warning("track %s video response had no id, payload=%r", track_id, video)
            log.warning("tidal: track %s video response had no id", track_id)
            return None

        try:
            stream = await self._get(VIDEO_STREAM_URL.format(video_id=video_id))
        except TidalError as exc:
            dbg.error("video stream fetch failed video_id=%s error=%s", video_id, exc)
            log.warning("tidal: video stream for %s failed: %s", video_id, exc)
            return None

        url = stream.get("url") or stream.get("streamUrl")
        if not url:
            dbg.info("video %s has no playable url, stream_keys=%r", video_id, list(stream.keys()))
            log.info("tidal: video %s has no playable url", video_id)
            return None
        dbg.event("video_lookup_complete", track_id=track_id, video_id=video_id,
                  elapsed_ms=(time.monotonic() - t0) * 1000)
        log.info("tidal: track %s video url resolved (video_id=%s)", track_id, video_id)
        return url

    # ── normalization ──────────────────────────────────────

    @staticmethod
    def _normalize_track(item: dict) -> dict | None:
        """Map a raw Tidal track JSON object to the bot's lightweight entry shape."""
        track_id = item.get("id")
        if not track_id:
            return None
        title = item.get("title") or item.get("name") or "Unknown"
        artist = ""
        artists = item.get("artists") or []
        if isinstance(artists, list) and artists:
            artist = artists[0].get("name") if isinstance(artists[0], dict) else str(artists[0])
        album = ""
        album_obj = item.get("album")
        if isinstance(album_obj, dict):
            album = album_obj.get("name") or ""

        duration_ms = 0
        if item.get("duration"):
            try:
                duration_ms = int(float(item["duration"]) * 1000)
            except (TypeError, ValueError):
                duration_ms = 0

        return {
            "id": track_id,
            "title": title,
            "artist": artist,
            "album": album,
            "duration": duration_ms,
            "url": f"https://tidal.com/track/{track_id}",
            "thumbnail": (album_obj.get("imageCover") if isinstance(album_obj, dict) else None)
            or item.get("imageCover")
            or None,
        }


_SINGLETON: TidalClient | None = None


def get_client() -> TidalClient:
    """Return a module-level singleton client, creating it on first use.

    The singleton is deliberately NOT created at import time (no network work
    happens on import, and the client is cheap). Cogs call ``get_client()``
    when they need it.
    """
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = TidalClient()
    return _SINGLETON


# ── Tidal v2 album search (uses PKCE access token from credential store) ─────

V2_BASE = "https://openapi.tidal.com/v2"


async def search_albums(query: str, limit: int = 10) -> list[dict]:
    """Search Tidal for albums matching *query* using the v2 JSON:API.

    Uses the PKCE access token from the credential store (same token the
    tidal-stream sidecar and LavasRC use). Returns a list of dicts:
        {name, artist, url, track_count, duration_seconds, album_id, year}

    Returns an empty list on failure (graceful degradation).
    """
    from credentials import creds
    from urllib.parse import quote
    import re as _re

    token = creds.get("tidal.access_token", "")
    if not token:
        log.debug("tidal: search_albums — no access token, skipping")
        return []

    # Normalize query for Tidal's literal search:
    # "volume X" → "vol. X" (Tidal catalogs use abbreviation)
    normalized = _re.sub(r'\bvolume\s+(\d+)', r'vol. \1', query, flags=_re.IGNORECASE)

    encoded_query = quote(normalized)
    url = (
        f"{V2_BASE}/searchResults"
        f"?filter%5Bquery%5D={encoded_query}"
        f"&countryCode=US"
        f"&include=albums,albums.artists"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.api+json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.warning("tidal: album search failed (status=%s)", resp.status)
                    return []
                data = await resp.json()
    except Exception as exc:
        log.warning("tidal: album search failed: %s", exc)
        return []

    included = data.get("included", [])

    # Build a lookup of artist resources
    artist_map: dict[str, str] = {}
    for resource in included:
        if resource.get("type") == "artists":
            aid = resource.get("id", "")
            name = resource.get("attributes", {}).get("name", "")
            if aid and name:
                artist_map[aid] = name

    # Extract album results
    results = []
    for resource in included:
        if resource.get("type") != "albums":
            continue
        attrs = resource.get("attributes", {})
        album_id = resource.get("id", "")
        title = attrs.get("title", "")
        if not title or not album_id:
            continue

        # Resolve artist names
        rels = resource.get("relationships", {})
        artist_refs = rels.get("artists", {}).get("data", [])
        artist_names = []
        for ref in artist_refs:
            name = artist_map.get(ref.get("id", ""))
            if name:
                artist_names.append(name)
        artist = ", ".join(artist_names) if artist_names else ""

        # Parse ISO 8601 duration (PT57M19S) to seconds
        duration_str = attrs.get("duration", "")
        duration_seconds = 0
        if duration_str:
            try:
                import re
                m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration_str)
                if m:
                    h = int(m.group(1) or 0)
                    mins = int(m.group(2) or 0)
                    s = int(m.group(3) or 0)
                    duration_seconds = h * 3600 + mins * 60 + s
            except (ValueError, TypeError):
                pass

        track_count = attrs.get("numberOfItems") or 0
        release_date = attrs.get("releaseDate", "")
        year = release_date[:4] if release_date else ""

        results.append({
            "name": title,
            "artist": artist,
            "url": f"https://tidal.com/album/{album_id}",
            "track_count": track_count,
            "duration_seconds": duration_seconds,
            "total_duration": duration_seconds * 1000,  # ms for bot display
            "album_id": album_id,
            "year": year,
            "source": "tidal",
        })

    # Sort by relevance: score based on how many query words appear in the title
    # Use both original query and normalized form for matching
    query_words = set(query.lower().split()) | set(normalized.lower().split())
    # Remove very short/common words that cause false matches
    query_words = {w for w in query_words if len(w) > 1}

    def _relevance(album: dict) -> float:
        title_lower = album["name"].lower()
        title_words = set(title_lower.split())
        # Count matching words (check both directions)
        matches = sum(1 for w in query_words if w in title_lower)
        # Bonus for exact substring match of the full query
        if query.lower() in title_lower or normalized.lower() in title_lower:
            matches += len(query_words) * 2
        return -matches  # negative for descending sort

    results.sort(key=_relevance)
    results = results[:limit]

    log.info("tidal: album search %r returned %d album(s)", query, len(results))
    return results
