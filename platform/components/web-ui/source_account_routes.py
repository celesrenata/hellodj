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
    persist_spotify_credential,
    persist_tidal_status,
    persist_youtube_credential,
)
from source_oauth import source_authorize_url_account
from source_token_exchange import (
    compose_youtube_tokens,
    fetch_guild_potoken,
    source_exchange_spotify,
)
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
        session["yt_device_interval"] = flow["interval"]
        return render_template(
            "partials/account_youtube_device.html",
            provider=provider,
            user_code=flow["user_code"],
            verification_url=flow["verification_url"],
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
                interval=int(session.get("yt_device_interval", 5) or 5),
                polling=True,
            )
        # Terminal outcomes clear the transient device state from the session.
        for key in (
            "yt_device_code",
            "yt_device_provider",
            "yt_device_user_code",
            "yt_device_verification_url",
            "yt_device_interval",
        ):
            session.pop(key, None)
        if status != "ok":
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
            return _render_account_sources(
                error="youtube_connect_failed", provider=provider
            )
        persist_youtube_credential(source_creds, sub, provider, tokens)
        return _render_account_sources(connected=provider)
