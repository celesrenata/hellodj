"""Map per-guild OAuth callback tokens into the unified credential store.

Extracted from :mod:`auth` to keep that module within the 500-line ceiling
(mirrors how :mod:`auth_forms` / :mod:`source_token_exchange` were split out).

The web-ui already completes the code->token exchange per provider
(:mod:`source_token_exchange` for YouTube/Spotify; the Tidal sidecar-forward
path for Tidal). Task 5 of the unified-oauth-and-token-watchdog spec routes the
result of a *successful* exchange into the new encrypted DynamoDB store
(:class:`SourceCredentialService`) IN ADDITION to the existing per-guild
Secrets Manager write, so:

* NEW credentials land in DynamoDB (envelope-encrypted), keyed by the connecting
  user's Cognito subject — the owner (design.md "guild-owned source connections
  write a credential item keyed by the connecting user's sub") (R1.4, R2.1).
* The legacy per-guild secret write/read stays in place as the migration
  fallback (R2.6) — this module never removes it.
* Tidal's tokens are owned by the ``tidal-stream`` sidecar, so only its
  *connection status* is recorded in DynamoDB (an empty-token
  :class:`TokenState`), never a token this component doesn't hold.

Every function is a thin, side-effect-scoped helper: it maps the provider's
already-exchanged token dict to a
:class:`~hellodj_platform_logic.source_refresh.TokenState` and calls
``service.store(...)``. When no unified store is wired (degraded mode) the
helpers no-op, so the legacy path is unaffected. Token material is never logged.

Requirements: 1.4, 2.1, 2.6
"""

from __future__ import annotations

from typing import Any

from hellodj_platform_logic.source_refresh import TokenState

from source_credential_service import SourceCredentialService
from spotify_librespot_capture import LIBRESPOT_CREDENTIALS_EXTRA_KEY

__all__ = [
    "TIDAL_STATUS_EXPIRES_AT",
    "persist_youtube_credential",
    "persist_spotify_credential",
    "persist_tidal_status",
    "persist_librespot_credentials",
]

#: Sentinel far-future ``expires_at`` (epoch seconds ~ year 5138) for a
#: status-only Tidal credential item. The web-ui holds no Tidal refresh token
#: (the ``tidal-stream`` sidecar owns Tidal's token lifecycle), so this keeps
#: the credential OUT of the watchdog's near-expiry refresh scan while still
#: recording the connection status for the UI.
TIDAL_STATUS_EXPIRES_AT = 99_999_999_999.0


def _youtube_token_state(tokens: dict[str, Any]) -> TokenState:
    """Map a composed YouTube secret dict to a :class:`TokenState`.

    The web-ui holds only the offline ``oauth_refresh_token`` (+ the PoToken /
    visitor-data pair the playback path needs); Google mints the access token on
    refresh, so ``access_token`` is empty and ``expires_at`` is ``0`` (the
    watchdog / reader refreshes from the refresh token). The PoToken material is
    carried in ``extra`` so it round-trips with the encrypted blob rather than
    living in plaintext.
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
    """Map an exchanged Spotify token dict to a :class:`TokenState`.

    Carries the refresh token plus the access token / expiry when the exchange
    returned them (``source_exchange_spotify`` includes them only when present).
    """
    return TokenState(
        access_token=str(tokens.get("access_token", "") or ""),
        refresh_token=str(tokens.get("refresh_token", "") or ""),
        expires_at=float(tokens.get("expires_at", 0.0) or 0.0),
        scope=str(tokens.get("scope", "") or ""),
    )


def persist_youtube_credential(
    service: SourceCredentialService | None,
    sub: str,
    provider: str,
    tokens: dict[str, Any],
) -> None:
    """Persist an exchanged YouTube/YouTube Music credential (encrypted).

    No-ops when no unified store is wired (``service is None``) or when the
    exchange yielded no refresh token (so nothing partial is written — the
    callback surfaces its clear error separately). ``connected_by`` is the
    connecting user's own subject (the owner).
    """
    if service is None or not sub:
        return
    state = _youtube_token_state(tokens)
    if not state.refresh_token:
        return
    service.store(sub, provider, state, connected_by=sub)


def persist_spotify_credential(
    service: SourceCredentialService | None,
    sub: str,
    tokens: dict[str, Any],
) -> None:
    """Persist an exchanged Spotify credential (encrypted DynamoDB).

    No-ops in degraded mode or when there is no refresh token to store.
    """
    if service is None or not sub:
        return
    state = _spotify_token_state(tokens)
    if not state.refresh_token:
        return
    service.store(sub, "spotify", state, connected_by=sub)


def persist_librespot_credentials(
    service: SourceCredentialService | None,
    sub: str,
    librespot_credentials: dict[str, Any],
) -> bool:
    """Attach a captured librespot reusable blob to the user's Spotify item.

    Decrypt-merge-re-encrypt on the EXISTING Spotify credential (written first
    by the standard OAuth connect): the prior token (refresh/access token,
    expiry, scope, other ``extra`` fields) is preserved verbatim and only
    ``extra[LIBRESPOT_CREDENTIALS_EXTRA_KEY]`` is added/replaced, so the reusable
    ``{username, credentials, type}`` object lives INSIDE the SAME
    envelope-encrypted blob — never a plaintext column (design "Data Models";
    R3.3, R6.4, R10.3). The per-user librespot session factory (task 2.3) then
    builds a ``Session`` non-interactively from it.

    Returns ``True`` when the blob was attached, ``False`` in degraded mode
    (no store / no sub / empty blob) or when there is no Spotify credential to
    attach to (the caller surfaces a clear error rather than a silent no-op).
    Token material is never logged. ``connected_by`` stays the owner's own sub.
    """
    if service is None or not sub or not librespot_credentials:
        return False
    prior = service.load_token(sub, "spotify")
    if prior is None:
        return False
    merged_extra = dict(prior.extra)
    merged_extra[LIBRESPOT_CREDENTIALS_EXTRA_KEY] = dict(librespot_credentials)
    service.store(
        sub,
        "spotify",
        TokenState(
            access_token=prior.access_token,
            refresh_token=prior.refresh_token,
            expires_at=prior.expires_at,
            scope=prior.scope,
            extra=merged_extra,
        ),
        connected_by=sub,
    )
    return True


def persist_tidal_status(
    service: SourceCredentialService | None,
    sub: str,
) -> None:
    """Record a Tidal *connection status* in DynamoDB (no token blob).

    Tidal tokens are owned by the ``tidal-stream`` sidecar (the callback is
    forwarded there), so the web-ui does not hold a Tidal token to store. To let
    the UI and watchdog SEE the connection we still write a credential item with
    an empty-token :class:`TokenState`: it marks ``connected`` with a plaintext
    status and an encrypted (empty) blob. Its ``expires_at`` is set far in the
    future (:data:`TIDAL_STATUS_EXPIRES_AT`) so the watchdog's near-expiry scan
    skips it — the web-ui holds no Tidal refresh token to renew (the sidecar
    owns Tidal's token lifecycle). No-ops in degraded mode.
    """
    if service is None or not sub:
        return
    state = TokenState(
        access_token="",
        refresh_token="",
        expires_at=TIDAL_STATUS_EXPIRES_AT,
        scope="",
        extra={"owned_by": "tidal-stream"},
    )
    service.store(sub, "tidal", state, connected_by=sub)
