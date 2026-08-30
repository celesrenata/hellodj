"""Env-driven bootstrap for the token-refresh watchdog (degraded-safe).

Builds a :class:`~playback_orchestrator.token_watchdog.TokenWatchdog` from the
environment and starts it on a daemon thread next to the health server. Every
piece degrades to ``None`` / no-op when its backing resource is absent, so the
container comes up (health server unaffected) whether or not the credential
store, KMS, and provider OAuth clients are configured (R5.7).

Env:

* ``HELLODJ_CORE_TABLE``               DynamoDB table name (``hellodj-core``).
* ``HELLODJ_SOURCE_CREDS_KMS_KEY_ID``  Source-credentials CMK id/ARN.
* ``AWS_REGION``                       Region for boto3 clients.
* ``TOKEN_WATCHDOG_INTERVAL``          Seconds between ticks (optional).
* ``TOKEN_WATCHDOG_THRESHOLD``         Near-expiry window seconds (optional).
* ``GOOGLE_CLIENT_ID`` / ``GOOGLE_CLIENT_SECRET``    YouTube + YouTube Music.
* ``SPOTIFY_CLIENT_ID`` / ``SPOTIFY_CLIENT_SECRET``  Spotify.
* ``TIDAL_CLIENT_ID`` / ``TIDAL_CLIENT_SECRET``      Tidal (first-party).
* ``POTOKEN_SERVER_URL``               In-cluster potoken-server base URL.
                                       Defaults to
                                       ``http://potoken-server.hellodj-<stage>.svc.cluster.local:4416``
                                       (``<stage>`` from ``HELLODJ_STAGE``), so
                                       the watchdog renews the YouTube PoToken
                                       alongside the OAuth token every tick.
* ``HELLODJ_STAGE``                    Deployment stage (potoken URL default).

Requirements: 5.1, 5.3, 5.4, 5.6, 5.7
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from collections.abc import Mapping
from typing import Any

from hellodj_platform_logic.source_refresh import (
    GoogleRefreshClient,
    RefreshClient,
    SpotifyRefreshClient,
    youtube_device_refresh_client,
)
from hellodj_platform_logic.source_refresh_potoken import PoTokenRefreshClient

from .token_watchdog import (
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_THRESHOLD_SECONDS,
    TokenWatchdog,
)

_LOG = logging.getLogger("playback_orchestrator.watchdog_bootstrap")

__all__ = [
    "build_clients_by_provider",
    "build_watchdog",
    "start_watchdog_thread",
]


_HTTP_TIMEOUT = 15


def _potoken_server_url() -> str:
    """Return the in-cluster potoken-server base URL (env or stage-derived).

    Prefers an explicit ``POTOKEN_SERVER_URL``; otherwise derives the standard
    in-namespace service DNS name from ``HELLODJ_STAGE`` via the shared
    ``cluster_dns`` helper (single source of truth, also used by the web-ui), so
    the watchdog can renew PoTokens with no extra CDK env wiring. Returns ``""``
    only when neither is resolvable.
    """
    from hellodj_platform_logic.cluster_dns import potoken_server_url

    return potoken_server_url(
        os.getenv("HELLODJ_STAGE", ""),
        explicit=os.getenv("POTOKEN_SERVER_URL", ""),
    )


def _build_potoken_fetcher():  # noqa: ANN202 - returns a PoTokenFetcher
    """Build a PoToken fetcher over the potoken-server, or ``None`` if unset.

    The returned callable POSTs ``/get_pot`` and maps the response
    (``poToken`` -> ``pot_token``, ``contentBinding`` -> ``pot_visitor_data``),
    identical to the web-ui's ``fetch_guild_potoken`` and the bot's
    ``fetch_and_push_potoken`` shape. It returns ``{}`` on any failure (server
    down / incomplete) so the PoToken decorator degrades to the prior PoToken.
    Never logs token material.
    """
    base = _potoken_server_url()
    if not base:
        return None
    url = base.rstrip("/") + "/get_pot"

    def _fetch() -> Mapping[str, object]:
        data = json.dumps({}).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310 - in-cluster service URL only
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - fixed in-cluster URL
                req, timeout=_HTTP_TIMEOUT
            ) as resp:
                parsed = json.loads(resp.read().decode("utf-8")) or {}
        except Exception:  # noqa: BLE001 - degrade to empty (keep prior PoToken)
            return {}
        pot_token = str(parsed.get("poToken", "") or "")
        visitor = str(parsed.get("contentBinding", "") or "")
        if not pot_token or not visitor:
            return {}
        return {"pot_token": pot_token, "pot_visitor_data": visitor}

    return _fetch


def _float_env(name: str, default: float) -> float:
    """Return a positive float env value, or ``default`` when unset/invalid."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _core_table() -> Any | None:
    """Build a CoreTable from ``HELLODJ_CORE_TABLE``, or None (degraded)."""
    table_name = os.getenv("HELLODJ_CORE_TABLE", "").strip()
    if not table_name:
        return None
    try:
        import boto3
        from hellodj_platform_logic.data_access import CoreTable

        ddb = boto3.resource(
            "dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1")
        )
        return CoreTable(ddb.Table(table_name))
    except Exception:  # noqa: BLE001 - degrade to no datastore
        return None


