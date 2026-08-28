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
import urllib.parse
import urllib.request

from flask import current_app

__all__ = [
    "exchange_code_for_groups",
    "discord_id_from_code",
    "groups_from_id_token",
]

DISCORD_API_BASE = "https://discord.com/api"


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
        return None
    # Resolve id+secret from plain env first, then lazily from the
    # `discord-oauth` Secrets Manager secret (keeps the secret out of the k8s
    # manifest). Imported lazily to avoid a circular import at module load.
    from source_token_exchange import discord_client_credentials  # noqa: PLC0415

    client_id, client_secret = discord_client_credentials()
    if not client_id or not client_secret:
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
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(token_req, timeout=8) as resp:  # noqa: S310
            access = json.loads(resp.read().decode("utf-8")).get("access_token")
        if not access:
            return None
        me_req = urllib.request.Request(
            f"{DISCORD_API_BASE}/users/@me",
            headers={"Authorization": f"Bearer {access}"},
        )
        with urllib.request.urlopen(me_req, timeout=8) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8")).get("id")
    except Exception:  # noqa: BLE001
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
