"""YouTube OAuth via the youtube-source plugin's public device-code client.

YouTube playback does NOT use a private Google Cloud "web application" OAuth
client (there is none, and the on-prem deployment never had one). Instead the
`youtube-source` Lavalink plugin authenticates its TV client with a well-known
PUBLIC "TV / limited-input device" OAuth client whose id/secret are baked into
the plugin jar (``YoutubeOauth2Handler.java``). That client uses the OAuth 2.0
**device authorization grant** (RFC 8628-style): the user is shown a short code
and a verification URL, enters the code on any browser, and the poller receives
an offline ``refresh_token`` — with NO redirect URI to pre-register anywhere.

This module replicates that exact flow so the web-ui Account page can connect a
user's YouTube account without the operator registering a Google Cloud web app.
It mirrors the plugin byte-for-byte:

* Public client id/secret + scopes are the SAME constants the plugin uses (they
  are not secrets — they ship in the plugin jar and are reproduced in the bot's
  ``web-ui/app.py`` comment). We own ``youtube-source`` in CodeCommit, so if
  Google ever rotates this public client we update both the plugin and these
  constants together.
* Device-code endpoint: ``POST https://www.youtube.com/o/oauth2/device/code``.
* Token/poll + refresh endpoint: ``POST https://www.youtube.com/o/oauth2/token``.

The refresh token this flow yields MUST be refreshed with the SAME public client
against the SAME ``youtube.com/o/oauth2/token`` endpoint (not the generic
``oauth2.googleapis.com/token``) — see :data:`YOUTUBE_DEVICE_CLIENT_ID` /
:data:`YOUTUBE_DEVICE_TOKEN_URL`, which the durable watchdog's youtube refresh
client reuses so a device-issued token keeps refreshing.

Token material is never logged. The HTTP poster is injectable so the module is
unit-testable with no live network.
"""

from __future__ import annotations

import json
import time
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from typing import Any

__all__ = [
    "YOUTUBE_DEVICE_CLIENT_ID",
    "YOUTUBE_DEVICE_CLIENT_SECRET",
    "YOUTUBE_DEVICE_SCOPES",
    "YOUTUBE_DEVICE_CODE_URL",
    "YOUTUBE_DEVICE_TOKEN_URL",
    "DeviceCodeError",
    "start_device_authorization",
    "poll_device_token",
    "compose_youtube_device_tokens",
]

#: The youtube-source plugin's PUBLIC device client (not a secret — baked into
#: the plugin jar; see ``YoutubeOauth2Handler.java`` in the youtube-source repo
#: and the ``web-ui/app.py`` on-prem comment). Kept in lockstep with the plugin.
YOUTUBE_DEVICE_CLIENT_ID = (
    "861556708454-d6dlm3lh05idd8npek18k6be8ba3oc68.apps.googleusercontent.com"
)
YOUTUBE_DEVICE_CLIENT_SECRET = "SboVhoG9s0rNafixCSGGKXAT"  # noqa: S105 - public plugin client
YOUTUBE_DEVICE_SCOPES = (
    "http://gdata.youtube.com https://www.googleapis.com/auth/youtube"
)

#: Device-grant endpoints the plugin uses (Google's TV/limited-input host).
YOUTUBE_DEVICE_CODE_URL = "https://www.youtube.com/o/oauth2/device/code"
YOUTUBE_DEVICE_TOKEN_URL = "https://www.youtube.com/o/oauth2/token"  # noqa: S105 - URL, not a secret

#: Device-grant type identifier (the legacy OOB device grant the plugin sends).
_DEVICE_GRANT_TYPE = "http://oauth.net/grant_type/device/1.0"

_HTTP_TIMEOUT = 15

#: An injectable JSON-body HTTP poster: ``(url, json_body) -> parsed dict``.
#: Defaults to a small urllib implementation; tests inject a fake.
JsonPost = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


class DeviceCodeError(Exception):
    """Raised when the device-code request fails (never carries token material)."""


def _urllib_json_post(url: str, body: Mapping[str, Any]) -> Mapping[str, Any]:
    """POST ``body`` as JSON to ``url`` and return the parsed JSON response.

    Mirrors the plugin's ``application/json`` request shape. Raises
    :class:`DeviceCodeError` on a transport or decode failure (no token leak).
    """
    data = json.dumps(dict(body)).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - fixed https youtube endpoints
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - https only
            request, timeout=_HTTP_TIMEOUT
        ) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # pragma: no cover - network error path
        # The token endpoint returns 4xx with a JSON {"error": ...} body during
        # polling (authorization_pending / slow_down); surface that body so the
        # poller can act on the error code rather than raising.
        try:
            return json.loads(exc.read().decode("utf-8"))
        except (ValueError, TypeError, OSError):
            raise DeviceCodeError("youtube device endpoint HTTP error") from exc
    except (urllib.error.URLError, OSError) as exc:  # pragma: no cover
        raise DeviceCodeError("youtube device endpoint unreachable") from exc
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise DeviceCodeError("youtube device endpoint returned non-JSON") from exc
    if not isinstance(parsed, dict):
        raise DeviceCodeError("youtube device endpoint returned a non-object")
    return parsed


