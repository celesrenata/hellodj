"""Unified per-provider OAuth token refresh contract + provider clients.

This module generalizes the single-provider :mod:`hellodj_platform_logic.tidal_refresh`
shape into one provider-agnostic refresh contract that every OAuth source
implements, so the durable token-refresh watchdog can refresh any provider
uniformly (Requirement 4).

Behavior (Requirements 4.1-4.6, 10.2):

    * :class:`TokenState` is an immutable snapshot of a stored source token
      (access token, refresh token, absolute expiry, scope, and a mapping of
      provider-specific extra fields). It mirrors the Tidal
      :class:`~hellodj_platform_logic.tidal_refresh.TidalTokenState` shape (R4.1).
    * :class:`RefreshClient` is the one pure contract each provider implements:
      given a refresh token and ``now`` it returns a fresh :class:`TokenState`
      (R4.1, R4.2).
    * :func:`apply_refresh` is the shared decision/derivation function. It
      fast-paths a still-valid token, mints a fresh one via the client when
      expired or forced, preserves the prior refresh token when the provider
      does not rotate it (R4.3), and treats an already-expired result as a
      failed refresh rather than a stored success (R4.4). This exactly mirrors
      :func:`hellodj_platform_logic.tidal_refresh.refresh_tidal`.
    * :class:`GoogleRefreshClient` (youtube / youtube_music) and
      :class:`SpotifyRefreshClient` implement the contract by POSTing a
      ``grant_type=refresh_token`` form to the provider token endpoint. The HTTP
      form-post is injected as a callable so the clients are unit-testable with
      no network (R4.2).
    * :class:`TidalRefreshClient` is a thin adapter that delegates to the
      EXISTING :func:`hellodj_platform_logic.tidal_refresh.refresh_tidal` +
      first-party client, so Tidal's behavior and its property tests are
      untouched (R4.5, R10.2).
    * ``discord`` is identity-only: it has no playback token to refresh, so it
      has no refresh client here (R4.6).

Purity / testability: the Google/Spotify clients take an injectable
``http_post`` callable (a form poster), so the module performs no live network
calls and can be exercised directly by property-based tests. ``TokenState`` is a
frozen dataclass so inputs/outputs are immutable.

Design reference: design.md "Unified refresh contract (``source_refresh``)" and
Correctness Property 4 (refresh soundness) / Property 7 (Tidal no-regression).

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 10.2
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, runtime_checkable

from .tidal_refresh import (
    FirstPartyRefreshClient,
    TidalTokenState,
    refresh_tidal,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Provider identifiers that have an OAuth refresh grant (``discord`` is
#: identity-only and ``soundcloud`` is search-only, so neither appears here).
PROVIDER_YOUTUBE = "youtube"
PROVIDER_YOUTUBE_MUSIC = "youtube_music"
PROVIDER_SPOTIFY = "spotify"
PROVIDER_TIDAL = "tidal"

#: Google (YouTube / YouTube Music) OAuth token endpoint.
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

#: Spotify OAuth token endpoint.
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"

#: Default skew (seconds) subtracted from a token's TTL when deciding whether it
#: needs refresh, so a token about to expire is refreshed pre-emptively.
DEFAULT_EXPIRY_SKEW_SECONDS = 0.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SourceRefreshError(Exception):
    """Base error for the unified source refresh logic.

    Error messages carry no token material so a log line or traceback can never
    leak a refresh/access token.
    """


class RefreshFailedError(SourceRefreshError):
    """Raised when a refresh client returns an unusable (expired) token (R4.4).

    A successful refresh must yield a non-expired token; if the client returns a
    token that is already expired relative to ``now``, the refresh is treated as
    failed and nothing is stored as a success.
    """


class ProviderTokenError(SourceRefreshError):
    """Raised when a provider token endpoint returns an unusable response.

    Covers a missing ``access_token`` or a malformed body. The message carries
    no token material.
    """


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenState:
    """Immutable snapshot of a stored source OAuth token (R4.1).

    Attributes:
        access_token: The current access token value (may be empty when never
            acquired).
        refresh_token: The long-lived refresh token used to mint a new access
            token.
        expires_at: Absolute expiry as an epoch-seconds timestamp. The token is
            considered expired when ``expires_at <= now + skew``.
        scope: The granted scope string (plaintext status; may be empty).
        extra: Provider-specific fields carried alongside the token (for
            example a YouTube ``visitor_data``). Never assumed to contain
            secrets that must be logged.
    """

    access_token: str
    refresh_token: str
    expires_at: float
    scope: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

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


# ---------------------------------------------------------------------------
# Refresh contract
# ---------------------------------------------------------------------------


@runtime_checkable
class RefreshClient(Protocol):
    """The one pure refresh contract every OAuth provider implements (R4.1).

    Implementations exchange a refresh token for a fresh access token via that
    provider's token endpoint (or, for Tidal, the existing first-party logic).
    Kept as a pure protocol so tests and the watchdog can inject deterministic
    fakes with no live network calls.
    """

    provider: str

    def refresh(self, refresh_token: str, now: float) -> TokenState:
        """Exchange ``refresh_token`` for a fresh :class:`TokenState`."""
        ...


# ---------------------------------------------------------------------------
# Shared decision/derivation
# ---------------------------------------------------------------------------


def needs_refresh(
    state: TokenState,
    now: float,
    skew: float = DEFAULT_EXPIRY_SKEW_SECONDS,
) -> bool:
    """Return whether ``state`` needs a refresh at ``now`` under ``skew``.

    A thin, side-effect-free predicate the watchdog uses when enumerating
    near-expiry credentials (R4.1). Equivalent to ``state.is_expired(now, skew)``.
    """
    return state.is_expired(now, skew)


def apply_refresh(
    state: TokenState,
    client: RefreshClient,
    now: float,
    *,
    skew: float = DEFAULT_EXPIRY_SKEW_SECONDS,
    force: bool = False,
) -> TokenState:
    """Return a non-expired :class:`TokenState`, refreshing via ``client``.

    Mirrors :func:`hellodj_platform_logic.tidal_refresh.refresh_tidal`:

        * **Fast path** — if ``state`` is still valid at ``now`` and ``force``
          is False, the existing token is returned unchanged.
        * **Refresh** — otherwise ``client.refresh`` mints a fresh token.
        * **Preserve refresh token** — when the provider does not rotate the
          refresh token (the minted state has an empty ``refresh_token``), the
          prior refresh token is carried forward so the next cycle can proceed
          (R4.3).
        * **Expired-result-is-failure** — if the minted token is already expired
          relative to ``now`` it is treated as a failed refresh (R4.4).

    Args:
        state: The current token snapshot.
        client: The injected provider refresh client.
        now: Current time as an epoch-seconds timestamp.
        skew: Expiry skew used when deciding whether to refresh.
        force: When True, always refresh even if the current token is valid.

    Returns:
        A :class:`TokenState` that is non-expired at ``now`` and carries a
        non-empty refresh token for the next cycle.

    Raises:
        RefreshFailedError: If the client returns an already-expired token
            (R4.4).
    """
    # Fast path: a still-valid token needs no refresh unless forced.
    if not force and not state.is_expired(now, skew):
        return state

    refreshed = client.refresh(state.refresh_token, now)

    # A successful refresh must produce a non-expired token (R4.4).
    if refreshed.is_expired(now, skew):
        raise RefreshFailedError(
            f"{client.provider} refresh returned an already-expired token"
        )

    # Preserve the prior refresh token when the provider does not rotate it, so
    # the next refresh cycle can proceed (R4.3).
    if not refreshed.refresh_token:
        refreshed = replace(refreshed, refresh_token=state.refresh_token)

    return refreshed


# ---------------------------------------------------------------------------
# HTTP form-post (injectable)
# ---------------------------------------------------------------------------

#: An injectable HTTP form poster: ``(url, form_fields) -> parsed JSON dict``.
#: The Google/Spotify clients depend on this so they can be unit-tested without
#: network. The default implementation uses :mod:`urllib`.
HttpFormPost = Callable[[str, Mapping[str, str]], Mapping[str, Any]]


def urllib_form_post(url: str, fields: Mapping[str, str]) -> Mapping[str, Any]:
    """POST ``fields`` as ``application/x-www-form-urlencoded`` to ``url``.

    Parses and returns the JSON response body as a mapping. This is the default
    :data:`HttpFormPost` used by the Google/Spotify clients when no poster is
    injected. Kept minimal (no third-party deps) so the module stays importable
    from every component.

    Raises:
        ProviderTokenError: If the response body is not valid JSON.
    """
    data = urllib.parse.urlencode(dict(fields)).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - fixed https token endpoints
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request) as response:  # noqa: S310 - https only
        raw = response.read().decode("utf-8")
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ProviderTokenError("provider token endpoint returned non-JSON") from exc
    if not isinstance(parsed, dict):
        raise ProviderTokenError("provider token endpoint returned a non-object")
    return parsed


def _token_state_from_oauth_response(
    response: Mapping[str, Any],
    *,
    prior_refresh_token: str,
    now: float,
) -> TokenState:
    """Build a :class:`TokenState` from a standard OAuth token response.

    Reads ``access_token``, optional rotated ``refresh_token`` (falls back to
    ``prior_refresh_token`` when the provider does not rotate — R4.3),
    ``expires_in`` (seconds from ``now``), and ``scope``. All other keys are
    preserved in :attr:`TokenState.extra`.

    Raises:
        ProviderTokenError: If the response has no ``access_token``.
    """
    access_token = response.get("access_token")
    if not access_token or not isinstance(access_token, str):
        raise ProviderTokenError("provider token response missing access_token")

    rotated = response.get("refresh_token")
    refresh_token = rotated if isinstance(rotated, str) and rotated else ""

    expires_in = response.get("expires_in", 0)
    try:
        ttl = float(expires_in)
    except (TypeError, ValueError):
        ttl = 0.0
    expires_at = now + ttl

    scope = response.get("scope", "")
    if not isinstance(scope, str):
        scope = ""

    reserved = {"access_token", "refresh_token", "expires_in", "scope"}
    extra = {k: v for k, v in response.items() if k not in reserved}

    return TokenState(
        access_token=access_token,
        refresh_token=refresh_token or prior_refresh_token,
        expires_at=expires_at,
        scope=scope,
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Concrete clients
# ---------------------------------------------------------------------------


@dataclass
class GoogleRefreshClient:
    """Refresh client for Google-backed providers (youtube / youtube_music).

    POSTs a ``grant_type=refresh_token`` form to
    :data:`GOOGLE_TOKEN_URL` with the OAuth ``client_id`` / ``client_secret``
    (R4.2). Google does not rotate the refresh token on this grant, so the prior
    refresh token is preserved by :func:`apply_refresh` / the response builder
    (R4.3). The HTTP form-post is injectable for testing.

    Attributes:
        client_id: The Google OAuth client id.
        client_secret: The Google OAuth client secret.
        provider: ``youtube`` or ``youtube_music`` (both use this client).
        token_url: The token endpoint (overridable for tests).
        http_post: The injected form poster (defaults to :func:`urllib_form_post`).
    """

    client_id: str
    client_secret: str
    provider: str = PROVIDER_YOUTUBE
    token_url: str = GOOGLE_TOKEN_URL
    http_post: HttpFormPost = urllib_form_post

    def refresh(self, refresh_token: str, now: float) -> TokenState:
        """Exchange ``refresh_token`` at Google's token endpoint (R4.2)."""
        response = self.http_post(
            self.token_url,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        return _token_state_from_oauth_response(
            response,
            prior_refresh_token=refresh_token,
            now=now,
        )


@dataclass
class SpotifyRefreshClient:
    """Refresh client for Spotify.

    POSTs a ``grant_type=refresh_token`` form to :data:`SPOTIFY_TOKEN_URL`
    (R4.2). Spotify may or may not rotate the refresh token; when it does not,
    the prior refresh token is preserved (R4.3). The HTTP form-post is
    injectable for testing.

    Attributes:
        client_id: The Spotify OAuth client id.
        client_secret: The Spotify OAuth client secret.
        provider: ``spotify``.
        token_url: The token endpoint (overridable for tests).
        http_post: The injected form poster (defaults to :func:`urllib_form_post`).
    """

    client_id: str
    client_secret: str
    provider: str = PROVIDER_SPOTIFY
    token_url: str = SPOTIFY_TOKEN_URL
    http_post: HttpFormPost = urllib_form_post

    def refresh(self, refresh_token: str, now: float) -> TokenState:
        """Exchange ``refresh_token`` at Spotify's token endpoint (R4.2)."""
        response = self.http_post(
            self.token_url,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        return _token_state_from_oauth_response(
            response,
            prior_refresh_token=refresh_token,
            now=now,
        )


@dataclass
class TidalRefreshClient:
    """Adapter routing Tidal refresh through the EXISTING first-party logic.

    This client does NOT re-implement Tidal refresh. It delegates to
    :func:`hellodj_platform_logic.tidal_refresh.refresh_tidal` with the injected
    first-party client, so the Tidal single-app-id behavior and its property
    tests remain untouched (R4.5, R10.2). It only translates between the unified
    :class:`TokenState` and the Tidal
    :class:`~hellodj_platform_logic.tidal_refresh.TidalTokenState`.

    Because ``refresh_tidal`` owns the expiry decision, this adapter forces a
    refresh (the unified :func:`apply_refresh` fast-path already decided a
    refresh is due before calling here).

    Attributes:
        first_party_client: The existing injectable first-party Tidal client.
        provider: ``tidal``.
    """

    first_party_client: FirstPartyRefreshClient
    provider: str = PROVIDER_TIDAL

    def refresh(self, refresh_token: str, now: float) -> TokenState:
        """Delegate to :func:`refresh_tidal` and adapt the result (R4.5)."""
        prior = TidalTokenState(
            access_token="",
            refresh_token=refresh_token,
            # An expiry at/behind ``now`` makes refresh_tidal treat it as due;
            # ``force=True`` below makes the decision explicit regardless.
            expires_at=now,
        )
        refreshed: TidalTokenState = refresh_tidal(
            prior,
            self.first_party_client,
            now,
            force=True,
        )
        return TokenState(
            access_token=refreshed.access_token,
            refresh_token=refreshed.refresh_token,
            expires_at=refreshed.expires_at,
        )
