"""HelloDJ — Per-guild source credential resolution (bot playback path).

Resolves a guild's per-provider OAuth tokens from AWS Secrets Manager at play
time so a track played in guild A uses guild A's Tidal/Spotify/YouTube auth and
guild B uses guild B's, isolated from every other guild (R6.1, R6.3).

Per_Guild_Secret naming (isolated per guild+provider) — shared VERBATIM with the
web-ui's ``guild_sources.guild_source_secret_name`` so both sides address the
SAME secret::

    hellodj/<stage>/guild/<guildId>/<provider>

The ``guild/<guildId>/`` path segment is what isolates one guild's tokens from
another's, and is the exact prefix the bot's IAM read grant is scoped to
(``hellodj/<stage>/guild/*``).

Fallback (R6.2): if a guild has no secret for a provider, the resolver falls
back to the optional Platform_Owner-controlled global secret (default name
``hellodj/<stage>/<globalLeaf>`` — e.g. ``hellodj/<stage>/tidal-refresh`` /
``hellodj/<stage>/spotify``); if neither exists the provider is skipped
gracefully (``None``).

YouTube / YouTube_Music — per-guild capture, NO global fallback leaf
--------------------------------------------------------------------

``youtube`` and ``youtube_music`` intentionally have **no** entry in
:data:`GLOBAL_FALLBACK_LEAVES`. This is a deliberate, load-bearing design choice
that must NOT be changed:

* A guild that HAS connected its own YouTube (a per-guild secret
  ``hellodj/<stage>/guild/<gid>/youtube`` holding ``oauth_refresh_token`` +
  ``pot_token`` + ``pot_visitor_data``) has those exact creds resolved here and
  injected into Lavalink just-in-time, immediately before that guild's track is
  resolved/played (see :class:`YouTubeCredentialInjector`).
* A guild that has NO per-guild YouTube secret resolves to ``None`` and therefore
  triggers NO per-guild swap — it plays through the **untouched** global
  credential-store push (``bot.py:push_youtube_oauth`` → single ``POST /youtube``)
  exactly as before this change (preservation 3.5). Adding a youtube global
  fallback leaf here would break that separation, so ``GLOBAL_FALLBACK_LEAVES``
  keeps ONLY ``tidal`` and ``spotify`` (3.7).

SHARED-LAVALINK LIMITATION
--------------------------

The youtube-source plugin's ``POST /youtube`` replaces ALL credential fields on
every call, so one shared Lavalink node can hold only ONE YouTube credential set
at a time. :class:`YouTubeCredentialInjector` performs a just-in-time
last-writer-wins swap serialized by a per-node :class:`asyncio.Lock` (held from
the push through track resolution), which guarantees each *resolution* uses the
correct guild's creds. It does NOT provide true concurrent per-guild isolation on
a single node — two guilds resolving YouTube tracks at the very same instant
still serialize on the node lock. The fully isolated answer (a node-per-guild
Lavalink pool) is deferred (Design Risks #1).

Resolution is cached per ``(guild_id, provider)`` with a bounded TTL and
refreshed on expiry (R6.4). The cache key includes the guild id — combined with
the guild-scoped secret name this guarantees one guild's tokens are never
returned for another guild (R6.3).

Tokens are never written to logs.

Requirements: 6.1, 6.2, 6.3, 6.4
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

__all__ = [
    "GLOBAL_FALLBACK_LEAVES",
    "SUPPORTED_PROVIDERS",
    "YOUTUBE_PROVIDERS",
    "GuildCredentialResolver",
    "SecretsReader",
    "YouTubeCredentialInjector",
    "YouTubePush",
    "guild_source_secret_name",
    "youtube_oauth_payload",
]

log = logging.getLogger(__name__)

#: The music providers a guild can own OAuth for. Kept in lock-step with the
#: web-ui's ``guild_sources.SUPPORTED_PROVIDERS``.
SUPPORTED_PROVIDERS = ("youtube", "youtube_music", "tidal", "spotify")

#: The YouTube-family providers that use the per-guild ``POST /youtube`` swap.
#: These are exactly the providers with NO global fallback leaf.
YOUTUBE_PROVIDERS = ("youtube", "youtube_music")

#: Default provider → global-secret leaf mapping for the optional fallback
#: (R6.2, R5.5). The full global name is ``hellodj/<stage>/<leaf>``, matching
#: the AuthStack stage-scoped secret naming (``tidal-refresh`` / ``spotify``).
#: Providers absent from this map (youtube / youtube_music) have no global
#: fallback and are simply skipped when the guild has no secret.
GLOBAL_FALLBACK_LEAVES: dict[str, str] = {
    "tidal": "tidal-refresh",
    "spotify": "spotify",
}

#: Default cache time-to-live, in seconds.
DEFAULT_TTL_SECONDS = 300.0


def guild_source_secret_name(stage: str, guild_id: str, provider: str) -> str:
    """Return the Per_Guild_Secret name for a guild+provider (isolated).

    Shared verbatim with the web-ui so both the writer (web-ui) and the reader
    (this resolver) address the SAME secret. The ``guild/<guildId>/`` segment
    isolates one guild's tokens from every other guild's (R6.1, R6.3).
    """
    return f"hellodj/{stage}/guild/{guild_id}/{provider}"


class SecretsReader(Protocol):
    """Subset of the boto3 ``secretsmanager`` client used for reads only.

    The bot's IAM grant is read-only on ``hellodj/<stage>/guild/*`` (R7.2), so
    this resolver only ever calls ``get_secret_value``.
    """

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]: ...


class GuildCredentialResolver:
    """Resolve a guild's per-provider tokens (cached, bounded TTL, fallback).

    Parameters
    ----------
    secrets_client:
        A boto3 ``secretsmanager`` client (or any object satisfying
        :class:`SecretsReader`).
    stage:
        The deployment stage (``beta`` / ``staging`` / ``production``) used in
        the secret name.
    global_fallback_leaves:
        Optional provider → global-secret-leaf map for the fallback. Defaults to
        :data:`GLOBAL_FALLBACK_LEAVES`. Pass an empty dict to disable the global
        fallback entirely.
    ttl_seconds:
        Bounded cache TTL. A resolution is reused for at most this many seconds
        before it is refreshed from Secrets Manager (R6.4).
    time_fn:
        Injectable monotonic clock (defaults to :func:`time.monotonic`) so the
        cache TTL is deterministically testable.
    """

    def __init__(
        self,
        secrets_client: SecretsReader,
        *,
        stage: str,
        global_fallback_leaves: dict[str, str] | None = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._secrets = secrets_client
        self._stage = stage
        self._global_leaves = (
            GLOBAL_FALLBACK_LEAVES
            if global_fallback_leaves is None
            else dict(global_fallback_leaves)
        )
        self._ttl = float(ttl_seconds)
        self._now = time_fn
        # cache: (guild_id, provider) -> (expires_at_monotonic, value)
        self._cache: dict[tuple[str, str], tuple[float, dict[str, Any] | None]] = {}

    def is_supported(self, provider: str) -> bool:
        """Return whether ``provider`` is a supported source."""
        return provider in SUPPORTED_PROVIDERS

    def resolve(self, guild_id: str | int, provider: str) -> dict[str, Any] | None:
        """Resolve a guild's tokens for a provider.

        Returns the parsed token dict for the guild's own Per_Guild_Secret when
        present (R6.1); otherwise the global fallback tokens if configured and
        present (R6.2); otherwise ``None`` so the caller skips the provider
        gracefully. Results are cached per ``(guild_id, provider)`` with a
        bounded TTL and refreshed on expiry (R6.4). The guild-scoped cache key
        and secret name guarantee no cross-guild leakage (R6.3).
        """
        gid = str(guild_id)
        key = (gid, provider)

        cached = self._cache.get(key)
        if cached is not None and self._now() < cached[0]:
            return cached[1]

        value = self._load(gid, provider)
        self._cache[key] = (self._now() + self._ttl, value)
        return value

    def invalidate(self, guild_id: str | int, provider: str) -> None:
        """Drop any cached resolution for a ``(guild_id, provider)`` pair."""
        self._cache.pop((str(guild_id), provider), None)

    # ── internals ───────────────────────────────────────────────────────

    def _load(self, guild_id: str, provider: str) -> dict[str, Any] | None:
        """Load tokens: guild secret first, then global fallback (R6.1, R6.2)."""
        name = guild_source_secret_name(self._stage, guild_id, provider)
        tokens = self._read_secret(name)
        if tokens is not None:
            return tokens

        leaf = self._global_leaves.get(provider)
        if leaf is None:
            return None
        global_name = f"hellodj/{self._stage}/{leaf}"
        fallback = self._read_secret(global_name)
        if fallback is not None:
            log.info(
                "guild_credentials: guild %s provider %s using global fallback",
                guild_id, provider,
            )
        return fallback

    def _read_secret(self, name: str) -> dict[str, Any] | None:
        """Fetch + JSON-parse a secret; return ``None`` if absent/unreadable.

        Never logs the secret value — only its name and the failure reason.
        """
        try:
            resp = self._secrets.get_secret_value(SecretId=name)
        except Exception as exc:  # noqa: BLE001 - absent/denied → treat as missing
            log.debug("guild_credentials: secret %s not available (%s)", name, exc)
            return None
        raw = resp.get("SecretString")
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("guild_credentials: secret %s is not valid JSON", name)
            return None
        if not isinstance(parsed, dict):
            log.warning("guild_credentials: secret %s is not a JSON object", name)
            return None
        return parsed


# ── YouTube per-guild just-in-time credential injection ─────────────────


def youtube_oauth_payload(
    tokens: dict[str, Any] | None,
    *,
    skip_initialization: bool = False,
) -> dict[str, Any] | None:
    """Build the ``POST /youtube`` payload from an explicit token dict.

    This is the SINGLE payload builder shared by the bot's global push
    (``bot.py:push_youtube_oauth``) and the per-guild just-in-time swap. It
    encodes the load-bearing invariant that OAuth refresh token AND poToken +
    visitorData are sent TOGETHER in ONE request — the youtube-source plugin
    replaces ALL fields on each call, so splitting them would erase the first
    (see hellodj-architecture "single POST /youtube request").

    Parameters
    ----------
    tokens:
        A dict that may contain ``oauth_refresh_token`` (or ``refresh_token``),
        ``pot_token``, and ``pot_visitor_data``. Missing/empty fields are simply
        omitted from the payload.
    skip_initialization:
        Value for the plugin's ``skipInitialization`` field.

    Returns
    -------
    dict | None
        The payload dict, or ``None`` when there is neither a refresh token nor a
        complete poToken pair to push (caller should skip the request).
    """
    tokens = tokens or {}
    refresh = (
        tokens.get("oauth_refresh_token")
        or tokens.get("refreshToken")
        or tokens.get("refresh_token")
        or ""
    )
    pot_token = tokens.get("pot_token") or tokens.get("poToken") or ""
    pot_visitor = (
        tokens.get("pot_visitor_data")
        or tokens.get("visitorData")
        or tokens.get("visitor_data")
        or ""
    )

    if not refresh and not (pot_token and pot_visitor):
        return None

    payload: dict[str, Any] = {"skipInitialization": skip_initialization}
    if refresh:
        payload["refreshToken"] = refresh
    if pot_token and pot_visitor:
        payload["poToken"] = pot_token
        payload["visitorData"] = pot_visitor
    return payload


class YouTubePush(Protocol):
    """Seam for issuing the ``POST /youtube`` request to a Lavalink node.

    Implemented for real by ``bot.py`` (an aiohttp POST to
    ``{LAVALINK_URI}/youtube``); replaced by a fake in unit tests so the
    injector is testable from ``bot/playback/`` without a live Lavalink or the
    discord/wavelink stack. Returns whether the push succeeded.
    """

    async def __call__(self, payload: dict[str, Any]) -> bool: ...


class YouTubeCredentialInjector:
    """Just-in-time per-guild YouTube credential swap on a shared Lavalink node.

    Before a guild's YouTube track is resolved/played, :meth:`inject_for_guild`
    resolves that guild's own ``{oauth_refresh_token, pot_token,
    pot_visitor_data}`` and pushes them via the single ``POST /youtube`` request
    (last-writer-wins). Guilds WITHOUT a per-guild YouTube secret cause NO swap —
    the caller falls through to the untouched global push (preservation 3.5).

    The swap is serialized with a per-Lavalink-node :class:`asyncio.Lock` so a
    concurrent resolution for another guild cannot interleave between the push
    and the track resolution. The caller holds the returned lock context across
    the push AND the subsequent resolve/play (see :meth:`swap_lock`).

    Parameters
    ----------
    resolver:
        A :class:`GuildCredentialResolver` used to fetch the per-guild secret.
    push:
        A :class:`YouTubePush` seam that issues the actual ``POST /youtube``.
    """

    def __init__(self, resolver: "GuildCredentialResolver", push: YouTubePush) -> None:
        self._resolver = resolver
        self._push = push
        # node key -> lock. A single shared node uses one lock; a future
        # node-per-guild pool would key by node uri.
        self._locks: dict[str, "asyncio.Lock"] = {}

    def swap_lock(self, node_key: str = "default") -> "asyncio.Lock":
        """Return the per-node lock, creating it on first use.

        The caller MUST hold this lock across the credential push AND the track
        resolution so a concurrent per-guild swap cannot clobber the node's creds
        mid-resolution (SHARED-LAVALINK LIMITATION).
        """
        import asyncio

        lock = self._locks.get(node_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[node_key] = lock
        return lock

    def resolve_youtube(self, guild_id: str | int, provider: str) -> dict[str, Any] | None:
        """Return a guild's per-guild YouTube tokens, or ``None`` if it has none.

        Only youtube / youtube_music are per-guild-swappable; any other provider
        returns ``None`` (its resolution/fallback is handled elsewhere).
        """
        if provider not in YOUTUBE_PROVIDERS:
            return None
        tokens = self._resolver.resolve(guild_id, provider)
        if not tokens or not isinstance(tokens, dict):
            return None
        # Only treat it as a usable per-guild secret when a refresh token is
        # present — matches the stored shape written by the web-ui.
        if not (tokens.get("oauth_refresh_token") or tokens.get("refresh_token")):
            return None
        return tokens

    async def inject_for_guild(self, guild_id: str | int, provider: str) -> bool:
        """Resolve + push a guild's own YouTube creds if it has a per-guild secret.

        Returns ``True`` when a per-guild swap was performed (this guild's creds
        are now loaded on the node), ``False`` when the guild has no per-guild
        secret and the caller should use the untouched global push (3.5).

        NOTE: the caller is expected to hold :meth:`swap_lock` around this call
        and the subsequent track resolution.
        """
        tokens = self.resolve_youtube(guild_id, provider)
        if tokens is None:
            return False
        payload = youtube_oauth_payload(tokens, skip_initialization=False)
        if payload is None:
            return False
        ok = await self._push(payload)
        if ok:
            log.info(
                "guild_credentials: swapped per-guild YouTube creds for guild %s "
                "(provider=%s) before playback",
                guild_id, provider,
            )
        else:
            log.warning(
                "guild_credentials: per-guild YouTube cred swap POST failed for "
                "guild %s (provider=%s)",
                guild_id, provider,
            )
        return ok
