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
from source_token_exchange import compose_youtube_tokens, source_exchange_spotify

__all__ = ["ACCOUNT_SOURCE_PROVIDERS", "register_source_oauth_routes"]

#: Providers offered for a per-account source OAuth connect via the single
#: fixed callback (B2). SoundCloud is intentionally absent (search-only, no
#: OAuth). Mirrors ``guild_routes.OAUTH_SOURCE_PROVIDERS``.
ACCOUNT_SOURCE_PROVIDERS = ("youtube", "youtube_music", "spotify", "tidal")


def _new_state() -> str:
    """Return a URL-safe random CSRF/state token."""
    return pysecrets.token_urlsafe(32)


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
