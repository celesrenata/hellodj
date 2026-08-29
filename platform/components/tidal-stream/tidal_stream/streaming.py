"""Direct Tidal streaming resolution.

Resolves Tidal track search results and direct stream URLs for the audio path
(R6.1). All requests are authenticated with a bearer access token obtained from
the :class:`~tidal_stream.token_manager.TidalTokenManager`, which uses the
first-party single-app-id OAuth integration (R9.1) and refreshes expired tokens
via the shared decision logic (R9.4). On a 401 the token is force-refreshed once
and the request retried, so a token that dies mid-session self-heals.

The token manager's synchronous, thread-safe ``get_access_token`` is offloaded
to a thread from the async request path so the event loop is never blocked.

Requirements: 6.1, 9.1, 9.4, 15.1
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

import aiohttp

__all__ = ["AccessTokenSource", "TidalStreamer", "TidalStreamError"]

log = logging.getLogger(__name__)

#: Per-request timeout (seconds) for Tidal API calls.
DEFAULT_TIMEOUT_SECONDS = 30.0


class AccessTokenSource(Protocol):
    """A synchronous source of a valid Tidal bearer access token.

    The streamer is agnostic to WHERE the token comes from: the legacy single
    -account :class:`~tidal_stream.token_manager.TidalTokenManager` (which
    refreshes + persists) and the multi-tenant read-only
    :class:`~tidal_stream.user_sessions.ReadOnlyTidalTokenSource` (which resolves
    the owning user's token from the unified store, read-only — R5.3) both
    satisfy this protocol. ``force=True`` requests a fresh read (a legacy
    refresh, or a read-only uncached re-resolve).
    """

    def get_access_token(self, *, force: bool = False) -> str:
        """Return a valid access token, optionally forcing a fresh read."""
        ...


class TidalStreamError(Exception):
    """Raised when a Tidal streaming/search request fails."""


class TidalStreamer:
    """Resolves Tidal search results and direct stream URLs.

    Args:
        token_manager: Any :class:`AccessTokenSource` providing valid Tidal
            access tokens (the legacy single-account token manager or the
            multi-tenant read-only per-user token source — R5.3).
        api_base: Tidal API base URL.
        country_code: ISO country code for catalog/stream resolution.
        session: Optional injected aiohttp session (a new one is created and
            owned when omitted).
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        token_manager: AccessTokenSource,
        *,
        api_base: str,
        country_code: str = "US",
        session: aiohttp.ClientSession | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._tokens = token_manager
        self._api_base = api_base.rstrip("/")
        self._country_code = country_code
        self._session = session
        self._owns_session = session is None
        self._timeout = timeout

    async def close(self) -> None:
        """Close the internal aiohttp session if this streamer owns it."""
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    def _session_or_new(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _access_token(self, *, force: bool = False) -> str:
        """Fetch a valid access token without blocking the event loop."""
        return await asyncio.to_thread(self._tokens.get_access_token, force=force)

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """Authenticated GET against the Tidal API with a single 401 retry."""
        url = f"{self._api_base}/{path.lstrip('/')}"
        session = self._session_or_new()
        timeout = aiohttp.ClientTimeout(total=self._timeout)

        for attempt in (1, 2):
            token = await self._access_token(force=attempt == 2)
            headers = {"Authorization": f"Bearer {token}"}
            async with session.get(
                url, params=params, headers=headers, timeout=timeout
            ) as response:
                if response.status == 401 and attempt == 1:
                    log.info("tidal-stream: 401 on %s, forcing refresh and retrying", url)
                    continue
                if response.status == 404:
                    raise TidalStreamError(f"Tidal resource not found: {url}")
                if response.status != 200:
                    body = await response.text()
                    log.error(
                        "tidal-stream: GET %s failed status=%s body=%r",
                        url,
                        response.status,
                        body,
                    )
                    raise TidalStreamError(
                        f"Tidal API request failed (HTTP {response.status})"
                    )
                try:
                    return await response.json()
                except Exception as error:  # noqa: BLE001 - normalize to TidalStreamError
                    raise TidalStreamError(
                        f"Tidal response was not valid JSON: {error}"
                    ) from error

        raise TidalStreamError(f"Tidal API request failed twice with 401: {url}")

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search Tidal for tracks matching ``query`` (R6.1)."""
        if not query:
            raise ValueError("query is required")
        params = {
            "query": query,
            "types": "TRACKS",
            "limit": str(max(1, min(limit, 50))),
            "countryCode": self._country_code,
        }
        payload = await self._get("search", params)
        tracks = payload.get("tracks")
        items = tracks.get("items", []) if isinstance(tracks, dict) else []
        results: list[dict[str, Any]] = []
        for item in items:
            normalized = _normalize_track(item)
            if normalized:
                results.append(normalized)
        return results

    async def get_stream_url(self, track_id: str) -> str:
        """Resolve the direct audio stream URL for a Tidal track (R6.1)."""
        if not track_id:
            raise ValueError("track_id is required")
        payload = await self._get(
            f"tracks/{track_id}/urlpostpaywall",
            {
                "countryCode": self._country_code,
                "audioquality": "LOSSLESS",
                "assetpresentation": "FULL",
                "urlusagemode": "STREAM",
            },
        )
        urls = payload.get("urls")
        if isinstance(urls, list) and urls:
            return str(urls[0])
        single = payload.get("url") or payload.get("streamUrl")
        if single:
            return str(single)
        raise TidalStreamError(f"Tidal track {track_id} has no playable stream URL")


def _normalize_track(item: dict[str, Any]) -> dict[str, Any] | None:
    """Map a raw Tidal track JSON object to the sidecar's entry shape."""
    track_id = item.get("id")
    if not track_id:
        return None
    title = item.get("title") or item.get("name") or "Unknown"
    artist = ""
    artists = item.get("artists") or []
    if isinstance(artists, list) and artists:
        first = artists[0]
        artist = first.get("name") if isinstance(first, dict) else str(first)
    album_obj = item.get("album")
    album = album_obj.get("name") if isinstance(album_obj, dict) else ""

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
        "album": album or "",
        "duration": duration_ms,
        "url": f"https://tidal.com/track/{track_id}",
    }
