"""Per-guild source OAuth URL construction and callback token extraction.

Extracted from :mod:`auth` to keep each module within the 500-line ceiling.
Builds the provider authorize URLs for a per-guild source connect (Spotify,
Tidal, YouTube / YouTube Music via Google) and extracts the tokens/code to
persist on callback. Client ids come from app config (resolved from Secrets
Manager at startup). The full code→token exchange for each provider is
completed by that provider's streaming sidecar (which owns the client secret)
against the guild's isolated Per_Guild_Secret.

Requirements: 5.1, 5.4, 6.2
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from flask import current_app, request, url_for

__all__ = [
    "source_authorize_url",
    "source_authorize_url_account",
    "source_provider_configured",
    "redirect_uri_for_source",
    "redirect_uri_for",
    "account_callback_endpoint",
    "source_tokens_from_request",
]

#: The single fixed callback endpoint every source-OAuth provider redirects
#: back to for a per-account (B2) connect. The provider rides in the URL path
#: segment (``/auth/oauth/<provider>/callback``) — which is FIXED per stage
#: host — while the connecting user's identity rides in the OAuth ``state``,
#: NOT the path. This yields exactly one registered redirect URI per provider
#: per stage (mirroring the Discord ``/auth/discord/callback`` convention), so
#: adding users or guilds never touches a provider's redirect-URI allowlist.
_ACCOUNT_CALLBACK_ENDPOINT = "auth.source_oauth_callback"


def account_callback_endpoint() -> str:
    """Return the Flask endpoint name of the fixed per-account source callback."""
    return _ACCOUNT_CALLBACK_ENDPOINT

_SPOTIFY_AUTHORIZE = "https://accounts.spotify.com/authorize"
_GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
_TIDAL_AUTHORIZE = "https://login.tidal.com/authorize"

#: Which app-config client-id key gates each provider's interactive OAuth. A
#: provider is "configured" (offerable as connectable) when its client id is
#: present in ``current_app.config``. YouTube / YouTube Music share the Google
#: client id.
_PROVIDER_CLIENT_ID_KEY = {
    "spotify": "SPOTIFY_CLIENT_ID",
    "youtube": "GOOGLE_CLIENT_ID",
    "youtube_music": "GOOGLE_CLIENT_ID",
    "tidal": "TIDAL_CLIENT_ID",
}


def source_provider_configured(provider: str) -> bool:
    """Return whether ``provider`` has the client id needed to start OAuth.

    Mirrors the client-id checks in :func:`source_authorize_url`: a provider is
    configured when the relevant client id is present in ``current_app.config``.
    Unknown providers are never configured.
    """
    key = _PROVIDER_CLIENT_ID_KEY.get(provider)
    if key is None:
        return False
    return bool(current_app.config.get(key, ""))


def redirect_uri_for_source(provider: str, guild_id: str) -> str:
    """Return the absolute per-guild source OAuth callback URI."""
    base = current_app.config.get("PUBLIC_BASE_URL", "").rstrip("/")
    path = url_for("auth.source_callback", guild_id=guild_id, provider=provider)
    if base:
        return base + path
    return url_for(
        "auth.source_callback",
        guild_id=guild_id,
        provider=provider,
        _external=True,
    )


def redirect_uri_for(provider: str) -> str:
    """Return the absolute FIXED per-account source OAuth callback URI (B2).

    Unlike :func:`redirect_uri_for_source`, this carries NO guild id in the
    path — it is the single stable URI (``<base>/auth/oauth/<provider>/callback``)
    registered once per provider per stage host. The connecting user + optional
    guild binding travel in the OAuth ``state`` instead of the URL, so the
    redirect URI never varies per user/guild (the provider allowlist stays
    fixed). ``provider`` is a fixed path segment (spotify/tidal/youtube/
    youtube_music), not dynamic data.
    """
    base = current_app.config.get("PUBLIC_BASE_URL", "").rstrip("/")
    path = url_for(_ACCOUNT_CALLBACK_ENDPOINT, provider=provider)
    if base:
        return base + path
    return url_for(_ACCOUNT_CALLBACK_ENDPOINT, provider=provider, _external=True)


def source_authorize_url_account(provider: str, state: str) -> str | None:
    """Build a provider authorize URL for a per-account connect (fixed callback).

    Identical scope/params to :func:`source_authorize_url` but points every
    provider at the single FIXED :func:`redirect_uri_for` callback (no guild in
    the path). Returns ``None`` when the provider's client id is unconfigured
    (the caller then surfaces a clear "needs setup" error rather than a silent
    no-op).
    """
    redirect_uri = redirect_uri_for(provider)
    return _authorize_url_with_redirect(provider, state, redirect_uri)


def source_authorize_url(provider: str, state: str, guild_id: str) -> str | None:
    """Build the provider OAuth authorize URL, or ``None`` if unconfigured.

    Legacy per-guild variant (guild id in the callback path). Retained for the
    deprecated per-guild connect route; new per-account connects use
    :func:`source_authorize_url_account` with the fixed callback.
    """
    redirect_uri = redirect_uri_for_source(provider, guild_id)
    return _authorize_url_with_redirect(provider, state, redirect_uri)


def _authorize_url_with_redirect(
    provider: str, state: str, redirect_uri: str
) -> str | None:
    """Build a provider authorize URL for a given ``redirect_uri``.

    Shared by the per-account (fixed callback) and legacy per-guild builders so
    the provider scopes/params live in exactly one place. Returns ``None`` when
    the provider's client id is unconfigured or the provider is unknown.
    """
    if provider == "spotify":
        client_id = current_app.config.get("SPOTIFY_CLIENT_ID", "")
        if not client_id:
            return None
        return _SPOTIFY_AUTHORIZE + "?" + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": "user-read-playback-state streaming",
                "state": state,
            }
        )
    if provider in ("youtube", "youtube_music"):
        client_id = current_app.config.get("GOOGLE_CLIENT_ID", "")
        if not client_id:
            return None
        # Playback needs an OFFLINE refresh token for the guild's own YouTube
        # account, so request the full youtube scope with access_type=offline
        # and prompt=consent (Google only reliably returns a refresh token when
        # consent is re-prompted). The refresh token is exchanged server-side
        # by :mod:`source_token_exchange` on callback.
        return _GOOGLE_AUTHORIZE + "?" + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": "https://www.googleapis.com/auth/youtube",
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
        )
    if provider == "tidal":
        client_id = current_app.config.get("TIDAL_CLIENT_ID", "")
        if not client_id:
            return None
        return _TIDAL_AUTHORIZE + "?" + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": "r_usr",
                "state": state,
            }
        )
    return None


def source_tokens_from_request(provider: str) -> dict[str, Any]:
    """Extract the tokens/code to persist from the provider callback request.

    Captures the authorization code; the provider's streaming sidecar completes
    the code→token exchange (it owns the client secret) against the guild's
    isolated Per_Guild_Secret.
    """
    code = request.args.get("code", "")
    if not code:
        return {}
    return {"provider": provider, "authorization_code": code}
