"""Server-side OAuth code exchange + PoToken fetch for per-guild sources.

Extracted from :mod:`source_oauth` / :mod:`auth` to keep every module within the
500-line ceiling. Unlike Tidal (which has a dedicated streaming sidecar that
owns its client secret and completes the code->token exchange via the
``tidal-stream`` forward path), YouTube and Spotify complete their exchange in
the **web-ui**: it holds ``GOOGLE_CLIENT_SECRET`` and completes the YouTube
``authorization_code`` -> ``refresh_token`` exchange (then attaches a fresh
PoToken (+ visitor data) from the in-cluster potoken-server), and it resolves
the Spotify client id/secret to complete the Spotify ``authorization_code`` ->
``refresh_token`` exchange, mirroring the refresh-token-centric global Spotify
fallback secret shape.

The resulting per-guild secret shape the bot playback path consumes is::

    {
      "provider": "youtube",
      "oauth_refresh_token": "1//0g...",
      "pot_token": "MnQ...",
      "pot_visitor_data": "Cgs...",
      "connected_by": "<cognito-sub>",
      "connected_at": 1730000000
    }

All network access goes through small ``_http_post_*`` seams and a lazy
Secrets Manager resolver so tests can inject fakes without live AWS / Google /
potoken-server calls. Every helper returns ``{}`` (or ``None``) on failure so a
caller never stores a partial secret.

Requirements: 2.3, 2.4
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any

from flask import current_app

__all__ = [
    "source_exchange_google",
    "source_exchange_spotify",
    "fetch_guild_potoken",
    "compose_youtube_tokens",
    "discord_client_credentials",
]

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
_HTTP_TIMEOUT = 10


# ── HTTP seams (monkeypatchable in tests; no `requests` dependency) ─────────


def _http_post_form(url: str, form: dict[str, str], timeout: int = _HTTP_TIMEOUT) -> dict[str, Any]:
    """POST an ``application/x-www-form-urlencoded`` body, return parsed JSON.

    Returns ``{}`` on any transport / decode error so callers never store a
    partial secret.
    """
    data = urllib.parse.urlencode(form).encode("ascii")
    req = urllib.request.Request(  # noqa: S310 - fixed https endpoints only
        url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8")) or {}
    except Exception:  # noqa: BLE001 - degrade to empty; caller stores nothing
        return {}


def _http_post_json(url: str, body: dict[str, Any], timeout: int = _HTTP_TIMEOUT) -> dict[str, Any]:
    """POST a JSON body, return parsed JSON (``{}`` on any failure)."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 - in-cluster service URL only
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8")) or {}
    except Exception:  # noqa: BLE001 - degrade to empty
        return {}


# ── Google client id/secret resolution (env first, then Secrets Manager) ────


def _google_client_credentials() -> tuple[str, str]:
    """Return ``(client_id, client_secret)`` for the Google OAuth exchange.

    Prefers the plain ``GOOGLE_CLIENT_ID`` / ``GOOGLE_CLIENT_SECRET`` env config
    (injected by the workloads-stack). When the secret is absent but a
    ``HELLODJ_GOOGLE_OAUTH_SECRET_ARN`` is configured, resolve the
    ``{client_id, client_secret}`` JSON lazily from Secrets Manager. Returns
    empty strings when neither source yields a secret.
    """
    cfg = current_app.config
    client_id = cfg.get("GOOGLE_CLIENT_ID", "") or ""
    client_secret = cfg.get("GOOGLE_CLIENT_SECRET", "") or ""
    if client_id and client_secret:
        return client_id, client_secret

    arn = cfg.get("HELLODJ_GOOGLE_OAUTH_SECRET_ARN", "") or ""
    if not arn:
        return client_id, client_secret
    resolved = _resolve_google_secret(arn)
    return (
        client_id or str(resolved.get("client_id", "")),
        client_secret or str(resolved.get("client_secret", "")),
    )


def _resolve_google_secret(arn: str) -> dict[str, Any]:
    """Fetch + parse the Google OAuth ``{client_id, client_secret}`` secret.

    Thin alias over :func:`_resolve_secret_json` (both Google and Spotify hold a
    ``{client_id, client_secret}`` JSON secret). Returns ``{}`` on any failure.
    """
    return _resolve_secret_json(arn)


