"""Tidal token manager: load, refresh (first-party), and persist.

Ties together the AWS Secrets Manager refresh-token store, the concrete
first-party single-app-id OAuth client, and the shared
:func:`hellodj_platform_logic.tidal_refresh.refresh_tidal` decision logic. Every
refresh routes through the shared function, whose guard rejects the legacy
two-client-id key-split path (R9.3), so the legacy approach cannot be used here.

Responsibilities:
    * Load the current token from Secrets Manager (R9.2).
    * Return a non-expired access token, refreshing via the first-party path
      when expired (R9.4).
    * Persist any newly minted refresh/access token back to Secrets Manager so
      the long-lived credential survives restarts.
    * Handle the OAuth authorization-code exchange initiated by the
      HelloDJ-owned ``/auth/callback`` endpoint (R9.2).

Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 15.1
"""

from __future__ import annotations

import threading

from hellodj_platform_logic.tidal_refresh import (
    TidalTokenState,
    refresh_tidal,
)

from .oauth_client import FirstPartyTidalOAuthClient
from .secrets import StoredTidalToken, TidalRefreshTokenStore

__all__ = ["TidalTokenManager"]


class TidalTokenManager:
    """Manages the Tidal access/refresh token lifecycle.

    Args:
        store: The Secrets Manager refresh-token store.
        client: The concrete first-party single-app-id OAuth client.
        clock: Callable returning the current epoch-seconds time (injectable
            for tests).
        expiry_skew_seconds: Skew applied when deciding token expiry.
    """

    def __init__(
        self,
        store: TidalRefreshTokenStore,
        client: FirstPartyTidalOAuthClient,
        *,
        clock,
        expiry_skew_seconds: float = 60.0,
    ) -> None:
        self._store = store
        self._client = client
        self._clock = clock
        self._skew = expiry_skew_seconds
        self._lock = threading.Lock()
        self._cached: TidalTokenState | None = None

    def _to_token_state(self, stored: StoredTidalToken) -> TidalTokenState:
        """Adapt a stored token payload into a shared :class:`TidalTokenState`."""
        return TidalTokenState(
            access_token=stored.access_token,
            refresh_token=stored.refresh_token,
            expires_at=stored.expires_at,
        )

    def _persist(self, token: TidalTokenState) -> None:
        """Persist the token back to Secrets Manager."""
        self._store.store(
            StoredTidalToken(
                access_token=token.access_token,
                refresh_token=token.refresh_token,
                expires_at=token.expires_at,
            )
        )

    def get_access_token(self, *, force: bool = False) -> str:
        """Return a non-expired access token, refreshing if needed (R9.4).

        The refresh is performed through the shared first-party decision logic;
        the legacy key-split path is rejected by that logic's guard (R9.3).
        Newly minted tokens are persisted to Secrets Manager.
        """
        with self._lock:
            now = float(self._clock())
            current = self._cached
            if current is None:
                current = self._to_token_state(self._store.load())

            refreshed = refresh_tidal(
                current,
                self._client,
                now,
                skew_seconds=self._skew,
                force=force,
            )

            if refreshed is not current:
                self._persist(refreshed)
            self._cached = refreshed
            return refreshed.access_token

    def complete_authorization(self, code: str) -> TidalTokenState:
        """Exchange an authorization ``code`` and persist the result (R9.2).

        Called by the HelloDJ-owned ``/auth/callback`` endpoint after the
        web-ui forwards the Tidal authorization code.
        """
        with self._lock:
            now = float(self._clock())
            token = self._client.exchange_code(code, now)
            self._persist(token)
            self._cached = token
            return token
