"""Tests for the PRIMARY HelloDJ bot invite URL on the guild-detail page.

The primary ``discord-bot-core`` application is the command-owner — the ONLY
bot that registers slash commands (``/activate`` included). The guild-detail
page must offer its invite as the required first step; pool bots are optional
additional voice instances and never surface commands. This covers the
:func:`guild_bot_routes.primary_bot_invite_url` helper that builds that link
from the same ``DISCORD_CLIENT_ID`` used for Discord OAuth.
"""

from __future__ import annotations

from flask import Flask

from guild_bot_routes import primary_bot_invite_url


def _app(**config) -> Flask:
    app = Flask(__name__)
    app.config.update(config)
    return app


def test_primary_invite_url_uses_discord_client_id() -> None:
    app = _app(DISCORD_CLIENT_ID="1534778518137995325", DISCORD_CLIENT_SECRET="s")
    with app.app_context():
        url = primary_bot_invite_url()
    assert url.startswith("https://discord.com/oauth2/authorize?")
    assert "client_id=1534778518137995325" in url
    # Must request the applications.commands scope so slash commands register.
    assert "applications.commands" in url


def test_primary_invite_url_empty_when_no_client_id() -> None:
    app = _app()  # no DISCORD_CLIENT_ID / secret / ARN configured
    with app.app_context():
        assert primary_bot_invite_url() == ""


def test_primary_invite_url_never_leaks_secret() -> None:
    app = _app(
        DISCORD_CLIENT_ID="123", DISCORD_CLIENT_SECRET="super-secret-value"
    )
    with app.app_context():
        url = primary_bot_invite_url()
    assert "super-secret-value" not in url
