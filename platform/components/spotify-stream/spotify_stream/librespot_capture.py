"""Sidecar side of the one-time librespot reusable-credential capture (task 2.2).

The web-ui (``spotify_librespot_capture.SpotifyLibrespotCapture``) ORCHESTRATES a
one-time interactive librespot login but does NOT carry the native ``librespot``
dependency; the actual capture runs HERE in the sidecar, which already has
librespot + Spotify egress. This module implements the sidecar side of the HTTP
contract the web-ui depends on (multi-tenant-source-streaming task 2.2/2.3):

* ``POST /auth/librespot/start`` body ``{"sub", "redirect_uri"}`` ->
  ``{"authorize_url": "https://accounts.spotify.com/authorize?..."}``. Mints a
  PKCE verifier + authorize URL for librespot's built-in keymaster client bound
  to ``sub``; the verifier stays SERVER-SIDE (kept in memory keyed by ``sub``).
* ``POST /auth/librespot/complete`` body ``{"sub", "code"}`` ->
  ``{"credentials": {"username", "credentials", "type"}}`` on success; a non-2xx
  / a body without a well-formed ``credentials`` object on failure.

Design note (loopback constraint): librespot's console ``oauth()`` flow spins up
a local ``127.0.0.1:5588`` callback server. That is NOT web-compatible, so this
module drives librespot's lower-level :class:`librespot.oauth.OAuth` directly —
``get_auth_url()`` mints the PKCE challenge + authorize URL (with the web-ui's
fixed ``redirect_uri``), and on ``complete`` ``set_code()`` + ``request_token()``
exchange the browser-returned code for a Spotify token, from which a real
``Session`` is opened to obtain the reusable ``{username, credentials, type}``
blob (task 2.1 spike). If a provider ever makes the fixed web ``redirect_uri``
impractical for the keymaster client, the documented fallback is the
Zeroconf/Spotify-Connect transfer capture — either way the OUTPUT is the same
reusable JSON blob the session factory consumes.

The librespot import is lazy so this module is importable (and the endpoints
unit-testable with an injected capture backend) without the native package.
Token material (the reusable blob) is never logged.

Requirements: 3.3, 6.4, 10.3
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

__all__ = [
    "CaptureBackend",
    "LibrespotCaptureService",
    "LibrespotCaptureError",
    "LibrespotOAuthBackend",
]

log = logging.getLogger(__name__)

#: How long a started capture (its PKCE verifier) is retained awaiting the code.
_CAPTURE_TTL_SECONDS = 600.0


class LibrespotCaptureError(RuntimeError):
    """A librespot capture step failed (non-secret reason). Never logs tokens."""


class CaptureBackend(Protocol):
    """Per-``sub`` librespot capture backend (mint URL / exchange code).

    Injected so :class:`LibrespotCaptureService` is unit-testable without the
    native ``librespot`` package or Spotify egress.
    """

    def authorize_url(self, sub: str, redirect_uri: str) -> str:
        """Mint the authorize URL for ``sub``, retaining the PKCE verifier."""
        ...

    def complete(self, sub: str, code: str) -> dict[str, Any] | None:
        """Exchange ``code`` for the reusable ``{username, credentials, type}``."""
        ...

    def discard(self, sub: str) -> None:
        """Drop any retained state for ``sub`` (TTL sweep / after complete)."""
        ...


class LibrespotCaptureService:
    """Stateful orchestrator for the two-step librespot capture (task 2.2).

    Keeps NO token material: the only server-side state is the per-``sub`` PKCE
    verifier held inside the :class:`CaptureBackend` between ``start`` and
    ``complete``. Thread-safe (aiohttp runs the blocking librespot work in an
    executor thread) and TTL-swept so an abandoned capture does not pin state.
    """

    def __init__(
        self,
        backend: CaptureBackend,
        *,
        ttl_seconds: float = _CAPTURE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._backend = backend
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._started_at: dict[str, float] = {}

    def start(self, sub: str, redirect_uri: str) -> str:
        """Begin a capture for ``sub``; return the Spotify authorize URL.

        Raises:
            LibrespotCaptureError: When the backend cannot mint a URL.
        """
        if not sub or not redirect_uri:
            raise LibrespotCaptureError("missing_sub_or_redirect_uri")
        self._sweep()
        url = self._backend.authorize_url(sub, redirect_uri)
        if not url:
            raise LibrespotCaptureError("authorize_url_unavailable")
        with self._lock:
            self._started_at[sub] = self._clock()
        return url

    def complete(self, sub: str, code: str) -> dict[str, Any]:
        """Complete the capture for ``sub`` with the browser ``code``.

        Returns the reusable ``{username, credentials, type}`` object. Clears the
        retained per-``sub`` verifier on success or failure.

        Raises:
            LibrespotCaptureError: When there is no pending capture for ``sub`` or
                the exchange produced no usable blob.
        """
        if not sub or not code:
            raise LibrespotCaptureError("missing_sub_or_code")
        with self._lock:
            pending = sub in self._started_at
        if not pending:
            raise LibrespotCaptureError("no_pending_capture")
        try:
            creds = self._backend.complete(sub, code)
        finally:
            self._forget(sub)
        if not creds:
            raise LibrespotCaptureError("capture_failed")
        return creds

    def _forget(self, sub: str) -> None:
        with self._lock:
            self._started_at.pop(sub, None)
        self._backend.discard(sub)

    def _sweep(self) -> None:
        """Drop capture state older than the TTL (abandoned flows)."""
        now = self._clock()
        with self._lock:
            stale = [s for s, t in self._started_at.items() if now - t >= self._ttl]
            for s in stale:
                del self._started_at[s]
        for s in stale:
            self._backend.discard(s)


class LibrespotOAuthBackend:
    """Real librespot OAuth capture backend (PKCE, server-side verifier).

    Drives :class:`librespot.oauth.OAuth` directly (not the console
    loopback-server ``flow()``): :meth:`authorize_url` mints the PKCE challenge +
    authorize URL and RETAINS the ``OAuth`` object (which holds the verifier)
    keyed by ``sub``; :meth:`complete` feeds the browser code back, exchanges it
    for a Spotify token, opens a real ``Session`` to obtain the reusable blob,
    and returns ``{username, credentials, type}``.

    ``cache_dir_for(sub)`` scopes the per-user librespot credential cache file so
    the capture writes under that user's directory (R9.3). The native
    ``librespot`` imports are lazy so this backend is only constructed where the
    package is present. Token material is never logged.
    """

    def __init__(self, cache_dir_for: Callable[[str], str]) -> None:
        self._cache_dir_for = cache_dir_for
        self._lock = threading.Lock()
        self._pending: dict[str, Any] = {}  # sub -> librespot OAuth object

    def authorize_url(self, sub: str, redirect_uri: str) -> str:
        """Mint the authorize URL for ``sub`` and retain the PKCE verifier."""
        from librespot.mercury import MercuryRequests
        from librespot.oauth import OAuth

        oauth = OAuth(MercuryRequests.keymaster_client_id, redirect_uri, None)
        url = oauth.get_auth_url()  # generates + stores the code_verifier
        with self._lock:
            self._pending[sub] = oauth
        return url

    def complete(self, sub: str, code: str) -> dict[str, Any] | None:
        """Exchange ``code`` and open a Session to capture the reusable blob."""
        with self._lock:
            oauth = self._pending.get(sub)
        if oauth is None:
            return None
        try:
            oauth.set_code(code)
            oauth.request_token()
            credentials = oauth.get_credentials()
            return self._session_blob(sub, credentials)
        except Exception as exc:  # noqa: BLE001 - capture failure → None, no token log
            log.warning(
                "spotify-stream: librespot capture complete failed (%s)",
                type(exc).__name__,
            )
            return None

    def _session_blob(self, sub: str, credentials: Any) -> dict[str, Any] | None:
        """Open a librespot ``Session`` and read the reusable-credentials JSON."""
        import json
        import os

        from librespot.core import Session

        cache_dir = self._cache_dir_for(sub)
        os.makedirs(cache_dir, exist_ok=True)
        stored_file = os.path.join(cache_dir, "spotify-credentials.json")

        conf = Session.Configuration.Builder()
        conf.set_store_credentials(True)
        conf.set_stored_credential_file(stored_file)

        builder = Session.Builder(conf=conf.build())
        builder.login_credentials = credentials
        session = builder.create()
        try:
            if not session.is_valid():
                return None
            # librespot writes {username, credentials, type} to the stored file
            # on a successful authenticate (store_credentials=True).
            if os.path.isfile(stored_file):
                with open(stored_file, encoding="utf-8") as f:
                    blob = json.load(f)
                if isinstance(blob, dict):
                    return blob
            return None
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - best-effort teardown
                    pass

    def discard(self, sub: str) -> None:
        """Drop the retained OAuth object for ``sub``."""
        with self._lock:
            self._pending.pop(sub, None)
