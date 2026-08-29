"""Discord "add a server" flow: pick a guild the user manages, then claim it.

This is the entry point that lets a logged-in user get HelloDJ onto one of
their Discord servers. Without it there is no way to reach ``/guilds/<id>``
(the guild-detail page that assigns pool bots and shows the Discord bot invite
URL) — a guild only appears once its ownership is claimed.

Flow (Option 2 — Discord ``guilds`` scope):

1. ``/auth/discord/guilds/connect`` starts a Discord OAuth with the
   ``identify guilds`` scope and a CSRF ``state`` in the signed session, using
   the FIXED redirect ``/auth/discord/guilds/callback`` (one registered URI per
   stage host, mirroring the login/source callback convention).
2. ``/auth/discord/guilds/callback`` validates ``state``, exchanges the code,
   fetches ``/users/@me/guilds``, and keeps ONLY the guilds the user OWNS or has
   ``MANAGE_GUILD`` on. The candidate list is stashed in the session (so the
   claim step can authorize it) and a picker is rendered.
3. ``/auth/discord/guilds/claim`` (POST) records the chosen guild's ownership
   for the caller (``GuildAdminService.claim_ownership``) — but ONLY if the
   guild id is in the session candidate list, so a user can never claim a guild
   they don't actually manage. On success it redirects to ``/guilds/<id>`` to
   invite a bot.

Extracted to its own module (registered on the auth blueprint) so ``auth.py``
and ``guild_routes.py`` stay under the 500-line ceiling.
"""

from __future__ import annotations

import secrets as pysecrets
import urllib.parse
from typing import Any

from flask import (
    Blueprint,
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

import auth_oauth

__all__ = ["register_discord_guilds_routes"]

DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"

#: Session key holding the guilds the user may claim (from the OAuth fetch).
_CANDIDATES_KEY = "add_guild_candidates"
_STATE_KEY = "add_guild_state"


def _new_state() -> str:
    """Return a URL-safe random CSRF/state token."""
    return pysecrets.token_urlsafe(32)


def _redirect_uri(endpoint: str) -> str:
    """Return an absolute redirect URI for an auth endpoint.

    Mirrors ``auth._redirect_uri``: prefers the configured ``PUBLIC_BASE_URL``
    (the stage host) so the redirect matches the one registered in the Discord
    application, falling back to Flask's external URL builder.
    """
    base = current_app.config.get("PUBLIC_BASE_URL", "").rstrip("/")
    if base:
        return base + url_for(endpoint)
    return url_for(endpoint, _external=True)


def _discord_client_id() -> str:
    """Return the Discord OAuth client id (plain env, else the secret ARN)."""
    from source_token_exchange import discord_client_credentials  # noqa: PLC0415

    client_id, _secret = discord_client_credentials()
    return client_id


def register_discord_guilds_routes(bp: Blueprint) -> None:
    """Register the add-a-server connect/callback/claim routes on ``bp``."""

    @bp.route("/discord/guilds/connect")
    def add_guild_connect():  # type: ignore[unused-ignore]
        """Start the Discord ``identify guilds`` OAuth to pick a server."""
        if not session.get("user"):
            return redirect(url_for("pages.login"))
        state = _new_state()
        session[_STATE_KEY] = state
        params = {
            "client_id": _discord_client_id(),
            "response_type": "code",
            "scope": "identify guilds",
            "redirect_uri": _redirect_uri("auth.add_guild_callback"),
            "state": state,
        }
        return redirect(
            f"{DISCORD_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
        )

    @bp.route("/discord/guilds/callback")
    def add_guild_callback():  # type: ignore[unused-ignore]
        """Fetch the user's manageable guilds and render the picker.

        Validates ``state`` (CSRF), exchanges the code for the guilds the user
        owns or can manage, stashes them as the claim candidate list in the
        session, and renders the selection page. A denied/failed authorization
        or an empty manageable set bounces back to ``/guilds`` with a notice.
        """
        if not session.get("user"):
            return redirect(url_for("pages.login"))
        if request.args.get("error"):
            return redirect(url_for("pages.guilds", add="denied"))
        state = request.args.get("state", "")
        if not state or state != session.pop(_STATE_KEY, None):
            return redirect(url_for("pages.guilds", add="state_mismatch"))
        candidates = auth_oauth.discord_manageable_guilds_from_code(
            request.args.get("code", ""),
            _redirect_uri("auth.add_guild_callback"),
        )
        # Persist ONLY the id set (+ names) needed to authorize the claim; the
        # picker renders names, the claim step authorizes ids.
        session[_CANDIDATES_KEY] = {c["id"]: c["name"] for c in candidates}
        if not candidates:
            return redirect(url_for("pages.guilds", add="none"))
        return render_template(
            "pages/guild_select.html",
            layout=_layout(),
            nav_items=_nav(),
            active="guilds",
            candidates=candidates,
        )

    @bp.route("/discord/guilds/claim", methods=["POST"])
    def add_guild_claim():  # type: ignore[unused-ignore]
        """Claim ownership of a chosen guild, then go to its detail page.

        The submitted ``guild_id`` MUST be in the session candidate list (the
        guilds the user proved they manage via the OAuth fetch) — otherwise the
        claim is refused, so a user can never claim a server they don't control.
        Ownership is first-come-first-served; if the guild is already owned by
        someone else the caller lands back on the picker with a clear notice.
        """
        if not session.get("user"):
            return redirect(url_for("pages.login"))
        guild_id = request.form.get("guild_id", "").strip()
        candidates: dict[str, str] = session.get(_CANDIDATES_KEY, {}) or {}
        if not guild_id or guild_id not in candidates:
            return redirect(url_for("pages.guilds", add="not_authorized"))
        guild_admin = current_app.extensions.get("guild_admin")
        sub = (session.get("user") or {}).get("sub", "")
        if guild_admin is None or not sub:
            return redirect(url_for("pages.guilds", add="unavailable"))
        guild_admin.claim_ownership(
            guild_id, sub, name=candidates.get(guild_id, "")
        )
        # First-come-first-served: if someone else already owns it, the caller
        # cannot manage it — surface that rather than dropping them on a page
        # they'll be redirected away from.
        if guild_admin.owner_of(guild_id) != sub:
            return redirect(url_for("pages.guilds", add="already_claimed"))
        session.pop(_CANDIDATES_KEY, None)
        return redirect(url_for("guild.guild_detail", guild_id=guild_id))


# ---- shared nav/layout helpers (one source in pages.py) ------------------- #


def _layout() -> str:
    from pages import _layout as pages_layout  # noqa: PLC0415

    return pages_layout()


def _nav() -> list[dict[str, Any]]:
    from pages import _nav_for_current_user  # noqa: PLC0415

    return _nav_for_current_user()
