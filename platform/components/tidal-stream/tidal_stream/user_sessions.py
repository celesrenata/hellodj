"""Per-user Tidal streaming sessions (read-only, unified-store backed).

This module realizes the multi-tenant Tidal data plane (multi-tenant-source-
streaming, task 3.1). It replaces the single startup-bound ``refresh_secret_id``
account with a :data:`TidalSessionRegistry` — a
:class:`hellodj_platform_logic.session_registry.SessionRegistry` keyed by the
owning user's Cognito ``sub`` — so concurrent requests from different guilds are
served with different users' Tidal tokens, isolated from one another and with no
cross-user fallback (R5.1, R5.2, R5.4, R6.1, R10.5).

Read-only contract (R5.3, R2.1)
-------------------------------

The sidecar is **read-only** on token material. Each per-user session gets its
access token from a :class:`ReadOnlyTidalTokenSource` that resolves the owning
user's ``SOURCECRED#tidal`` item from the unified store via the shared
:class:`~hellodj_platform_logic.user_credential_resolver.UserCredentialResolver`
(guild → owner ``sub`` → decrypt), which itself handles the read-only expiry
re-read (R2.2) and the ``refresh_status=failed`` gate (R2.3). The sidecar NEVER
refreshes, re-encrypts, or writes the credential — the durable watchdog owns
Tidal's single-app-id refresh, unchanged (R5.3). The ``app_id`` / ``callback_url``
stay global config; only the per-user token varies.

Request flow (R1.1, R5.1)
-------------------------

A stream/search request carries the ``guild_id`` in its path (mirroring the
Spotify sidecar). :class:`TidalStreamRouter.client_for_guild` resolves the
guild's owning ``sub`` server-side (so the ``sub`` is never in a URL or log),
then :meth:`TidalSessionRegistry.get_or_create` returns that user's live
:class:`TidalUserClient`, building it on a miss. A guild with no recorded owner
or no Tidal credential raises :class:`TidalCredentialUnavailableError` with a
non-secret reason — an observable failure with no cross-user fallback (R5.4,
R10.5).

Isolation + bounds (R6, R8)
---------------------------

The registry is keyed by ``sub``, so a request for one user can never return
another user's client, and the shared registry enforces the bounded-LRU / idle
-eviction / clean-shutdown guarantees (R8.1, R8.2, R8.4) and the honest per-``sub``
failure state (R7.2, R7.4). Token values are never logged (R6.4).

Requirements: 5.1, 5.2, 5.3, 5.4, 10.5, 6.1, 7.2, 7.4
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from hellodj_platform_logic.session_registry import (
    SessionCreateError,
    SessionRegistry,
)
from hellodj_platform_logic.user_credential_resolver import (
    CredentialUnavailable,
    OwnerLookup,
    UserCredentialResolver,
)

from .streaming import TidalStreamer

__all__ = [
    "PROVIDER_TIDAL",
    "ReadOnlyTidalTokenSource",
    "TidalCredentialUnavailableError",
    "TidalSessionRegistry",
    "TidalStreamRouter",
    "TidalUserClient",
]

log = logging.getLogger(__name__)

#: The source provider these sessions serve.
PROVIDER_TIDAL = "tidal"


class TidalCredentialUnavailableError(Exception):
    """No usable Tidal credential for a guild — an observable failure (R5.4).

    Carries the non-secret ``CredentialUnavailable`` reason (``no_owner`` /
    ``no_credential`` / ``refresh_failed`` / ``decrypt_failed``) from the shared
    resolver so the request fails cleanly and attributably (R7.1) with NO
    fallback to another user's token (R5.4, R10.5). It never carries token
    material.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ReadOnlyTidalTokenSource:
    """Read-only per-guild Tidal access-token supplier over the unified store.

    Bound to one ``guild_id``, it resolves the owning user's Tidal credential
    through the shared :class:`UserCredentialResolver` and returns the current
    access token. It exposes the SAME synchronous ``get_access_token(force=...)``
    surface the :class:`~tidal_stream.streaming.TidalStreamer` already calls, so
    the streamer is reused verbatim — but there is NO refresh/persist here: the
    resolver's expiry re-read (R2.2) picks up the value the watchdog refreshed
    out-of-band, and a ``force`` re-read simply invalidates the resolver's cache
    and re-resolves (R5.3, sidecar read-only).

    A resolution that yields
    :class:`~hellodj_platform_logic.user_credential_resolver.CredentialUnavailable`
    raises :class:`TidalCredentialUnavailableError` so a mid-session credential
    failure surfaces observably rather than streaming with a dead/absent token
    (R2.3, R5.4).

    Because every guild owned by the same ``sub`` resolves the SAME
    ``USER#<sub>/SOURCECRED#tidal`` item, binding the supplier to the guild that
    first created the session yields that user's token regardless of which of
    their guilds is streaming (per-user token, not per-guild — R5.1/R5.2).
    """

    def __init__(
        self,
        resolver: UserCredentialResolver,
        guild_id: str,
        *,
        provider: str = PROVIDER_TIDAL,
    ) -> None:
        self._resolver = resolver
        self._guild_id = str(guild_id)
        self._provider = provider

    def get_access_token(self, *, force: bool = False) -> str:
        """Return the owning user's current Tidal access token (read-only).

        Args:
            force: When ``True``, drop the resolver's cached resolution and
                re-read the credential item once so a token that died mid-stream
                is re-fetched from the watchdog-refreshed store value (R2.2). No
                OAuth refresh is performed here — the sidecar is read-only.

        Raises:
            TidalCredentialUnavailableError: When the guild has no owner, no Tidal
                credential, a ``refresh_status=failed`` credential, or an
                undecryptable blob — an observable failure with no fallback.
        """
        if force:
            self._resolver.invalidate(self._guild_id, self._provider)
        result = self._resolver.resolve(self._guild_id, self._provider)
        if isinstance(result, CredentialUnavailable):
            raise TidalCredentialUnavailableError(result.reason)
        access_token = str(result.get("access_token", "") or "")
        if not access_token:
            # A resolved credential with no usable access token is unusable for
            # streaming; surface it as observably unavailable (no dead token).
            raise TidalCredentialUnavailableError("no_credential")
        return access_token


