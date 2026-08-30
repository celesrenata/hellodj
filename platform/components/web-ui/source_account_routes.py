"""Per-account source OAuth routes: ONE fixed callback per provider (B2).

Extracted from :mod:`auth` to keep that module within the 500-line ceiling
(mirrors :mod:`auth_forms` / :mod:`source_token_exchange`).

Design (B2, guild binding carried elsewhere): source OAuth is a per-ACCOUNT
action. The connecting user authorizes each provider ONCE; the credential is
stored in the encrypted per-user store (:class:`SourceCredentialService`) keyed
by the session ``sub``. A guild "uses" a source by binding to a managing user's
connected credential (see the guild routes), so no token is ever duplicated
per guild.

Why a single fixed callback: Google / Spotify / Tidal require every OAuth
``redirect_uri`` to be pre-registered in the provider console. Embedding a
guild id (or user id) in the callback PATH would need a new registered URI per
guild — impossible at scale. Instead every provider redirects back to the one
FIXED path ``/auth/oauth/<provider>/callback`` (``<provider>`` is a fixed path
segment, not dynamic data), registered once per provider per stage host
(mirroring the Discord ``/auth/discord/callback`` convention). The connecting
user's identity rides in the OAuth ``state`` (kept in the signed session), NOT
the URL. Adding users or guilds never touches a provider's redirect allowlist.

Requirements: 1.3, 1.4, 1.5, 1.6, 2.1
"""

from __future__ import annotations

import logging
import secrets as pysecrets