def discord_client_credentials() -> tuple[str, str]:
    """Return ``(client_id, client_secret)`` for the Discord OAuth exchange.

    Prefers the plain ``DISCORD_CLIENT_ID`` / ``DISCORD_CLIENT_SECRET`` env
    config; when the secret is absent but ``HELLODJ_DISCORD_OAUTH_SECRET_ARN``
    is configured, resolves the ``{client_id, client_secret}`` JSON lazily from
    Secrets Manager (mirrors :func:`_google_client_credentials`). This keeps the
    Discord client secret out of the k8s manifest / cloud assembly — only its
    ARN is wired as env. Returns empty strings when neither source yields one.
    """
    cfg = current_app.config
    client_id = cfg.get("DISCORD_CLIENT_ID", "") or ""
    client_secret = cfg.get("DISCORD_CLIENT_SECRET", "") or ""
    if client_id and client_secret:
        return client_id, client_secret

    arn = cfg.get("HELLODJ_DISCORD_OAUTH_SECRET_ARN", "") or ""
    if not arn:
        return client_id, client_secret
    resolved = _resolve_secret_json(arn)
    return (
        client_id or str(resolved.get("client_id", "")),
        client_secret or str(resolved.get("client_secret", "")),
    )


# ── Spotify client id/secret resolution (env first, then Secrets Manager) ───


def _spotify_client_credentials() -> tuple[str, str]:
    """Return ``(client_id, client_secret)`` for the Spotify OAuth exchange.

    Prefers the plain ``SPOTIFY_CLIENT_ID`` / ``SPOTIFY_CLIENT_SECRET`` env
    config; when the secret is absent but ``HELLODJ_SPOTIFY_SECRET_ARN`` is
    configured, resolve the ``{client_id, client_secret}`` JSON lazily from
    Secrets Manager (mirrors :func:`_google_client_credentials`). Returns empty
    strings when neither source yields a secret.
    """
    cfg = current_app.config
    client_id = cfg.get("SPOTIFY_CLIENT_ID", "") or ""
    client_secret = cfg.get("SPOTIFY_CLIENT_SECRET", "") or ""
    if client_id and client_secret:
        return client_id, client_secret

    arn = cfg.get("HELLODJ_SPOTIFY_SECRET_ARN", "") or ""
    if not arn:
        return client_id, client_secret
    resolved = _resolve_secret_json(arn)
    return (
        client_id or str(resolved.get("client_id", "")),
        client_secret or str(resolved.get("client_secret", "")),
    )


def _resolve_secret_json(arn: str) -> dict[str, Any]:
    """Fetch + parse a ``{...}`` JSON secret by ARN (``{}`` on any failure).

    Uses a Secrets Manager client stashed on ``app.extensions['secrets_admin']``
    when present (tests inject a fake); otherwise lazily constructs a boto3
    client. Shared by the Google and Spotify credential resolvers.
    """
    client = current_app.extensions.get("secrets_admin")
    if client is None:
        try:
            import boto3  # noqa: PLC0415 - lazy; keeps import cost off hot path

            client = boto3.client("secretsmanager")
        except Exception:  # noqa: BLE001 - no AWS available (e.g. tests)
            return {}
    try:
        raw = client.get_secret_value(SecretId=arn).get("SecretString", "")
        parsed = json.loads(raw) if raw else {}
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001 - absent/denied secret -> empty
        return {}


# ── Public exchange + potoken helpers ───────────────────────────────────────


