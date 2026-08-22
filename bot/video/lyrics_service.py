"""Per-guild lyrics orchestrator for the synced lyrics overlay system.

Manages lyrics fetch resolution, in-memory LRU cache, timing computation,
and WebSocket broadcast. Each guild gets one LyricsService instance following
the VisualizerManager per-guild pattern.

Audio Independence: This module MUST NOT propagate exceptions to the audio
pipeline. All fetches are fire-and-forget with exception swallowing at the
boundary.

Requirements: 1.1, 1.4, 1.5, 2.4, 2.5, 9.3, 9.4, 9.5
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import TYPE_CHECKING

import player
from video.beat_timing import compute_beat_timing
from video.genius_provider import GeniusProvider
from video.lrclib_provider import LRCLIBProvider
from video.lyrics_models import TimedLyrics

if TYPE_CHECKING:
    from video.ws_hub import WebSocketHub

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level registry
# ---------------------------------------------------------------------------

_instances: dict[int, LyricsService] = {}
_ws_hub_ref: WebSocketHub | None = None


def init_lyrics_services(ws_hub: WebSocketHub) -> None:
    """Set the module-level ws_hub reference for factory use.

    Called once during bot setup before any LyricsService instances are created.
    """
    global _ws_hub_ref
    _ws_hub_ref = ws_hub


def register_track_start_callback() -> None:
    """Chain LyricsService into the player track-start callback.

    Captures whatever callback is currently registered (e.g. VisualizerManager),
    creates a chained callback that:
    1. Calls the original callback first (wrapped in try/except)
    2. Then calls LyricsService.on_track_change (wrapped in try/except)

    Audio independence (Req 9.5): Neither the original callback failure nor a
    lyrics failure will propagate to the caller (player.py on_track_start).
    The original callback always runs first so visualizer updates are never
    blocked by lyrics processing.
    """
    original_callback = player._on_track_start_callback

    async def _chained_track_start(guild_id: int, metadata: dict) -> None:
        # Forward to original callback (e.g. VisualizerManager) first
        if original_callback is not None:
            try:
                await original_callback(guild_id, metadata)
            except Exception:
                log.debug(
                    "Chained track-start: original callback failed (swallowed)",
                    exc_info=True,
                )

        # Then handle lyrics — completely independent
        try:
            svc = get_lyrics_service(guild_id)
            await svc.on_track_change(guild_id, metadata)
        except Exception:
            log.debug(
                "Chained track-start: lyrics callback failed (swallowed)",
                exc_info=True,
            )

    player.set_on_track_start_callback(_chained_track_start)


def get_lyrics_service(guild_id: int) -> LyricsService:
    """Get or create a LyricsService for the given guild.

    Uses the module-level ws_hub reference set by init_lyrics_services().

    Raises:
        RuntimeError: If init_lyrics_services() has not been called.
    """
    if guild_id in _instances:
        return _instances[guild_id]

    if _ws_hub_ref is None:
        raise RuntimeError(
            "init_lyrics_services() must be called before get_lyrics_service()"
        )

    instance = LyricsService(guild_id, _ws_hub_ref)
    _instances[guild_id] = instance
    return instance


# ---------------------------------------------------------------------------
# LyricsService
# ---------------------------------------------------------------------------


class LyricsService:
    """Per-guild lyrics orchestrator.

    Manages:
    - Lyrics fetch resolution (LRCLIB synced → LRCLIB plain → Genius plain → unavailable)
    - In-memory LRU cache (max 50 entries, keyed by artist:title)
    - WebSocket broadcast of lyrics payloads
    - Overlay enabled/disabled state
    - Track change auto-fetch when overlay is enabled

    Audio independence is absolute: all exceptions are caught and logged,
    never propagated to the caller.
    """

    _CACHE_MAX = 50
    _DURATION_MAX_S = 86400  # 24 hours — skip timing for live streams

    def __init__(self, guild_id: int, ws_hub: WebSocketHub) -> None:
        self.guild_id = guild_id
        self.enabled: bool = False
        self.current_lyrics: TimedLyrics | None = None
        self.current_track_key: str = ""
        self._ws_hub = ws_hub
        self._cache: OrderedDict[str, TimedLyrics] = OrderedDict()
        self._lrclib = LRCLIBProvider()
        self._genius: GeniusProvider | None = None

    # ------------------------------------------------------------------
    # Lazy provider initialization
    # ------------------------------------------------------------------

    def _get_genius(self) -> GeniusProvider | None:
        """Lazily initialize GeniusProvider with access token from config.

        Returns None if no Genius token is configured.
        """
        if self._genius is not None:
            return self._genius

        from config import cfg

        token = cfg("genius.access_token", "") or cfg("genius.api_key", "")
        if not token:
            return None

        self._genius = GeniusProvider(token)
        return self._genius

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def on_track_change(self, guild_id: int, metadata: dict) -> None:
        """Called via player.py track_start callback.

        If overlay is enabled, auto-fetches lyrics for the new track.
        If overlay is disabled, updates metadata for when it's next enabled.

        This method NEVER raises — all exceptions are caught and logged.
        """
        try:
            artist = metadata.get("artist", "")
            title = metadata.get("title", "")
            duration_ms = metadata.get("duration_ms", 0)

            self.current_track_key = _make_cache_key(artist, title)

            if not self.enabled:
                return

            await self._fetch_and_broadcast_internal(artist, title, duration_ms)
        except Exception as exc:
            log.debug(
                "LyricsService[guild=%d]: on_track_change error (swallowed): %s",
                self.guild_id,
                exc,
            )

    async def fetch_and_broadcast(
        self,
        artist: str | None = None,
        title: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Fetch lyrics for current track and broadcast to clients.

        Uses cache if available. If artist/title/duration_ms are not provided,
        falls back to the current_track_key metadata stored from last
        on_track_change call.

        This method NEVER raises — all exceptions are caught and logged.
        """
        try:
            if artist is None or title is None or duration_ms is None:
                # Cannot proceed without metadata
                log.debug(
                    "LyricsService[guild=%d]: fetch_and_broadcast called without "
                    "complete metadata, broadcasting unavailable",
                    self.guild_id,
                )
                await self._broadcast_unavailable()
                return

            await self._fetch_and_broadcast_internal(artist, title, duration_ms)
        except Exception as exc:
            log.debug(
                "LyricsService[guild=%d]: fetch_and_broadcast error (swallowed): %s",
                self.guild_id,
                exc,
            )

    # ------------------------------------------------------------------
    # Internal fetch + broadcast logic
    # ------------------------------------------------------------------

    async def _fetch_and_broadcast_internal(
        self, artist: str, title: str, duration_ms: int
    ) -> None:
        """Core resolution chain: cache → LRCLIB → Genius → beat-estimate → unavailable.

        Combined timeout budget: 5s LRCLIB + 5s Genius = 10s max.
        All exceptions caught at boundary — never propagates.
        """
        cache_key = _make_cache_key(artist, title)
        duration_s = duration_ms / 1000.0

        # Guard: skip timing for invalid or live-stream durations
        if duration_s <= 0 or duration_s > self._DURATION_MAX_S:
            log.debug(
                "LyricsService[guild=%d]: duration guard triggered "
                "(duration_s=%.1f), broadcasting unavailable",
                self.guild_id,
                duration_s,
            )
            self.current_lyrics = None
            await self._broadcast_unavailable(cache_key)
            return

        # Check LRU cache
        if cache_key in self._cache:
            # Move to end (most recently used)
            self._cache.move_to_end(cache_key)
            lyrics = self._cache[cache_key]
            self.current_lyrics = lyrics
            await self._broadcast_lyrics(lyrics)
            return

        # Phase 1: LRCLIB resolution chain
        result = await self._lrclib.fetch(artist, title, duration_s)

        if isinstance(result, TimedLyrics):
            # LRCLIB returned synced lyrics — cache and broadcast
            self._cache_put(cache_key, result)
            self.current_lyrics = result
            await self._broadcast_lyrics(result)
            return

        if isinstance(result, str):
            # LRCLIB returned plain text — compute beat-estimated timing
            timed_lines = await compute_beat_timing(result, duration_s)
            if timed_lines:
                lyrics = TimedLyrics(
                    track_id=cache_key,
                    sync_type="beat_estimated",
                    duration_s=duration_s,
                    lines=timed_lines,
                )
                self._cache_put(cache_key, lyrics)
                self.current_lyrics = lyrics
                await self._broadcast_lyrics(lyrics)
                return

        # No results from LRCLIB (None or empty lines from plain text)
        # Phase 2: Genius fallback
        genius = self._get_genius()
        if genius is not None:
            try:
                genius_text = await genius.fetch(title, artist)
            except Exception:
                genius_text = None
                log.debug(
                    "LyricsService[guild=%d]: Genius fetch error (swallowed)",
                    self.guild_id,
                    exc_info=True,
                )

            if genius_text:
                timed_lines = await compute_beat_timing(genius_text, duration_s)
                if timed_lines:
                    lyrics = TimedLyrics(
                        track_id=cache_key,
                        sync_type="beat_estimated",
                        duration_s=duration_s,
                        lines=timed_lines,
                    )
                    self._cache_put(cache_key, lyrics)
                    self.current_lyrics = lyrics
                    await self._broadcast_lyrics(lyrics)
                    return

        # No results from any provider
        self.current_lyrics = None
        await self._broadcast_unavailable(cache_key)

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def _cache_put(self, key: str, lyrics: TimedLyrics) -> None:
        """Insert into LRU cache, evicting oldest if at capacity."""
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            if len(self._cache) >= self._CACHE_MAX:
                # Evict least recently used (first item)
                self._cache.popitem(last=False)
            self._cache[key] = lyrics

    # ------------------------------------------------------------------
    # WebSocket broadcast helpers
    # ------------------------------------------------------------------

    async def _broadcast_lyrics(self, lyrics: TimedLyrics) -> None:
        """Broadcast lyrics_data message to all guild clients."""
        message = lyrics.to_ws_message()
        await self._ws_hub.broadcast_from_bot(self.guild_id, message)

    async def _broadcast_unavailable(self, track_id: str = "") -> None:
        """Broadcast lyrics_unavailable message to all guild clients."""
        message = {
            "type": "lyrics_unavailable",
            "track_id": track_id or self.current_track_key,
            "reason": "not_found",
        }
        await self._ws_hub.broadcast_from_bot(self.guild_id, message)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cache_key(artist: str, title: str) -> str:
    """Create a normalized cache key from artist and title."""
    return f"{artist.lower().strip()}:{title.lower().strip()}"
