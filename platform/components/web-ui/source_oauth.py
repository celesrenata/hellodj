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
    "redirect_uri_for_source",
    "source_tokens_from_request",
]

_SPOTIFY_AUTHORIZE = "https://accounts.spotify.com/authorize"
_GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
_TIDAL_AUTHORIZE = "https://login.tidal.com/authorize"


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


def source_authorize_url(provider: str, state: str, guild_id: str) -> str | None:
    """Build the provider OAuth authorize URL, or ``None`` if unconfigured."""
    redirect_uri = redirect_uri_for_source(provider, guild_id)
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
        return _GOOGLE_AUTHORIZE + "?" + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": "https://www.googleapis.com/auth/youtube.readonly",
                "access_type": "offline",
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