def _kms_client() -> Any | None:
    """Build a KMS client, or None when boto3 is unavailable (degraded)."""
    try:
        import boto3

        return boto3.client(
            "kms", region_name=os.getenv("AWS_REGION", "us-east-1")
        )
    except Exception:  # noqa: BLE001
        return None


def build_clients_by_provider() -> dict[str, RefreshClient]:
    """Return the ``{provider: RefreshClient}`` map from env (may be empty).

    Each provider client is included only when its OAuth client id + secret are
    configured, so a partially configured deployment refreshes exactly the
    providers it can. ``youtube`` and ``youtube_music`` both use
    :class:`GoogleRefreshClient`; ``spotify`` uses :class:`SpotifyRefreshClient`.
    Tidal is wired via the first-party adapter only when its first-party client
    can be constructed. ``discord`` is identity-only and never appears here.
    """
    clients: dict[str, RefreshClient] = {}

    google_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    google_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    if google_id and google_secret:
        # An operator-supplied Google web-app client (rare) takes precedence.
        yt_base: RefreshClient = GoogleRefreshClient(
            client_id=google_id, client_secret=google_secret, provider="youtube"
        )
        ytm_base: RefreshClient = GoogleRefreshClient(
            client_id=google_id,
            client_secret=google_secret,
            provider="youtube_music",
        )
    else:
        # Default: YouTube tokens are issued by the youtube-source plugin's
        # PUBLIC device-code client (no operator Google app). Refresh them with
        # that same public client against youtube.com/o/oauth2/token, so
        # device-issued credentials keep renewing with no env configuration.
        yt_base = youtube_device_refresh_client("youtube")
        ytm_base = youtube_device_refresh_client("youtube_music")

    # The YouTube playback credential also carries a short-lived PoToken +
    # visitor data that must stay fresh (R5.3) — the OAuth refresh response has
    # no PoToken, so wrap the base client to regenerate the PoToken from the
    # in-cluster potoken-server on every refresh. When no potoken-server is
    # resolvable the base client is used unwrapped (OAuth-only refresh; the
    # last-known PoToken is preserved by the watchdog).
    potoken_fetcher = _build_potoken_fetcher()
    if potoken_fetcher is not None:
        clients["youtube"] = PoTokenRefreshClient(
            base=yt_base, fetch_potoken=potoken_fetcher, provider="youtube"
        )
        clients["youtube_music"] = PoTokenRefreshClient(
            base=ytm_base,
            fetch_potoken=potoken_fetcher,
            provider="youtube_music",
        )
    else:
        clients["youtube"] = yt_base
        clients["youtube_music"] = ytm_base

    spotify_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    spotify_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    if spotify_id and spotify_secret:
        clients["spotify"] = SpotifyRefreshClient(
            client_id=spotify_id, client_secret=spotify_secret
        )

    tidal = _tidal_client()
    if tidal is not None:
        clients["tidal"] = tidal

    return clients


