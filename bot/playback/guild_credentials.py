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
    "GuildCredentialResolver",
    "SecretsReader",
    "guild_source_secret_name",
]

log = logging.getLogger(__name__)

#: The music providers a guild can own OAuth for. Kept in lock-step with the
#: web-ui's ``guild_sources.SUPPORTED_PROVIDERS``.
SUPPORTED_PROVIDERS = ("youtube", "youtube_music", "tidal", "spotify")

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
