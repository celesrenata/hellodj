"""OAuth token-exchange helpers for the auth blueprint.

These pure helpers perform the network token exchanges for the Cognito hosted
UI and Discord OAuth and decode the resulting claims. They are factored out of
``auth.py`` so the blueprint module stays within the per-file line ceiling and
so the exchange logic can be unit-tested in isolation.

Each helper degrades gracefully (returns an empty/``None`` result) on missing
input, unconfigured providers, or any network/parse error so login/linking
never fails hard.

Requirements: 3.1, 3.2, 8.x
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from flask import current_app

log = logging.getLogger(__name__)

__all__ = [
    "exchange_code_for_groups",
    "discord_id_from_code",
    "discord_manageable_guilds_from_code",
    "groups_from_id_token",
]

#: Discord ``MANAGE_GUILD`` permission bit. A user who is a guild owner OR holds
#: this permission is allowed to add HelloDJ to that guild.
DISCORD_MANAGE_GUILD = 0x20

DISCORD_API_BASE = "https://discord.com/api"

#: Discord's API requires a descriptive User-Agent; requests with a default
#: library UA (e.g. ``Python-urllib/3.x``) are rejected at Discord's Cloudflare
#: edge with HTTP 403 "error code: 1010" (ban by client signature). Send an
#: identifying UA in Discord's documented ``ClientName ($url, $version)`` shape.
DISCORD_USER_AGENT = "HelloDJ (https://hellodj.bot, 1.0)"


def exchange_code_for_groups(code: str, verifier: str, redirect_uri: str) -> list[str]:
    """Exchange a Cognito auth code for tokens and return its group claims.

    Performs the authorization-code + PKCE token exchange against the Cognito
    hosted-UI ``/oauth2/token`` endpoint, then decodes the ID token payload to
    read the ``cognito:groups`` claim. The claim drives the admin gate: a user
    in the ``admins`` group is an administrator (manages all accounts), any
    other authenticated user is a standard user.

    Returns an empty list when the exchange can't be performed (missing code,
    unconfigured Cognito, or a network/parse error) so login still succeeds as
    a non-admin rather than failing hard.
    """
    if not code:
        return []
    domain = current_app.config.get("COGNITO_DOMAIN", "").rstrip("/")
    client_id = current_app.config.get("COGNITO_CLIENT_ID", "")
    if not domain or not client_id:
        return []
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
    ).encode("ascii")
    request_obj = urllib.request.Request(
        f"{domain}/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=8) as resp:  # noqa: S310
            tokens = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - login degrades to non-admin on failure
        return []
    return groups_from_id_token(tokens.get("id_token", ""))


def discord_id_from_code(code: str, redirect_uri: str) -> str | None:
    """Exchange a Discord OAuth code for the user's Discord id.

    Performs the Discord token exchange then calls ``/users/@me`` to read the
    numeric user id used to link/resolve the account (R3.1, R3.2). Returns
    ``None`` on any failure so linking/login degrades rather than erroring.
    """
    if not code:
        log.warning("discord oauth: empty authorization code")
        return None
    # Resolve id+secret from plain env first, then lazily from the
    # `discord-oauth` Secrets Manager secret (keeps the secret out of the k8s
    # manifest). Imported lazily to avoid a circular import at module load.
    from source_token_exchange import discord_client_credentials  # noqa: PLC0415

    client_id, client_secret = discord_client_credentials()
    if not client_id or not client_secret:
        # Log which half is missing (never the values) so a mis-wired secret is
        # diagnosable from the pod logs instead of silently 500-ing to
        # ?error=discord_failed.
        log.warning(
            "discord oauth: missing credentials (client_id=%s, client_secret=%s)",
            "set" if client_id else "empty",
            "set" if client_secret else "empty",
        )
        return None
    token_body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("ascii")
    try:
        token_req = urllib.request.Request(
            f"{DISCORD_API_BASE}/oauth2/token",
            data=token_body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": DISCORD_USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(token_req, timeout=8) as resp:  # noqa: S310
            access = json.loads(resp.read().decode("utf-8")).get("access_token")
        if not access:
            log.warning("discord oauth: token response had no access_token")
            return None
        me_req = urllib.request.Request(
            f"{DISCORD_API_BASE}/users/@me",
            headers={
                "Authorization": f"Bearer {access}",
                "User-Agent": DISCORD_USER_AGENT,
            },
        )
        with urllib.request.urlopen(me_req, timeout=8) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8")).get("id")
    except urllib.error.HTTPError as exc:  # noqa: PERF203
        # Discord returns the OAuth error (e.g. invalid_grant, redirect_uri
        # mismatch) in the body. That body carries no token/secret material, so
        # logging it is safe and turns a silent failure into a real fact. Log
        # the redirect_uri too since a mismatch there is the most common cause.
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001
            detail = "<unreadable>"
        log.warning(
            "discord oauth exchange failed: HTTP %s at %s (redirect_uri=%s): %s",
            exc.code,
            getattr(exc, "url", "?"),
            redirect_uri,
            detail,
        )
        return None
    except Exception:  # noqa: BLE001
        log.warning(
            "discord oauth exchange failed (redirect_uri=%s)",
            redirect_uri,
            exc_info=True,
        )
        return None


def discord_manageable_guilds_from_code(
    code: str, redirect_uri: str
) -> list[dict[str, str]]:
    """Exchange a Discord ``guilds``-scope code for the user's manageable guilds.

    Performs the Discord token exchange (the code must have been obtained with
    the ``identify guilds`` scope) then calls ``/users/@me/guilds`` and filters
    to the guilds the user may add a bot to: those they OWN or where they hold
    the ``MANAGE_GUILD`` permission. Returns a list of
    ``{"id", "name", "owner"}`` (owner as ``"1"``/``""``) ordered as Discord
    returns them. Returns ``[]`` on any failure so the flow degrades to an
    empty picker rather than erroring.
    """
    access = _discord_access_token(code, redirect_uri)
    if not access:
        return []
    try:
        req = urllib.request.Request(
            f"{DISCORD_API_BASE}/users/@me/guilds",
            headers={
                "Authorization": f"Bearer {access}",
                "User-Agent": DISCORD_USER_AGENT,
            },
        )
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
            guilds = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - degrade to an empty picker on failure
        log.warning("discord guilds fetch failed", exc_info=True)
        return []
    if not isinstance(guilds, list):
        return []
    manageable: list[dict[str, str]] = []
    for guild in guilds:
        if not isinstance(guild, dict):
            continue
        is_owner = bool(guild.get("owner", False))
        try:
            perms = int(str(guild.get("permissions", "0")))
        except ValueError:
            perms = 0
        if is_owner or (perms & DISCORD_MANAGE_GUILD):
            manageable.append(
                {
                    "id": str(guild.get("id", "")),
                    "name": str(guild.get("name", "")),
                    "owner": "1" if is_owner else "",
                }
            )
    return [g for g in manageable if g["id"]]


def _discord_access_token(code: str, redirect_uri: str) -> str | None:
    """Exchange a Discord OAuth code for an access token, or ``None``.

    Shared by the guilds-scope flow; mirrors the token exchange in
    :func:`discord_id_from_code` (same credential resolution, User-Agent, and
    graceful degradation) without duplicating the ``/users/@me`` call.
    """
    if not code:
        return None
    from source_token_exchange import discord_client_credentials  # noqa: PLC0415

    client_id, client_secret = discord_client_credentials()
    if not client_id or not client_secret:
        log.warning("discord guilds oauth: missing credentials")
        return None
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("ascii")
    try:
        req = urllib.request.Request(
            f"{DISCORD_API_BASE}/oauth2/token",
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": DISCORD_USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8")).get("access_token")
    except urllib.error.HTTPError as exc:
        # Discord returns the OAuth error (invalid_client, redirect_uri_mismatch,
        # invalid_grant, …) in the body. It carries no token/secret material, so
        # logging it turns a bare HTTP 401 traceback into an actionable fact.
        # Mirrors ``discord_id_from_code``'s HTTPError handling.
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001
            detail = "<unreadable>"
        log.warning(
            "discord guilds token exchange failed: HTTP %s at %s "
            "(redirect_uri=%s): %s",
            exc.code,
            getattr(exc, "url", "?"),
            redirect_uri,
            detail,
        )
        return None
    except Exception:  # noqa: BLE001
        log.warning(
            "discord guilds token exchange failed (redirect_uri=%s)",
            redirect_uri,
            exc_info=True,
        )
        return None


def groups_from_id_token(id_token: str) -> list[str]:
    """Return the ``cognito:groups`` claim from a JWT ID token payload.

    Decodes the JWT payload segment only (no signature verification — the token
    came directly from the Cognito token endpoint over TLS in this same
    request, so it is trusted here for the group-membership read).
    """
    try:
        payload_b64 = id_token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return []
    groups = claims.get("cognito:groups", [])
    return list(groups) if isinstance(groups, list) else []