class TidalUserClient:
    """One owning user's live Tidal streaming client (the registry session).

    Wraps a :class:`~tidal_stream.streaming.TidalStreamer` authenticated by a
    :class:`ReadOnlyTidalTokenSource` for that user. This is the ``S`` value the
    :data:`TidalSessionRegistry` holds per ``sub``; its :meth:`close` is invoked
    by the registry on eviction / idle-sweep / shutdown (R8).
    """

    def __init__(self, streamer: TidalStreamer) -> None:
        self._streamer = streamer

    @property
    def streamer(self) -> TidalStreamer:
        """The underlying authenticated Tidal streamer."""
        return self._streamer

    async def search(self, query: str, limit: int = 10):
        """Search this user's Tidal catalog (R5.1)."""
        return await self._streamer.search(query, limit=limit)

    async def get_stream_url(self, track_id: str) -> str:
        """Resolve a direct stream URL using this user's token (R5.1)."""
        return await self._streamer.get_stream_url(track_id)

    async def aclose(self) -> None:
        """Await the underlying streamer's HTTP session close (R8.4)."""
        await self._streamer.close()

    def close(self) -> None:
        """Synchronous close hook for the :class:`SessionRegistry` closer (R8).

        The registry's closer is synchronous, but the underlying aiohttp session
        close is a coroutine. This schedules :meth:`aclose` on the running event
        loop when one is present (eviction/idle-sweep happen on the loop), and
        falls back to running it to completion otherwise (shutdown paths). It is
        best-effort: a failed close never strands the registry (R8.4). No token
        material is touched or logged.
        """
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(self.aclose())
            return
        try:
            asyncio.run(self.aclose())
        except Exception:  # noqa: BLE001 - best-effort teardown, no token material
            pass


#: The per-user Tidal session pool: a shared :class:`SessionRegistry` keyed by
#: the owning user's Cognito ``sub`` holding one :class:`TidalUserClient` each
#: (R6.1, R8). This alias is the concrete instantiation the design names
#: ``TidalSessionRegistry = SessionRegistry[str, TidalClient]``.
TidalSessionRegistry = SessionRegistry[str, TidalUserClient]


class TidalStreamRouter:
    """Route a guild request to its owning user's Tidal client (read-only).

    Ties together the guild→owner :class:`OwnerLookup`, the per-user
    :class:`UserCredentialResolver`, and the per-``sub``
    :data:`TidalSessionRegistry`. For a request carrying ``guild_id`` it:

    1. resolves the guild's owning ``sub`` server-side (never exposed in the URL
       or logs); a guild with no owner is an observable failure (R5.4);
    2. returns that ``sub``'s live :class:`TidalUserClient` from the registry,
       building it on a miss via a factory that binds a
       :class:`ReadOnlyTidalTokenSource` for the requesting guild.

    The ``sub`` keying guarantees no cross-user leakage (R6.1): two guilds owned
    by DISTINCT users get distinct clients bound to distinct tokens; two guilds
    owned by the SAME user share that user's client (same token — correct).
    """

    def __init__(
        self,
        owners: OwnerLookup,
        resolver: UserCredentialResolver,
        registry: TidalSessionRegistry,
        *,
        streamer_factory: Callable[[ReadOnlyTidalTokenSource], TidalStreamer],
        provider: str = PROVIDER_TIDAL,
    ) -> None:
        self._owners = owners
        self._resolver = resolver
        self._registry = registry
        self._streamer_factory = streamer_factory
        self._provider = provider

    @property
    def registry(self) -> TidalSessionRegistry:
        """The per-``sub`` session registry (for health + shutdown)."""
        return self._registry

    def client_for_guild(self, guild_id: str | int) -> TidalUserClient:
        """Return the owning user's live Tidal client for ``guild_id`` (R5.1).

        Raises:
            TidalCredentialUnavailableError: When the guild has no recorded owner
                (``no_owner``) or the per-user session cannot be built because
                the credential is unavailable — an observable failure with no
                cross-user fallback (R5.4, R10.5).
        """
        gid = str(guild_id)
        try:
            owner_sub = self._owners.owner_of(gid)
        except Exception as exc:  # noqa: BLE001 - unavailable → observable no_owner
            log.info("tidal-stream: owner lookup failed for guild %s (%s)", gid, exc)
            raise TidalCredentialUnavailableError("no_owner") from exc
        if not owner_sub:
            log.info("tidal-stream: guild %s has no recorded owner", gid)
            raise TidalCredentialUnavailableError("no_owner")

        def _factory(_sub: str) -> TidalUserClient:
            """Build the owning user's Tidal client, read-only per-user token.

            Eagerly resolves the credential once so a missing/failed credential
            becomes a SPECIFIC per-``sub`` failure state on the registry
            (R7.2/R7.4) rather than a green-but-broken session.
            """
            token_source = ReadOnlyTidalTokenSource(
                self._resolver, gid, provider=self._provider
            )
            try:
                token_source.get_access_token()
            except TidalCredentialUnavailableError as exc:
                raise SessionCreateError(exc.reason) from exc
            return TidalUserClient(self._streamer_factory(token_source))

        try:
            return self._registry.get_or_create(owner_sub, _factory)
        except SessionCreateError as exc:
            raise TidalCredentialUnavailableError(exc.reason) from exc