def _tidal_client() -> RefreshClient | None:
    """Build the Tidal first-party refresh adapter, or None when unconfigured.

    Delegates to the EXISTING first-party single-app-id Tidal logic (no
    re-implementation), so Tidal's behavior and its property tests are untouched
    (R4.5, R10.2): it wraps the concrete
    :class:`tidal_stream.oauth_client.FirstPartyTidalOAuthClient` in the shared
    :class:`~hellodj_platform_logic.source_refresh.TidalRefreshClient` adapter.

    Returns ``None`` unless the first-party app id + token URL are configured
    AND the concrete client is importable (the ``tidal-stream`` component owns
    it). A degraded deployment simply refreshes the other providers; Tidal is
    skipped rather than failing the watchdog.
    """
    app_id = os.getenv("TIDAL_CLIENT_ID", "").strip()
    token_url = os.getenv("TIDAL_TOKEN_URL", "").strip()
    callback_url = os.getenv("TIDAL_CALLBACK_URL", "").strip()
    if not (app_id and token_url):
        return None
    try:
        from hellodj_platform_logic.source_refresh import TidalRefreshClient
        from hellodj_platform_logic.tidal_refresh import FirstPartyClientConfig
        from tidal_stream.oauth_client import FirstPartyTidalOAuthClient

        config = FirstPartyClientConfig(
            app_id=app_id,
            callback_url=callback_url,
        )
        first_party = FirstPartyTidalOAuthClient(config, token_url=token_url)
        return TidalRefreshClient(first_party_client=first_party)
    except Exception:  # noqa: BLE001 - degrade: skip tidal refresh
        _LOG.debug("token watchdog: tidal first-party client unavailable")
        return None


def build_watchdog() -> TokenWatchdog | None:
    """Build a :class:`TokenWatchdog` from env, or None in degraded mode (R5.7).

    Returns ``None`` (so nothing starts) unless the datastore, KMS, the CMK id,
    AND at least one provider refresh client are all configured. Any of these
    missing means the watchdog cannot do useful work, so it stays disabled
    rather than failing the container.
    """
    core = _core_table()
    kms = _kms_client()
    kms_key_id = os.getenv("HELLODJ_SOURCE_CREDS_KMS_KEY_ID", "").strip()
    clients = build_clients_by_provider()

    if core is None or kms is None or not kms_key_id or not clients:
        return None

    try:
        from source_credential_service import SourceCredentialService
    except Exception:  # noqa: BLE001 - shared service not importable → degrade
        _LOG.debug("token watchdog: SourceCredentialService unavailable")
        return None

    service = SourceCredentialService(core, kms, kms_key_id)
    return TokenWatchdog(
        service,
        clients,
        interval=_float_env("TOKEN_WATCHDOG_INTERVAL", DEFAULT_INTERVAL_SECONDS),
        threshold=_float_env(
            "TOKEN_WATCHDOG_THRESHOLD", DEFAULT_THRESHOLD_SECONDS
        ),
    )


def start_watchdog_thread() -> threading.Thread | None:
    """Start the watchdog on a daemon thread, or log degraded + return None.

    Called from ``__main__.main`` next to the health server. When the watchdog
    cannot be built (degraded mode) it logs a single "degraded: watchdog
    disabled" line and returns ``None`` so the health server still runs (R5.7).
    The thread is a daemon so it never blocks container shutdown.
    """
    watchdog = build_watchdog()
    if watchdog is None:
        _LOG.info("degraded: watchdog disabled (no datastore/KMS/clients)")
        return None
    thread = threading.Thread(
        target=watchdog.run_forever,
        name="token-watchdog",
        daemon=True,
    )
    thread.start()
    _LOG.info("token watchdog thread started")
    return thread
