"""Pure helpers for the legacy-secret -> encrypted-credential backfill.

Extracted from :mod:`migration_job.source_credential_backfill` to keep that
orchestration module within the 500-line ceiling (mirrors how the web-ui split
``source_credential_store`` out of ``auth``). Everything here is side-effect
free: key builders, the legacy secret-name parser, and the legacy-JSON ->
:class:`~hellodj_platform_logic.source_refresh.TokenState` mappers.

The constants + item-key helpers are mirrored VERBATIM from the web-ui
``source_credential_service`` so a backfilled item is byte-for-byte the item a
fresh connect writes; the token-state mappers mirror the web-ui
``source_credential_store`` so the backfilled blob decrypts to the same shape a
reader/watchdog expect (R2.6, R6.5). The migration component does not import the
web-ui package — the two are separate deployables — so these are a deliberate
shared copy; change them together.

No token material is ever logged from here (these are pure functions returning
values; logging lives in the orchestration module).

Requirements: 2.6, 6.5
"""

from __future__ import annotations

from typing import Any

from hellodj_platform_logic.source_refresh import TokenState

__all__ = [
    "SUPPORTED_PROVIDERS",
    "SOURCECRED_SK_PREFIX",
    "SOURCECRED_ENTITY_TYPE",
    "REFRESH_STATUS_OK",
    "OWNER_SK",
    "TIDAL_STATUS_EXPIRES_AT",
    "user_pk",
    "sourcecred_sk",
    "guild_owner_pk",
    "guild_secret_prefix",
    "parse_guild_secret_name",
    "legacy_secret_to_token_state",
]

#: The providers a guild can own OAuth for. Kept in lock-step with the web-ui
#: ``source_credential_service`` / ``guild_sources`` and the bot's
#: ``guild_credentials`` module (all separate deployables — change together).
SUPPORTED_PROVIDERS = ("youtube", "youtube_music", "tidal", "spotify")

#: Sort-key prefix + entityType of a per-user source-credential item — mirrored
#: VERBATIM from the web-ui ``source_credential_service`` so the backfilled item
#: is byte-for-byte the item a fresh connect would write.
SOURCECRED_SK_PREFIX = "SOURCECRED#"
SOURCECRED_ENTITY_TYPE = "SourceCredential"

#: Plaintext ``refresh_status`` set on a freshly written credential.
REFRESH_STATUS_OK = "ok"

#: Sort key of a guild's ownership item (``guild_admin_service.OWNER_SK``).
OWNER_SK = "OWNER"

#: Sentinel far-future ``expires_at`` for a status-only (Tidal) credential item,
#: matching web-ui ``source_credential_store.TIDAL_STATUS_EXPIRES_AT`` so the
#: watchdog's near-expiry scan skips a credential the web-ui holds no refresh
#: token to renew.
TIDAL_STATUS_EXPIRES_AT = 99_999_999_999.0


def user_pk(sub: str) -> str:
    """Return the ``hellodj-core`` partition key for a user's items.

    Mirrors ``source_credential_service.user_pk`` so the backfill writes the
    SAME item the web-ui creates on a fresh connect.
    """
    return f"USER#{sub}"


def sourcecred_sk(provider: str) -> str:
    """Return the sort key for a user's per-provider credential item."""
    return f"{SOURCECRED_SK_PREFIX}{provider}"


def guild_owner_pk(guild_id: str) -> str:
    """Return the partition key for a guild's items (``guild_admin_service``)."""
    return f"GUILD#{guild_id}"


def guild_secret_prefix(stage: str) -> str:
    """Return the shared name prefix of every legacy per-guild secret.

    Matches ``guild_sources.guild_source_secret_name`` /
    ``guild_credentials.guild_source_secret_name`` up to (but not including) the
    ``<guildId>/<provider>`` tail, so a ``list_secrets`` name filter enumerates
    exactly the migration's input set.
    """
    return f"hellodj/{stage}/guild/"


def parse_guild_secret_name(name: str, stage: str) -> tuple[str, str] | None:
    """Parse ``(guild_id, provider)`` from a legacy secret name, or ``None``.

    Returns ``None`` (so the caller skips it) when the name does not match the
    ``hellodj/<stage>/guild/<gid>/<provider>`` shape or names an unsupported
    provider — this keeps a stray secret under the prefix from being migrated as
    garbage. The stage is validated so a cross-stage secret is never migrated
    into the wrong table.
    """
    prefix = guild_secret_prefix(stage)
    if not name.startswith(prefix):
        return None
    tail = name[len(prefix):]
    parts = tail.split("/")
    if len(parts) != 2:
        return None
    guild_id, provider = parts
    if not guild_id or provider not in SUPPORTED_PROVIDERS:
        return None
    return guild_id, provider


def _youtube_token_state(tokens: dict[str, Any]) -> TokenState:
    """Map a legacy YouTube secret dict to a :class:`TokenState`.

    Mirrors web-ui ``source_credential_store._youtube_token_state`` VERBATIM so
    the backfilled blob decrypts to the same shape a fresh connect would: the
    offline ``oauth_refresh_token`` becomes ``refresh_token`` (Google mints the
    access token on refresh, so ``access_token`` is empty and ``expires_at`` is
    ``0`` — the watchdog/reader refreshes it), and the PoToken pair rides in
    ``extra`` inside the encrypted blob rather than in plaintext.
    """
    return TokenState(
        access_token="",
        refresh_token=str(tokens.get("oauth_refresh_token", "") or ""),
        expires_at=0.0,
        scope="https://www.googleapis.com/auth/youtube",
        extra={
            "pot_token": str(tokens.get("pot_token", "") or ""),
            "pot_visitor_data": str(tokens.get("pot_visitor_data", "") or ""),
        },
    )


def _spotify_token_state(tokens: dict[str, Any]) -> TokenState:
    """Map a legacy Spotify secret dict to a :class:`TokenState`.

    Mirrors web-ui ``source_credential_store._spotify_token_state``.
    """
    return TokenState(
        access_token=str(tokens.get("access_token", "") or ""),
        refresh_token=str(tokens.get("refresh_token", "") or ""),
        expires_at=float(tokens.get("expires_at", 0.0) or 0.0),
        scope=str(tokens.get("scope", "") or ""),
    )


def _tidal_token_state(tokens: dict[str, Any]) -> TokenState:
    """Map a legacy Tidal secret dict to a status-oriented :class:`TokenState`.

    The ``tidal-stream`` sidecar owns Tidal's token lifecycle, so like the
    web-ui the backfill records a *connection status* with a far-future
    ``expires_at`` (kept out of the near-expiry scan). Whatever refresh token the
    legacy secret carried is preserved inside the encrypted blob so the sidecar
    reader still finds it.
    """
    return TokenState(
        access_token="",
        refresh_token=str(
            tokens.get("refresh_token", "")
            or tokens.get("oauth_refresh_token", "")
            or ""
        ),
        expires_at=TIDAL_STATUS_EXPIRES_AT,
        scope="",
        extra={"owned_by": "tidal-stream"},
    )


def legacy_secret_to_token_state(
    provider: str, tokens: dict[str, Any]
) -> TokenState:
    """Map a legacy secret JSON dict to a :class:`TokenState` by provider.

    Reuses the exact mapping shapes the web-ui uses on a fresh connect so a
    backfilled item is indistinguishable from a freshly connected one.
    """
    if provider in ("youtube", "youtube_music"):
        return _youtube_token_state(tokens)
    if provider == "tidal":
        return _tidal_token_state(tokens)
    return _spotify_token_state(tokens)
