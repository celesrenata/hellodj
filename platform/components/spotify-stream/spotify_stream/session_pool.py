"""Per-user librespot session pool + guild→owner routing (Spotify data plane).

This module realizes the multi-tenant Spotify data plane (multi-tenant-source-
streaming task 2.3). It replaces the single global ``_session`` with a
:data:`SpotifySessionPool` — a
:class:`hellodj_platform_logic.session_registry.SessionRegistry` keyed by the
owning user's Cognito ``sub`` — so concurrent requests from different guilds are
served from different users' Spotify accounts, isolated from one another with NO
shared-account fallback (R3.1, R3.2, R3.6, R6.1, R10.5).

Session factory (R3.3)
----------------------

A miss builds a librespot ``Session`` NON-INTERACTIVELY from the resolved
credential's ``librespot_credentials`` reusable blob (captured once by the web-ui
connect flow, task 2.2) via :func:`spotify_stream.librespot_session.build_session_from_blob`
— no OAuth at stream time. A missing/invalid blob raises
:class:`~hellodj_platform_logic.session_registry.SessionCreateError` with reason
``session_create_failed``; a non-Premium account authenticates but fails at
first track-load, surfaced as ``not_premium`` — both are SPECIFIC per-``sub``
failure states, scoped to that user, never affecting another (R3.5, R3.7, R7.2,
R7.4).

Request flow (R1.1, R3.2)
-------------------------

A stream/preload request carries the ``guild_id`` in its path (Lavalink builds
the URL). :class:`SpotifyStreamRouter` resolves the guild's owning ``sub``
server-side (the ``sub`` is never in a URL or log), then
:meth:`SessionRegistry.get_or_create` returns that user's live
:class:`SpotifyUserSession`, building it on a miss. A guild with no recorded
owner, no Spotify credential, a ``refresh_status=failed`` credential, or no
captured librespot blob raises :class:`SpotifyCredentialUnavailableError` with a
non-secret reason — an observable failure with no cross-user fallback (R3.6,
R10.5).

Per-user audio cache (R6.2, R8.3)
---------------------------------

Loaded/transcoded track audio is cached by ``(sub, track_id)`` in a bounded,
TTL'd :class:`PerUserTrackCache`, so cached audio can NEVER be served across
users (R6.2) and the cache stays bounded (R8.3).

Token/credential material is never logged (R6.4).

Requirements: 3.1, 3.2, 3.3, 3.5, 3.6, 3.7, 6.1, 6.2, 7.2, 7.4, 8.3, 10.5
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from hellodj_platform_logic.session_registry import (
    SessionCreateError,
    SessionRegistry,
)
from hellodj_platform_logic.user_credential_resolver import (
    CredentialUnavailable,
    OwnerLookup,
    UserCredentialResolver,
)

from .librespot_session import (
    LibrespotSessionError,
    NotPremiumError,
    build_session_from_blob,
    load_track,
)

__all__ = [
    "LIBRESPOT_CREDENTIALS_KEY",
    "PROVIDER_SPOTIFY",
    "PerUserTrackCache",
    "SpotifyCredentialUnavailableError",
    "SpotifySessionPool",
    "SpotifyStreamRouter",
    "SpotifyUserSession",
    "normalize_track_id",
]

log = logging.getLogger(__name__)

#: The source provider these sessions serve.
PROVIDER_SPOTIFY = "spotify"

#: Flattened ``tokens`` key carrying the librespot reusable-credentials object
#: (the web-ui stores it under ``TokenState.extra.librespot_credentials``; the
#: resolver flattens ``extra`` verbatim). Kept in lock-step with the web-ui
#: ``spotify_librespot_capture.LIBRESPOT_CREDENTIALS_EXTRA_KEY``.
LIBRESPOT_CREDENTIALS_KEY = "librespot_credentials"


def normalize_track_id(track_id: str) -> str:
    """Strip a ``spotify:track:`` prefix, returning the bare base62 id."""
    if track_id.startswith("spotify:track:"):
        return track_id.split(":")[-1]
    return track_id


class SpotifyCredentialUnavailableError(Exception):
    """No usable Spotify credential/session for a guild — observable (R3.6).

    Carries a non-secret reason (the shared ``CredentialUnavailable`` reason —
    ``no_owner`` / ``no_credential`` / ``refresh_failed`` / ``decrypt_failed`` —
    or a session reason ``no_librespot_credential`` / ``not_premium`` /
    ``session_create_failed``) so the request fails cleanly and attributably
    (R7.1) with NO fallback to another user's account (R3.6, R10.5). It never
    carries token material.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PerUserTrackCache:
    """Bounded, TTL'd track-audio cache keyed by ``(sub, track_id)`` (R6.2, R8.3).

    Keying by the owning ``sub`` (not just the track id) guarantees a cache hit
    can never cross users (R6.2); the LRU cap + TTL keep it bounded (R8.3).
    Thread-safe: the aiohttp handlers load tracks in an executor thread.
    """

    def __init__(
        self,
        *,
        max_entries: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max(1, int(max_entries))
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        # (sub, track_id) -> (audio_bytes, codec, stored_at)
        self._entries: collections.OrderedDict[tuple[str, str], tuple[bytes, Any, float]] = (
            collections.OrderedDict()
        )

    def get(self, sub: str, track_id: str) -> tuple[bytes, Any] | None:
        """Return cached ``(audio, codec)`` for ``(sub, track_id)`` or ``None``."""
        key = (sub, track_id)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            audio, codec, stored_at = entry
            if self._clock() - stored_at > self._ttl:
                del self._entries[key]
                return None
            self._entries.move_to_end(key, last=True)
            return audio, codec

    def put(self, sub: str, track_id: str, audio: bytes, codec: Any) -> None:
        """Cache ``audio`` for ``(sub, track_id)``, evicting LRU over capacity."""
        key = (sub, track_id)
        with self._lock:
            self._entries[key] = (audio, codec, self._clock())
            self._entries.move_to_end(key, last=True)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def evict_user(self, sub: str) -> None:
        """Drop every cached entry for ``sub`` (on session close — R6.3)."""
        with self._lock:
            for key in [k for k in self._entries if k[0] == sub]:
                del self._entries[key]


class SpotifyUserSession:
    """One owning user's live librespot session (the registry session value).

    Wraps a librespot ``Session`` bound to that user's stored credential. This is
    the ``S`` value the :data:`SpotifySessionPool` holds per ``sub``; its
    :meth:`close` is invoked by the registry on eviction / idle-sweep / shutdown
    (R8). A non-Premium account is only detectable at first track-load (via the
    router's injected track loader), so a load failure raises
    :class:`~spotify_stream.librespot_session.NotPremiumError` which the router
    maps to a per-``sub`` ``failed(not_premium)`` state (R3.5).
    """

    def __init__(self, sub: str, session: Any) -> None:
        self._sub = sub
        self._session = session

    @property
    def sub(self) -> str:
        """The owning user's Cognito ``sub``."""
        return self._sub

    @property
    def session(self) -> Any:
        """The underlying librespot ``Session``."""
        return self._session

    def close(self) -> None:
        """Close the underlying librespot session (registry closer hook, R8).

        Best-effort: a failed close never strands the registry (R8.4). No token
        material is touched or logged.
        """
        close = getattr(self._session, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass


#: The per-user Spotify session pool: a shared :class:`SessionRegistry` keyed by
#: the owning user's Cognito ``sub`` holding one :class:`SpotifyUserSession` each
#: (R3.1, R6.1, R8). This is the design's ``SpotifySessionPool =
#: SessionRegistry[str, librespot.Session]`` (wrapped so eviction closes cleanly).
SpotifySessionPool = SessionRegistry[str, SpotifyUserSession]


class SpotifyStreamRouter:
    """Route a guild request to its owning user's librespot session (R3.2).

    Ties together the guild→owner :class:`OwnerLookup`, the per-user
    :class:`UserCredentialResolver`, the per-``sub`` :data:`SpotifySessionPool`,
    and a :class:`PerUserTrackCache`. For a request carrying ``guild_id`` it
    resolves the owning ``sub`` server-side (never exposed in a URL or log; R3.2)
    and returns that ``sub``'s live :class:`SpotifyUserSession`, building it on a
    miss via a factory that resolves + validates the user's stored credential.

    The ``sub`` keying guarantees no cross-user leakage (R6.1): two guilds owned
    by DISTINCT users get distinct sessions; two guilds owned by the SAME user
    share that user's one session (correct). There is NO ambient/default session
    any guild falls back to (R3.6, R10.5).
    """

    def __init__(
        self,
        owners: OwnerLookup,
        resolver: UserCredentialResolver,
        registry: SpotifySessionPool,
        cache: PerUserTrackCache,
        *,
        session_builder: Callable[[dict[str, Any], str], Any] | None = None,
        track_loader: Callable[[str, Any], tuple[bytes, Any]] | None = None,
        cache_dir_for: Callable[[str], str],
        provider: str = PROVIDER_SPOTIFY,
    ) -> None:
        self._owners = owners
        self._resolver = resolver
        self._registry = registry
        self._cache = cache
        self._provider = provider
        self._cache_dir_for = cache_dir_for
        # Injectable for tests; default to the real librespot builder + loader.
        self._session_builder = session_builder or build_session_from_blob
        self._track_loader = track_loader or load_track

    @property
    def registry(self) -> SpotifySessionPool:
        """The per-``sub`` session registry (for health + shutdown)."""
        return self._registry

    @property
    def cache(self) -> PerUserTrackCache:
        """The per-user track audio cache."""
        return self._cache

    def owner_sub_for(self, guild_id: str | int) -> str:
        """Resolve the guild's owning ``sub`` server-side, or fail observably.

        Raises:
            SpotifyCredentialUnavailableError: ``no_owner`` when the guild has no
                recorded owner — no cross-user fallback (R3.6, R10.5).
        """
        gid = str(guild_id)
        try:
            owner_sub = self._owners.owner_of(gid)
        except Exception as exc:  # noqa: BLE001 - unavailable → observable no_owner
            log.info("spotify-stream: owner lookup failed for guild %s (%s)", gid, exc)
            raise SpotifyCredentialUnavailableError("no_owner") from exc
        if not owner_sub:
            log.info("spotify-stream: guild %s has no recorded owner", gid)
            raise SpotifyCredentialUnavailableError("no_owner")
        return owner_sub

    def session_for_guild(self, guild_id: str | int) -> SpotifyUserSession:
        """Return the owning user's live librespot session for ``guild_id`` (R3.2).

        Raises:
            SpotifyCredentialUnavailableError: When the guild has no owner, no
                Spotify credential, no captured librespot blob, a failed/
                undecryptable credential, or a non-Premium account — an
                observable failure with no cross-user fallback (R3.5, R3.6,
                R10.5).
        """
        owner_sub = self.owner_sub_for(guild_id)
        gid = str(guild_id)

        def _factory(sub: str) -> SpotifyUserSession:
            """Build the owning user's librespot session (non-interactive, R3.3).

            Resolves + decrypts the credential, extracts the reusable librespot
            blob, and builds the session. A missing/failed credential or an
            invalid blob becomes a SPECIFIC per-``sub`` failure state on the
            registry (R7.2/R7.4) rather than a green-but-broken session.
            """
            result = self._resolver.resolve(gid, self._provider)
            if isinstance(result, CredentialUnavailable):
                raise SessionCreateError(result.reason)
            blob = result.get(LIBRESPOT_CREDENTIALS_KEY)
            if not isinstance(blob, dict) or not blob:
                # A Spotify credential exists but the one-time librespot capture
                # (task 2.2) never ran / was cleared — not streamable (R3.3).
                raise SessionCreateError("no_librespot_credential")
            try:
                session = self._session_builder(blob, self._cache_dir_for(sub))
            except LibrespotSessionError as exc:
                raise SessionCreateError(exc.reason) from exc
            return SpotifyUserSession(sub, session)

        try:
            return self._registry.get_or_create(owner_sub, _factory)
        except SessionCreateError as exc:
            raise SpotifyCredentialUnavailableError(exc.reason) from exc

    def load_track_for_guild(
        self, guild_id: str | int, track_id: str
    ) -> tuple[bytes, Any]:
        """Load a track for a guild's owner, using the per-``(sub,track)`` cache.

        Serves from the per-user cache when warm; otherwise loads from the user's
        session and caches by ``(sub, track_id)`` (R6.2, R8.3). A non-Premium
        account failing at track-load is recorded as a per-``sub``
        ``failed(not_premium)`` state (R3.5) and surfaced observably.

        Raises:
            SpotifyCredentialUnavailableError: With a non-secret reason.
        """
        owner_sub = self.owner_sub_for(guild_id)
        track = normalize_track_id(track_id)

        cached = self._cache.get(owner_sub, track)
        if cached is not None:
            return cached

        session = self.session_for_guild(guild_id)
        try:
            audio, codec = self._track_loader(track, session.session)
        except NotPremiumError as exc:
            # Detected only at track-load: mark this user's session failed and
            # surface observably (R3.5), isolated to this sub (R3.7).
            self._registry.close(owner_sub)
            self._cache.evict_user(owner_sub)
            self._record_failure(owner_sub, exc.reason)
            raise SpotifyCredentialUnavailableError(exc.reason) from exc
        except LibrespotSessionError as exc:
            raise SpotifyCredentialUnavailableError(exc.reason) from exc
        self._cache.put(owner_sub, track, audio, codec)
        return audio, codec

    def _record_failure(self, sub: str, reason: str) -> None:
        """Record a SPECIFIC per-``sub`` failure state on the registry (R7.2).

        Uses the registry's own failure recording via a raising factory so the
        health surface reports ``failed(reason)`` for this user rather than
        silently omitting them (no fake-green — R7.5).
        """
        def _fail(_sub: str):
            raise SessionCreateError(reason)

        try:
            self._registry.get_or_create(sub, _fail)
        except SessionCreateError:
            pass