def source_exchange_google(code: str, guild_id: str) -> dict[str, Any]:
    """Exchange a Google ``authorization_code`` for an offline refresh token.

    POSTs ``grant_type=authorization_code`` to Google's token endpoint with the
    resolved client id/secret and the per-guild callback ``redirect_uri``.
    Returns ``{"oauth_refresh_token": ...}`` on success, or ``{}`` when the code
    is empty, the client secret is unavailable, or Google returns no refresh
    token (so the caller stores nothing partial).
    """
    if not code:
        return {}
    client_id, client_secret = _google_client_credentials()
    if not client_id or not client_secret:
        return {}
    # The redirect_uri MUST match the one used to obtain the code. For a
    # per-account (B2) connect the code was obtained with the FIXED callback
    # (``guild_id`` is ""), so we use the guild-free ``redirect_uri_for``;
    # otherwise the legacy per-guild callback URI.
    from source_oauth import (  # noqa: PLC0415
        redirect_uri_for,
        redirect_uri_for_source,
    )

    provider = _current_provider_hint()
    redirect_uri = (
        redirect_uri_for(provider)
        if not guild_id
        else redirect_uri_for_source(provider, guild_id)
    )
    resp = _http_post_form(
        _GOOGLE_TOKEN_URL,
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    refresh_token = str(resp.get("refresh_token", "") or "")
    if not refresh_token:
        return {}
    return {"oauth_refresh_token": refresh_token}


def _current_provider_hint() -> str:
    """Best-effort provider for the in-flight callback (defaults to youtube)."""
    from flask import request  # noqa: PLC0415

    view_args = getattr(request, "view_args", None) or {}
    provider = view_args.get("provider")
    if provider in ("youtube", "youtube_music"):
        return str(provider)
    return "youtube"


def source_exchange_spotify(code: str, guild_id: str) -> dict[str, Any]:
    """Exchange a Spotify ``authorization_code`` for a refresh token.

    POSTs ``grant_type=authorization_code`` to Spotify's token endpoint with the
    resolved client id/secret (from ``SPOTIFY_CLIENT_ID`` / the
    ``HELLODJ_SPOTIFY_SECRET_ARN`` secret) and the per-guild callback
    ``redirect_uri``. Returns the refresh-token-centric shape the bot's global
    Spotify fallback also uses::

        {provider, refresh_token, access_token?, expires_at?, scope, obtained_at}

    ``access_token`` / ``expires_at`` are included only when present in the
    response. Returns ``{}`` when the code is empty, the client secret is
    unavailable, or Spotify returns no refresh token (so the caller stores
    nothing partial — mirroring :func:`source_exchange_google`).
    """
    if not code:
        return {}
    client_id, client_secret = _spotify_client_credentials()
    if not client_id or not client_secret:
        return {}
    from source_oauth import (  # noqa: PLC0415
        redirect_uri_for,
        redirect_uri_for_source,
    )

    # Match the redirect_uri used to obtain the code: fixed per-account callback
    # when guild_id is empty (B2), else the legacy per-guild callback.
    redirect_uri = (
        redirect_uri_for("spotify")
        if not guild_id
        else redirect_uri_for_source("spotify", guild_id)
    )
    resp = _http_post_form(
        _SPOTIFY_TOKEN_URL,
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    refresh_token = str(resp.get("refresh_token", "") or "")
    if not refresh_token:
        return {}
    obtained_at = int(time.time())
    tokens: dict[str, Any] = {
        "provider": "spotify",
        "refresh_token": refresh_token,
        "scope": str(resp.get("scope", "") or ""),
        "obtained_at": obtained_at,
    }
    access_token = str(resp.get("access_token", "") or "")
    if access_token:
        tokens["access_token"] = access_token
        expires_in = resp.get("expires_in")
        if isinstance(expires_in, (int, float)):
            tokens["expires_at"] = obtained_at + int(expires_in)
    return tokens


def fetch_guild_potoken() -> dict[str, Any]:
    """Fetch a PoToken (+ visitor data) from the in-cluster potoken-server.

    POSTs ``/get_pot`` to ``POTOKEN_SERVER_URL`` and maps the response
    (``poToken`` -> ``pot_token``, ``contentBinding`` -> ``pot_visitor_data``),
    identical to the bot's ``fetch_and_push_potoken`` shape. Returns ``{}`` on
    any failure (server down, missing fields) so no partial secret is stored.
    """
    base = current_app.config.get("POTOKEN_SERVER_URL", "") or ""
    if not base:
        return {}
    url = base.rstrip("/") + "/get_pot"
    resp = _http_post_json(url, {})
    pot_token = str(resp.get("poToken", "") or "")
    visitor_data = str(resp.get("contentBinding", "") or "")
    if not pot_token or not visitor_data:
        return {}
    return {"pot_token": pot_token, "pot_visitor_data": visitor_data}


def compose_youtube_tokens(
    provider: str,
    code: str,
    guild_id: str,
    *,
    connected_by: str,
) -> dict[str, Any]:
    """Compose the full per-guild YouTube secret, or ``{}`` if incomplete.

    Runs the code->refresh-token exchange then fetches a PoToken; only when BOTH
    yield their required fields does it return the complete shape the bot
    resolver reads. Any missing piece (refresh token or PoToken) returns ``{}``
    so the callback surfaces a clear error instead of storing a partial secret.
    """
    creds = source_exchange_google(code, guild_id)
    if not creds:
        return {}
    pot = fetch_guild_potoken()
    if not pot:
        return {}
    return {
        "provider": provider,
        "oauth_refresh_token": creds["oauth_refresh_token"],
        "pot_token": pot["pot_token"],
        "pot_visitor_data": pot["pot_visitor_data"],
        "connected_by": connected_by,
        "connected_at": int(time.time()),
    }
