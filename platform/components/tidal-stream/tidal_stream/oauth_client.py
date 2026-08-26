"""Concrete first-party Tidal OAuth client (single-app-id).

Implements the :class:`hellodj_platform_logic.tidal_refresh.FirstPartyRefreshClient`
protocol used by :func:`hellodj_platform_logic.tidal_refresh.refresh_tidal`. It
performs the single-app-id token exchange and refresh against the Tidal OAuth
token endpoint using the HelloDJ-owned OAuth application (R9.1, R9.2, R9.4).

There is exactly **one** application identifier; the legacy two-client-id
key-split path does not exist here, and the shared refresh logic's guard
rejects it anyway (R9.3). This client never touches Cognito (R9.5).

The HTTP transport is injectable (a callable posting form data and returning a
parsed JSON dict) so the client is unit-testable with no network access. The
default transport is a small :mod:`urllib` poster, keeping the runtime free of
extra dependencies for the synchronous OAuth calls.

Requirements: 9.1, 9.2, 9.4, 9.5, 15.1
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Protocol

from hellodj_platform_logic.tidal_refresh import (
    FirstPartyClientConfig,
    TidalRefreshFailedError,
    TidalTokenState,
    reject_legacy_key_split,
)

__all__ = [
    "FormPoster",
    "FirstPartyTidalOAuthClient",
    "TidalOAuthHTTPError",
    "urllib_form_poster",
]

#: Default connect/read timeout (seconds) for OAuth token calls.
DEFAULT_TIMEOUT_SECONDS = 20.0


class TidalOAuthHTTPError(Exception):
    """Raised when a Tidal OAuth token/refresh request fails at the HTTP level."""


class FormPoster(Protocol):
    """Injectable transport that POSTs form data and returns parsed JSON."""

    def __call__(
        self,
        url: str,
        data: dict[str, str],
        *,
        timeout: float,
    ) -> dict[str, object]:
        """POST ``data`` as form-encoded body to ``url`` and return JSON."""
        ...


def urllib_form_poster(
    url: str,
    data: dict[str, str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Default :class:`FormPoster` using the standard library ``urllib``.

    Raises:
        TidalOAuthHTTPError: On a non-2xx response or a transport failure.
    """
    encoded = urllib.parse.urlencode(data).encode("ascii")
    request = urllib.request.Request(  # noqa: S310 - fixed https token endpoint
        url,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise TidalOAuthHTTPError(
            f"Tidal OAuth request failed (HTTP {error.code}): {detail}"
        ) from error
    except urllib.error.URLError as error:
        raise TidalOAuthHTTPError(f"Tidal OAuth request transport error: {error}") from error

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as error:
        raise TidalOAuthHTTPError(f"Tidal OAuth response was not JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise TidalOAuthHTTPError("Tidal OAuth response JSON must be an object")
    return parsed


def _token_from_payload(payload: dict[str, object], now: float) -> TidalTokenState:
    """Build a :class:`TidalTokenState` from an OAuth token response payload."""
    access_token = str(payload.get("access_token", "") or "")
    if not access_token:
        raise TidalRefreshFailedError("Tidal OAuth response had no access_token")
    refresh_token = str(payload.get("refresh_token", "") or "")
    try:
        expires_in = float(payload.get("expires_in", 0) or 0)
    except (TypeError, ValueError) as error:
        raise TidalRefreshFailedError("Tidal OAuth response had non-numeric expires_in") from error
    return TidalTokenState(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=now + expires_in,
    )


class FirstPartyTidalOAuthClient:
    """First-party single-app-id Tidal OAuth client.

    Args:
        config: The first-party client config (single app id + HelloDJ callback).
            It is validated up front by
            :func:`hellodj_platform_logic.tidal_refresh.reject_legacy_key_split`
            so a legacy key-split config can never construct this client (R9.3).
        token_url: The Tidal OAuth token endpoint.
        poster: Injectable form-POST transport (defaults to a urllib poster).
        timeout: Request timeout in seconds.
    """

    def __init__(
        self,
        config: FirstPartyClientConfig,
        *,
        token_url: str,
        poster: FormPoster | Callable[..., dict[str, object]] = urllib_form_poster,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        reject_legacy_key_split(config)
        if not token_url:
            raise ValueError("token_url is required")
        self._config = config
        self._token_url = token_url
        self._poster = poster
        self._timeout = timeout

    @property
    def config(self) -> FirstPartyClientConfig:
        """The first-party client configuration (single app id + callback)."""
        return self._config

    def exchange_code(self, code: str, now: float) -> TidalTokenState:
        """Exchange an authorization ``code`` for an access+refresh token.

        Used by the HelloDJ-owned ``/auth/callback`` endpoint (R9.2). Uses the
        single application id and callback URL from the config (R9.1).
        """
        if not code:
            raise ValueError("authorization code is required")
        payload = self._poster(
            self._token_url,
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self._config.app_id,
                "redirect_uri": self._config.callback_url,
            },
            timeout=self._timeout,
        )
        return _token_from_payload(payload, now)

    def refresh(self, refresh_token: str, now: float) -> TidalTokenState:
        """Exchange ``refresh_token`` for a fresh token via single-app-id path.

        Satisfies the :class:`FirstPartyRefreshClient` protocol so
        :func:`hellodj_platform_logic.tidal_refresh.refresh_tidal` can drive it
        (R9.4).
        """
        if not refresh_token:
            raise TidalRefreshFailedError("no refresh_token available to refresh")
        payload = self._poster(
            self._token_url,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._config.app_id,
            },
            timeout=self._timeout,
        )
        return _token_from_payload(payload, now)