from flask import (
    Blueprint,
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from source_credential_store import (
    persist_librespot_credentials,
    persist_spotify_credential,
    persist_tidal_status,
    persist_youtube_credential,
)
from source_oauth import (
    redirect_uri_for_librespot,
    source_authorize_url_account,
)
from source_token_exchange import (
    compose_youtube_tokens,
    fetch_guild_potoken,
    source_exchange_spotify,
)
from spotify_librespot_capture import SpotifyLibrespotCapture
from youtube_device_oauth import (
    DeviceCodeError,
    compose_youtube_device_tokens,
    poll_device_token,
    start_device_authorization,
)

#: Providers that authenticate via the youtube-source plugin's PUBLIC
#: device-code client (no operator Google Cloud app). They use the device
#: connect UI instead of a browser redirect.
_DEVICE_PROVIDERS = ("youtube", "youtube_music")

_log = logging.getLogger("hellodj.web.source_oauth")

__all__ = ["ACCOUNT_SOURCE_PROVIDERS", "register_source_oauth_routes"]

#: Providers offered for a per-account source OAuth connect via the single
#: fixed callback (B2). SoundCloud is intentionally absent (search-only, no
#: OAuth). Mirrors ``guild_routes.OAUTH_SOURCE_PROVIDERS``.
ACCOUNT_SOURCE_PROVIDERS = ("youtube", "youtube_music", "spotify", "tidal")


def _new_state() -> str:
    """Return a URL-safe random CSRF/state token."""
    return pysecrets.token_urlsafe(32)


def _render_account_sources(*, connected: str = "", error: str = "", provider: str = ""):
    """Render the account connections partial after a device-flow outcome.

    Mirrors the context ``guild.account_disconnect_source`` builds so an HTMX
    swap of ``#account-source-list`` reflects the new connection state. The
    status/config helpers live in :mod:`guild_routes`; they are imported lazily
    here to avoid an import cycle (``guild_routes`` builds the blueprint that
    ``auth`` — which imports this module — is registered alongside). ``connected``
    / ``error`` are surfaced so the partial can show a success or failure notice.
    """
    from guild_routes import (  # local import breaks the cycle
        OAUTH_SOURCE_PROVIDERS,
        _account_providers_configured,
        _account_source_status,
    )

    sub = (session.get("user") or {}).get("sub", "")
    return render_template(
        "partials/account_source_list.html",
        providers=OAUTH_SOURCE_PROVIDERS,
        source_status=_account_source_status(sub),
        providers_configured=_account_providers_configured(),
        connected=connected,
        error=error,
        error_provider=provider,
    )


def register_source_oauth_routes(bp: Blueprint) -> None:
    """Register the per-account source OAuth connect + fixed callback on ``bp``.

    Adds ``/oauth/<provider>/connect`` and the single fixed
    ``/oauth/<provider>/callback`` to the auth blueprint (url_prefix ``/auth``).
    """

    @bp.route("/oauth/<provider>/connect")
    def source_oauth_connect(provider: str):  # type: ignore[unused-ignore]
        """Start a per-account source OAuth flow keyed to the logged-in user.

        Mints a CSRF ``state`` and stashes ``{state, provider}`` in the signed
        session, then redirects to the provider with the SINGLE FIXED
        ``redirect_uri`` (``/auth/oauth/<provider>/callback``) — the connecting
        user rides in ``state``, never the URL, so one registered redirect URI
        per provider per stage serves every user (B2). An unknown/unconfigured
        provider surfaces a clear error on the account page rather than a silent
        no-op.
        """
        if not session.get("user"):
            return redirect(url_for("pages.login"))
        if provider not in ACCOUNT_SOURCE_PROVIDERS:
            return redirect(url_for("guild.account", error="unknown_provider"))
        # YouTube / YouTube Music use the plugin's PUBLIC device-code client
        # (no redirect URI, no operator Google app): start the device flow and
        # render the user code + verification URL instead of redirecting.
        if provider in _DEVICE_PROVIDERS:
            return _start_youtube_device(provider)
        state = _new_state()
        session["source_oauth_state"] = state
        session["source_oauth_provider"] = provider
        authorize_url = source_authorize_url_account(provider, state)
        if not authorize_url:
            return redirect(
                url_for(
                    "guild.account",
                    error="provider_not_configured",
                    provider=provider,
                )
            )
        return redirect(authorize_url)

    @bp.route("/oauth/<provider>/callback")
    def source_oauth_callback(provider: str):  # type: ignore[unused-ignore]
        """The single fixed per-account source OAuth callback (B2).

        Validates ``state`` against the session (CSRF, R1.5), confirms it
        matches the provider the flow started for, exchanges the code, and
        stores the credential in the encrypted per-user store keyed by the
        session ``sub``. Any failure stores nothing partial and surfaces a
        clear ``<provider>_connect_failed`` error on the account page.
        """
        if not session.get("user"):
            return redirect(url_for("pages.login"))
        state = request.args.get("state", "")
        expected = session.pop("source_oauth_state", None)
        started_provider = session.pop("source_oauth_provider", None)
        if not state or state != expected or provider != started_provider:
            return redirect(url_for("guild.account", error="state_mismatch"))
        sub = (session.get("user") or {}).get("sub", "")
        source_creds = current_app.extensions.get("source_credentials")
        code = request.args.get("code", "")
        if provider in ("youtube", "youtube_music"):
            # guild_id="" selects the FIXED callback redirect_uri in the
            # exchange so it matches the one used to obtain the code.
            tokens = compose_youtube_tokens(
                provider, code, "", connected_by=sub
            )
            if not tokens:
                return redirect(
                    url_for(
                        "guild.account",
                        error="youtube_connect_failed",
                        provider=provider,
                    )
                )
            persist_youtube_credential(source_creds, sub, provider, tokens)
        elif provider == "spotify":
            tokens = source_exchange_spotify(code, "")
            if not tokens:
                return redirect(
                    url_for(
                        "guild.account",
                        error="spotify_connect_failed",
                        provider="spotify",
                    )
                )
            persist_spotify_credential(source_creds, sub, tokens)
            # The standard OAuth token above serves the Web API, but the
            # spotify-stream data plane streams via librespot, which needs a
            # separate one-time reusable credential (task 2.2). Chain into the
            # sidecar-run librespot capture when a sidecar is wired; otherwise
            # fall through to the normal connected redirect (degraded / tests).
            librespot_step = _start_spotify_librespot(sub)
            if librespot_step is not None:
                return librespot_step
        elif provider == "tidal":
            # Tidal's token lifecycle is owned by the tidal-stream sidecar; the
            # web-ui records only the connection STATUS (no token blob) and
            # forwards the callback to the sidecar for the exchange/refresh.
            persist_tidal_status(source_creds, sub)
            tidal_stream_url = current_app.config.get("TIDAL_STREAM_URL", "")
            if tidal_stream_url:
                query = request.query_string.decode()
                forwarded = (
                    f"{tidal_stream_url.rstrip('/')}/auth/callback?{query}"
                )
                return redirect(forwarded)
        return redirect(url_for("guild.account", connected=provider))

    def _start_youtube_device(provider: str):
        """Begin the YouTube device-code flow and render the code partial.

        Stashes the ``device_code`` + provider in the signed session (the
        ``device_code`` is a secret poll handle, kept server-side, never shown),
        and renders the ``account_youtube_device`` partial with the user-facing
        ``user_code`` + ``verification_url`` and an HTMX poller. On a device-code
        failure the account page shows a clear error rather than a broken screen.
        """
        try:
            flow = start_device_authorization()
        except DeviceCodeError:
            return redirect(
                url_for(
                    "guild.account",
                    error="youtube_device_failed",
                    provider=provider,
                )
            )
        session["yt_device_code"] = flow["device_code"]
        session["yt_device_provider"] = provider
        # Keep the user-facing fields so a "still pending" poll can re-render
        # the same code/URL (the device_code stays server-side only).
        session["yt_device_user_code"] = flow["user_code"]
        session["yt_device_verification_url"] = flow["verification_url"]
        session["yt_device_verification_url_complete"] = flow.get(
            "verification_url_complete", ""
        )
        session["yt_device_interval"] = flow["interval"]
        return render_template(
            "partials/account_youtube_device.html",
            provider=provider,
            user_code=flow["user_code"],
            verification_url=flow["verification_url"],
            verification_url_complete=flow.get("verification_url_complete", ""),
            interval=flow["interval"],
            polling=True,
        )

    @bp.route("/oauth/youtube/device/poll", methods=["POST"])
    def source_oauth_youtube_device_poll():  # type: ignore[unused-ignore]
        """Poll once for the YouTube device-flow refresh token (HTMX).

        Reads the ``device_code`` + provider from the session, polls Google
        once, and:

        * still pending -> re-renders the device partial (HTMX keeps polling);
        * complete -> pairs the refresh token with a fresh PoToken, persists the
          credential (encrypted, keyed by the session ``sub``), and swaps the
          refreshed connections list in;
        * error -> renders the connections list with a clear error.

        Never echoes token material.
        """
        if not session.get("user"):
            return redirect(url_for("pages.login"))
        provider = session.get("yt_device_provider", "")
        device_code = session.get("yt_device_code", "")
        if provider not in _DEVICE_PROVIDERS or not device_code:
            return _render_account_sources(error="youtube_device_failed")
        result = poll_device_token(device_code)
        status = result.get("status")
        if status == "pending":
            # Keep the poller alive: re-render the device partial unchanged.
            return render_template(
                "partials/account_youtube_device.html",
                provider=provider,
                user_code=session.get("yt_device_user_code", ""),
                verification_url=session.get("yt_device_verification_url", ""),
                verification_url_complete=session.get(
                    "yt_device_verification_url_complete", ""
                ),
                interval=int(session.get("yt_device_interval", 5) or 5),
                polling=True,
            )
        # Terminal outcomes clear the transient device state from the session.
        for key in (
            "yt_device_code",
            "yt_device_provider",
            "yt_device_user_code",
            "yt_device_verification_url",
            "yt_device_verification_url_complete",
            "yt_device_interval",
        ):
            session.pop(key, None)
        if status != "ok":
            _log.warning(
                "youtube device flow terminated for %s: status=%s error=%s",
                provider,
                status,
                result.get("error", ""),
            )
            return _render_account_sources(
                error="youtube_connect_failed", provider=provider
            )
        sub = (session.get("user") or {}).get("sub", "")
        source_creds = current_app.extensions.get("source_credentials")
        tokens = compose_youtube_device_tokens(
            provider,
            str(result.get("oauth_refresh_token", "") or ""),
            connected_by=sub,
            fetch_potoken=fetch_guild_potoken,
        )
        if not tokens:
            # The device grant succeeded (we have a refresh token) but the
            # credential could not be composed — almost always because the
            # PoToken fetch (potoken-server) returned nothing. Log it so this
            # is a diagnosable fact instead of a silent "nothing happened".
            _log.warning(
                "youtube device flow: authorized %s but could not compose "
                "credential (PoToken fetch likely failed); nothing stored",
                provider,
            )
            return _render_account_sources(
                error="youtube_connect_failed", provider=provider
            )
        if source_creds is None:
            _log.error(
                "youtube device flow: authorized %s but no source_credentials "
                "store is wired; nothing stored",
                provider,
            )
            return _render_account_sources(
                error="youtube_connect_failed", provider=provider
            )
        persist_youtube_credential(source_creds, sub, provider, tokens)
        _log.info("youtube device flow connected %s for the account", provider)
        return _render_account_sources(connected=provider)

    def _start_spotify_librespot(sub: str):
        """Begin the sidecar-run librespot capture, or ``None`` if unavailable.

        Asks the ``spotify-stream`` sidecar (which has librespot) to mint a
        librespot OAuth authorize URL bound to ``sub``, using the web-ui's FIXED
        librespot callback as the redirect target. On success it stashes a CSRF
        ``state`` in the session and renders the ``account_spotify_librespot``
        partial with the authorize link. Returns ``None`` when no sidecar is
        wired or the sidecar could not start (the caller then falls through to
        the normal connected redirect — the Web-API credential is still stored).
        """
        capture = _librespot_capture()
        if capture is None or not sub:
            return None
        redirect_uri = redirect_uri_for_librespot()
        authorize_url = capture.start(sub, redirect_uri)
        if not authorize_url:
            return None
        state = _new_state()
        session["librespot_state"] = state
        return render_template(
            "partials/account_spotify_librespot.html",
            authorize_url=authorize_url,
        )

    @bp.route("/oauth/spotify/librespot/callback")
    def source_oauth_spotify_librespot_callback():  # type: ignore[unused-ignore]
        """Fixed callback for the librespot capture: forward code, store blob.

        Validates the CSRF ``state`` against the session (R1.5), forwards the
        authorization ``code`` to the ``spotify-stream`` sidecar to complete the
        librespot login + capture the reusable blob, and attaches that blob to
        the user's Spotify credential inside the SAME envelope-encrypted token
        blob (``extra.librespot_credentials``) — never a plaintext column (task
        2.2, R3.3/R6.4/R10.3). Any failure surfaces a clear
        ``spotify_playback_failed`` error and stores nothing partial; token
        material is never logged.
        """
        if not session.get("user"):
            return redirect(url_for("pages.login"))
        state = request.args.get("state", "")
        expected = session.pop("librespot_state", None)
        if not state or state != expected:
            return redirect(url_for("guild.account", error="state_mismatch"))
        sub = (session.get("user") or {}).get("sub", "")
        code = request.args.get("code", "")
        capture = _librespot_capture()
        creds = capture.complete(sub, code) if capture is not None else None
        source_creds = current_app.extensions.get("source_credentials")
        if not creds or not persist_librespot_credentials(
            source_creds, sub, creds
        ):
            return redirect(
                url_for(
                    "guild.account",
                    error="spotify_playback_failed",
                    provider="spotify",
                )
            )
        return redirect(url_for("guild.account", connected="spotify"))

    def _librespot_capture() -> SpotifyLibrespotCapture | None:
        """Build the librespot capture client from config, or ``None``.

        Prefers an injected client on ``app.extensions['spotify_librespot']``
        (tests), else constructs one from ``SPOTIFY_STREAM_URL``. Returns
        ``None`` in degraded mode (no sidecar wired) so the caller no-ops.
        """
        injected = current_app.extensions.get("spotify_librespot")
        if injected is not None:
            return injected
        base = current_app.config.get("SPOTIFY_STREAM_URL", "") or ""
        if not base:
            return None
        return SpotifyLibrespotCapture(base)
