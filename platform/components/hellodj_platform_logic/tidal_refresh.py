"""First-party Tidal OAuth token refresh (single-app-id path).

This module implements the pure decision/derivation logic for refreshing a
Tidal source access token through the HelloDJ-owned first-party OAuth
integration. It is the single source of truth for the refresh behavior
exercised by Property 14 and consumed by the ``tidal-stream`` component.

Behavior (Requirements 9.1-9.5):

    * The refresh path uses a **single** Tidal application identifier
      (``first_party_client.app_id``) and the HelloDJ-owned OAuth callback
      endpoint (R9.1, R9.2).
    * When a token is expired (or force-refreshed), the refresh operation
      produces a **non-expired** token via the first-party single-app-id path
      (R9.4).
    * The legacy two-client-id key-split path is rejected outright by a guard
      so it can never be used to obtain a token (R9.3).
    * The refresh is fully independent of Cognito: no Cognito types or calls
      appear here (R9.5).

Purity: the ``first_party_client`` is an injectable callable protocol, so this
module performs no live network calls and can be exercised directly by
property-based tests. The ``token_state`` and refreshed token are modeled as a
frozen dataclass so inputs/outputs are immutable.

Design references:
    * Auth flows (Tidal first-party OAuth sequence)
    * Correctness Property 14 (Tidal token refresh via first-party path)

Requirements: 9.1, 9.2, 9.3, 9.4, 9.5
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Identifier of the legacy authentication approach that split a single key
#: across two client identifiers. The guard rejects any client presenting this
#: mode so the legacy path can never produce a token (R9.3).
LEGACY_KEY_SPLIT_MODE = "two_client_id_key_split"

#: Identifier of the sanctioned first-party single-app-id auth mode (R9.1).
FIRST_PARTY_SINGLE_APP_ID_MODE = "first_party_single_app_id"

#: Skew (seconds) subtracted from ``now`` when deciding whether a token is
#: expired, so a token about to expire is treated as needing refresh.
DEFAULT_EXPIRY_SKEW_SECONDS = 0.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TidalRefreshError(Exception):
    """Base error for the first-party Tidal refresh logic."""


class LegacyKeySplitRejectedError(TidalRefreshError):
    """Raised when a legacy two-client-id key-split configuration is supplied.

    The legacy approach that splits a key across two client identifiers is
    removed from the platform; the guard raises this error rather than allowing
    a token to be obtained through it (R9.3).
    """


class TidalRefreshFailedError(TidalRefreshError):
    """Raised when the first-party client returns an unusable (expired) token.

    A successful refresh must yield a non-expired token (R9.4); if the injected
    first-party client returns a token that is already expired relative to the
    supplied ``now``, the refresh is treated as failed.
    """


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TidalTokenState:
    """Immutable snapshot of a Tidal source token.

    Attributes:
        access_token: The current access token value (may be empty when never
            acquired).
        refresh_token: The long-lived refresh token used by the first-party
            OAuth integration to mint a new access token.
        expires_at: Absolute expiry as an epoch-seconds timestamp. The token is
            considered expired when ``expires_at <= now + skew``.
    """

    access_token: str
    refresh_token: str
    expires_at: float

    def is_expired(
        self,
        now: float,
        skew_seconds: float = DEFAULT_EXPIRY_SKEW_SECONDS,
    ) -> bool:
        """Return whether the token is expired at ``now`` (with optional skew).

        A non-positive time-to-live means the token must be refreshed before
        use. ``skew_seconds`` lets callers treat a token that is about to expire
        as already expired.
        """
        return self.expires_at <= now + skew_seconds


@dataclass(frozen=True)
class FirstPartyClientConfig:
    """Configuration of the HelloDJ-owned first-party Tidal OAuth client.

    Attributes:
        app_id: The **single** Tidal application identifier used for all Tidal
            source auth (R9.1). Exactly one non-empty id is required.
        callback_url: The HelloDJ-owned OAuth callback endpoint (R9.2).
        auth_mode: The auth mode identifier; must be
            :data:`FIRST_PARTY_SINGLE_APP_ID_MODE`. Any value equal to
            :data:`LEGACY_KEY_SPLIT_MODE` is rejected by the guard (R9.3).
    """

    app_id: str
    callback_url: str
    auth_mode: str = FIRST_PARTY_SINGLE_APP_ID_MODE


@runtime_checkable
class FirstPartyRefreshClient(Protocol):
    """Injectable protocol for the first-party Tidal OAuth refresh client.

    Implementations exchange a refresh token for a fresh access token via the
    HelloDJ-owned single-app-id integration. Kept as a pure protocol so tests
    (and the runtime component) can inject a deterministic fake with no live
    network calls.
    """

    @property
    def config(self) -> FirstPartyClientConfig:
        """The first-party client configuration (single app id + callback)."""
        ...

    def refresh(self, refresh_token: str, now: float) -> TidalTokenState:
        """Exchange ``refresh_token`` for a new :class:`TidalTokenState`."""
        ...


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


def reject_legacy_key_split(config: FirstPartyClientConfig) -> None:
    """Reject a legacy two-client-id key-split configuration (R9.3).

    Raises:
        LegacyKeySplitRejectedError: If ``config.auth_mode`` is the legacy
            two-client-id key-split mode.
        TidalRefreshError: If the configuration is not a well-formed
            first-party single-app-id config (empty/missing app id or callback,
            or an unrecognized auth mode). A well-formed first-party config with
            exactly one non-empty application id passes the guard.
    """
    if config.auth_mode == LEGACY_KEY_SPLIT_MODE:
        raise LegacyKeySplitRejectedError(
            "legacy two-client-id key-split path is removed; "
            "use the first-party single-app-id integration"
        )
    if config.auth_mode != FIRST_PARTY_SINGLE_APP_ID_MODE:
        raise TidalRefreshError(
            f"unrecognized Tidal auth mode: {config.auth_mode!r}; "
            f"expected {FIRST_PARTY_SINGLE_APP_ID_MODE!r}"
        )
    if not config.app_id:
        raise TidalRefreshError(
            "first-party Tidal OAuth requires a single non-empty application id"
        )
    if not config.callback_url:
        raise TidalRefreshError(
            "first-party Tidal OAuth requires a HelloDJ-owned callback endpoint"
        )


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def refresh_tidal(
    token_state: TidalTokenState,
    first_party_client: FirstPartyRefreshClient,
    now: float,
    *,
    skew_seconds: float = DEFAULT_EXPIRY_SKEW_SECONDS,
    force: bool = False,
) -> TidalTokenState:
    """Return a non-expired Tidal token via the first-party single-app-id path.

    If ``token_state`` is still valid at ``now`` and ``force`` is False, the
    existing token is returned unchanged. Otherwise the injected first-party
    client is used to mint a fresh token through the HelloDJ-owned single-app-id
    OAuth integration; the legacy two-client-id key-split path is rejected
    before any refresh occurs (R9.3).

    Args:
        token_state: The current token snapshot.
        first_party_client: The injected first-party OAuth refresh client.
        now: Current time as an epoch-seconds timestamp.
        skew_seconds: Expiry skew used when deciding whether to refresh.
        force: When True, always refresh even if the current token is valid.

    Returns:
        A :class:`TidalTokenState` that is non-expired at ``now`` and carries a
        non-empty refresh token for the next cycle (R9.4).

    Raises:
        LegacyKeySplitRejectedError: If the client is configured for the legacy
            two-client-id key-split path (R9.3).
        TidalRefreshError: If the client configuration is not a valid
            first-party single-app-id config.
        TidalRefreshFailedError: If the client returns an already-expired token
            (R9.4 not satisfied).
    """
    config = first_party_client.config

    # Guard first: the legacy key-split path must never mint a token (R9.3).
    reject_legacy_key_split(config)

    # Fast path: a still-valid token needs no refresh unless forced.
    if not force and not token_state.is_expired(now, skew_seconds):
        return token_state

    refreshed = first_party_client.refresh(token_state.refresh_token, now)

    # A successful refresh must produce a non-expired token (R9.4).
    if refreshed.is_expired(now, skew_seconds):
        raise TidalRefreshFailedError(
            "first-party refresh returned an already-expired token"
        )

    # Preserve the prior refresh token when the client does not rotate it, so
    # the next refresh cycle can proceed (R9.4).
    if not refreshed.refresh_token:
        refreshed = replace(refreshed, refresh_token=token_state.refresh_token)

    return refreshed