def start_device_authorization(
    *, http_post: JsonPost = _urllib_json_post
) -> dict[str, Any]:
    """Begin the device-code flow; return the user-facing code + poll params.

    POSTs the plugin's device-code request and returns a dict with
    ``device_code`` (secret handle used to poll — NOT shown to the user),
    ``user_code`` + ``verification_url`` (shown to the user), ``interval``
    (seconds between polls), and ``expires_in`` (seconds until the code dies).

    Raises:
        DeviceCodeError: If the endpoint does not return a device code.
    """
    response = http_post(
        YOUTUBE_DEVICE_CODE_URL,
        {
            "client_id": YOUTUBE_DEVICE_CLIENT_ID,
            "scope": YOUTUBE_DEVICE_SCOPES,
            "device_id": uuid.uuid4().hex,
            "device_model": "ytlr::",
        },
    )
    device_code = str(response.get("device_code", "") or "")
    user_code = str(response.get("user_code", "") or "")
    verification_url = str(
        response.get("verification_url", "")
        or response.get("verification_uri", "")
        or ""
    )
    if not device_code or not user_code or not verification_url:
        raise DeviceCodeError("youtube device-code response was incomplete")
    try:
        interval = int(response.get("interval", 5) or 5)
    except (TypeError, ValueError):
        interval = 5
    try:
        expires_in = int(response.get("expires_in", 1800) or 1800)
    except (TypeError, ValueError):
        expires_in = 1800
    return {
        "device_code": device_code,
        "user_code": user_code,
        "verification_url": verification_url,
        "interval": max(interval, 1),
        "expires_in": expires_in,
    }


def poll_device_token(
    device_code: str, *, http_post: JsonPost = _urllib_json_post
) -> dict[str, Any]:
    """Poll once for the offline refresh token behind ``device_code``.

    Returns one of:

    * ``{"status": "pending"}`` — the user has not finished authorizing yet
      (Google returned ``authorization_pending`` or ``slow_down``); the caller
      should poll again after the interval.
    * ``{"status": "ok", "oauth_refresh_token": "<token>"}`` — authorization
      complete; the offline refresh token is ready to persist.
    * ``{"status": "error", "error": "<code>"}`` — a terminal error
      (``expired_token``, ``access_denied``, or an unexpected code); the caller
      should abandon the flow and surface a clear message.

    Never logs or echoes the refresh token.
    """
    if not device_code:
        return {"status": "error", "error": "missing_device_code"}
    response = http_post(
        YOUTUBE_DEVICE_TOKEN_URL,
        {
            "client_id": YOUTUBE_DEVICE_CLIENT_ID,
            "client_secret": YOUTUBE_DEVICE_CLIENT_SECRET,
            "code": device_code,
            "grant_type": _DEVICE_GRANT_TYPE,
        },
    )
    error = response.get("error")
    if error:
        error = str(error)
        if error in ("authorization_pending", "slow_down"):
            return {"status": "pending"}
        return {"status": "error", "error": error}
    refresh_token = str(response.get("refresh_token", "") or "")
    if not refresh_token:
        # No error and no refresh token — treat as still pending rather than
        # storing something partial.
        return {"status": "pending"}
    return {"status": "ok", "oauth_refresh_token": refresh_token}


#: A PoToken fetcher: ``() -> {"pot_token": ..., "pot_visitor_data": ...}`` or
#: ``{}`` on failure. Injected so this module does not import
#: :mod:`source_token_exchange` (avoids a cycle) and stays unit-testable.
PotokenFetcher = Callable[[], Mapping[str, Any]]


def compose_youtube_device_tokens(
    provider: str,
    oauth_refresh_token: str,
    *,
    connected_by: str,
    fetch_potoken: PotokenFetcher,
) -> dict[str, Any]:
    """Compose the full YouTube credential from a device-flow refresh token.

    The device-code poll already yielded the offline ``oauth_refresh_token``
    (there is no authorization ``code`` to exchange — that is the whole point of
    the device grant). This mirrors ``source_token_exchange.compose_youtube_tokens``
    for the redirect path: it pairs the refresh token with a freshly fetched
    PoToken (+ visitor data) and returns the exact shape the bot's YouTube
    resolver reads (``provider``, ``oauth_refresh_token``, ``pot_token``,
    ``pot_visitor_data``, ``connected_by``, ``connected_at``).

    Returns ``{}`` when either the refresh token or the PoToken is missing, so
    the caller surfaces a clear error instead of persisting a partial secret.
    Never logs token material.
    """
    if not oauth_refresh_token:
        return {}
    pot = fetch_potoken() or {}
    pot_token = str(pot.get("pot_token", "") or "")
    pot_visitor_data = str(pot.get("pot_visitor_data", "") or "")
    if not pot_token or not pot_visitor_data:
        return {}
    return {
        "provider": provider,
        "oauth_refresh_token": oauth_refresh_token,
        "pot_token": pot_token,
        "pot_visitor_data": pot_visitor_data,
        "connected_by": connected_by,
        "connected_at": int(time.time()),
    }
