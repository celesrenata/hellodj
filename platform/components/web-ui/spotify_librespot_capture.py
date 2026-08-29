"""Orchestrate the one-time librespot reusable-credential capture (Spotify).

Multi-tenant-source-streaming task 2.2. The Spotify data plane
(``spotify-stream``) streams a guild's owning user's account via ``librespot``,
which can only build a per-user ``Session`` from a **reusable-credentials JSON
object** ``{username, credentials, type}`` (task 2.1 spike). That blob is NOT a
standard OAuth token and can only be produced by a one-time interactive login
that opens a real ``librespot`` ``Session`` against a Spotify access point.

librespot is a heavy native dependency that lives in the ``spotify-stream``
sidecar image, NOT in this Flask web-ui (Flask + boto3 only). So the capture is
**performed by the sidecar** (which already has librespot + Spotify egress) and
this module is the web-ui-side **orchestrator**: it drives the sidecar over a
small HTTP contract, relays the browser authorize leg, and hands the resulting
reusable blob to :class:`SourceCredentialService` for storage inside the SAME
envelope-encrypted token blob (``extra.librespot_credentials``) — never a
plaintext column (design "Data Models"; R3.3, R6.4, R10.3).

Sidecar HTTP contract (implemented on the ``spotify-stream`` side in task 2.3;
the web-ui only depends on this shape):

* ``POST {SPOTIFY_STREAM_URL}/auth/librespot/start``
  body ``{"sub": <owner-sub>, "redirect_uri": <web-ui fixed callback>}``
  -> ``{"authorize_url": "https://accounts.spotify.com/authorize?..."}``.
  The sidecar mints the PKCE verifier + authorize URL for its librespot
  keymaster client bound to that ``sub``; the verifier stays server-side in the
  sidecar.
* ``POST {SPOTIFY_STREAM_URL}/auth/librespot/complete``
  body ``{"sub": <owner-sub>, "code": <authorization-code>}``
  -> ``{"credentials": {"username": ..., "credentials": ..., "type":
  "AUTHENTICATION_STORED_SPOTIFY_CREDENTIALS"}}`` on success, or a non-2xx / a
  body without a well-formed ``credentials`` object on failure.

The transport is injected (a ``(url, json_body) -> (status, parsed_json)``
callable) so this module is unit-testable with no live sidecar / network and no
third-party HTTP dependency (mirrors the ``source_token_exchange`` urllib
seams). Token material (the reusable blob) is never logged.

Requirements: 3.3, 6.4, 10.3
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "LIBRESPOT_CREDENTIALS_EXTRA_KEY",
    "LIBRESPOT_CREDENTIALS_KEYS",
    "HttpJsonPost",
    "SpotifyLibrespotCapture",
    "valid_librespot_credentials",
]

#: Key under a Spotify credential's ``TokenState.extra`` that carries the
#: librespot reusable-credentials JSON object (``{username, credentials,
#: type}``). It lives INSIDE the envelope-encrypted blob (never a plaintext
#: column), so the KMS-Decrypt-only reader contract and "tokens never in
#: plaintext" hold. The reader (``UserCredentialResolver``) flattens ``extra``,
#: so ``spotify-stream``'s session factory (task 2.3) sees it as
#: ``tokens["librespot_credentials"]``.
LIBRESPOT_CREDENTIALS_EXTRA_KEY = "librespot_credentials"

#: The required keys of a well-formed librespot reusable-credentials object.
#: librespot writes exactly these (``type`` /``credentials`` may also arrive
#: under the alias keys ``auth_type`` / ``auth_data`` — normalized on read).
LIBRESPOT_CREDENTIALS_KEYS = ("username", "credentials", "type")

_HTTP_TIMEOUT = 15

#: An injectable JSON HTTP poster: ``(url, body) -> (status_code, parsed_json)``.
#: Defaults to a small :mod:`urllib` implementation so the module needs no
#: third-party HTTP dependency and stays importable everywhere.
HttpJsonPost = Callable[[str, Mapping[str, Any]], "tuple[int, dict[str, Any]]"]


def _urllib_json_post(
    url: str, body: Mapping[str, Any], timeout: int = _HTTP_TIMEOUT
) -> tuple[int, dict[str, Any]]:
    """POST a JSON body to ``url``; return ``(status, parsed_json)``.

    Returns ``(status, {})`` on a decode error and ``(0, {})`` on any transport
    error, so a caller never has to catch — it treats a non-2xx / empty body as
    a failed capture and surfaces a clear error (no partial store).
    """
    data = json.dumps(dict(body)).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 - in-cluster sidecar URL only
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8")
            status = int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:  # non-2xx with a body
        try:
            raw = exc.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            raw = ""
        status = int(exc.code)
    except Exception:  # noqa: BLE001 - transport error -> degrade to (0, {})
        return 0, {}
    try:
        parsed = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return status, {}
    return status, (parsed if isinstance(parsed, dict) else {})


def valid_librespot_credentials(obj: Any) -> bool:
    """Return whether ``obj`` is a well-formed librespot reusable-credentials
    object.

    Requires a mapping carrying non-empty ``username``, ``credentials``, and
    ``type`` values (the exact shape ``spotify-stream`` feeds
    ``Session.Builder(...).stored(...)``). Rejects anything partial so a
    malformed capture is never stored as if it were usable.
    """
    if not isinstance(obj, Mapping):
        return False
    for key in LIBRESPOT_CREDENTIALS_KEYS:
        value = obj.get(key)
        if not isinstance(value, str) or not value:
            return False
    return True


class SpotifyLibrespotCapture:
    """Web-ui orchestrator for the sidecar-run librespot credential capture.

    Args:
        base_url: The ``spotify-stream`` sidecar base URL (``SPOTIFY_STREAM_URL``).
        http_post: Injected JSON poster (defaults to a urllib implementation).
    """

    def __init__(
        self,
        base_url: str,
        *,
        http_post: HttpJsonPost = _urllib_json_post,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http_post = http_post

    @property
    def configured(self) -> bool:
        """Whether a sidecar base URL is wired (capture is possible)."""
        return bool(self._base_url)

    def start(self, sub: str, redirect_uri: str) -> str | None:
        """Ask the sidecar to begin a librespot OAuth capture for ``sub``.

        Returns the Spotify authorize URL to surface to the user's browser, or
        ``None`` when the sidecar is unconfigured or returns no usable URL (the
        caller then shows a clear error rather than a dead link).
        """
        if not self._base_url or not sub:
            return None
        status, resp = self._http_post(
            f"{self._base_url}/auth/librespot/start",
            {"sub": sub, "redirect_uri": redirect_uri},
        )
        if status < 200 or status >= 300:
            log.warning("librespot capture start failed (status=%s)", status)
            return None
        url = resp.get("authorize_url")
        if not isinstance(url, str) or not url:
            return None
        return url

    def complete(self, sub: str, code: str) -> dict[str, Any] | None:
        """Forward the browser authorization ``code`` to the sidecar.

        The sidecar completes the librespot login and opens a ``Session`` to
        obtain the reusable-credentials blob. Returns that
        ``{username, credentials, type}`` object on success, or ``None`` when the
        capture failed / returned a malformed blob (so the caller stores nothing
        partial and surfaces a clear error). Token material is never logged.
        """
        if not self._base_url or not sub or not code:
            return None
        status, resp = self._http_post(
            f"{self._base_url}/auth/librespot/complete",
            {"sub": sub, "code": code},
        )
        if status < 200 or status >= 300:
            log.warning("librespot capture complete failed (status=%s)", status)
            return None
        creds = resp.get("credentials")
        if not valid_librespot_credentials(creds):
            log.warning("librespot capture returned no usable credentials")
            return None
        return dict(creds)
