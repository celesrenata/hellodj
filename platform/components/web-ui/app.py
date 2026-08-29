"""HelloDJ web-ui Flask application (dark-glassmorphism sidebar admin UI).

This is the configuration and administration UI for the re-platformed HelloDJ
(R6.5, R14). It follows the modern-web-ui design standard: a dark glassmorphism
sidebar shell built with Flask + Jinja2 templates, HTMX for partial page
updates, Alpine.js for client-side interactivity, and a Tailwind CSS v4 build
(WCAG AA contrast palette in OKLCH).

Auth is delegated to the shared routing logic
(:func:`hellodj_platform_logic.auth_routing.route_auth`) via the :mod:`auth`
blueprint: Cognito for admin/registration/recovery, Discord OAuth for
day-to-day login, and a first-party Tidal OAuth callback wired to the
``tidal-stream`` component. Configuration is read/written through DynamoDB
(``hellodj-core`` via ``data_access.CoreTable``); secrets come from AWS Secrets
Manager. There is no PostgreSQL/SQLite anywhere.

Each Python source file is kept under the 500-line ceiling (R13.3) by splitting
concerns across :mod:`auth`, :mod:`config_store`, :mod:`secrets_store`, and
:mod:`pages`.

Requirements: 6.5, 8.1, 8.2, 8.3, 8.4, 8.5, 9.2, 14.1, 14.2, 14.3, 14.4
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, redirect, session, url_for

from admin_directory import AdminDirectory, build_admin_directory
from auth import build_auth_blueprint
from auth_ratelimit import RateLimiter
from bootstrap import build_services
from cognito_auth import build_cognito_auth
from cognito_jwt import build_verifier
from config_store import ConfigStore
from entitlement_routes import build_entitlement_blueprint
from guild_routes import build_guild_blueprint
from invite_admin_routes import build_invite_admin_blueprint
from invite_public_routes import build_invite_public_blueprint
from pages import build_pages_blueprint
from secrets_store import SecretsProvider

__all__ = ["create_app", "login_required"]

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATE_DIR = Path(__file__).parent / "templates"


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """Redirect to the login page when no authenticated session exists."""

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not session.get("user"):
            return redirect(url_for("pages.login"))
        return view(*args, **kwargs)

    return wrapper


def create_app(
    *,
    config_store: ConfigStore | None = None,
    secrets_provider: SecretsProvider | None = None,
    admin_directory: AdminDirectory | None = None,
    overrides: dict[str, Any] | None = None,
) -> Flask:
    """Build and configure the web-ui Flask application.

    Args:
        config_store: Optional pre-built :class:`ConfigStore`. When omitted the
            app runs in a degraded, no-datastore mode (useful for template
            snapshot tests) where config reads return empty mappings.
        secrets_provider: Optional pre-built :class:`SecretsProvider`.
        overrides: Optional config overrides merged last (used by tests).

    Returns:
        A configured :class:`flask.Flask` instance.
    """
    _configure_logging()
    app = Flask(
        __name__,
        static_folder=str(STATIC_DIR),
        template_folder=str(TEMPLATE_DIR),
    )
    _configure(app, overrides or {})

    # Runtime services (config, user profiles, guild admin, per-guild sources,
    # invites) built from the environment. When a config_store is explicitly
    # injected (tests), respect it; otherwise bootstrap the full service set.
    services = build_services()
    app.extensions["config_store"] = (
        config_store if config_store is not None else services["config_store"]
    )
    app.extensions["user_profiles"] = services["user_profiles"]
    app.extensions["guild_admin"] = services["guild_admin"]
    app.extensions["guild_sources"] = services["guild_sources"]
    # Unified per-user source-credential store (encrypted DynamoDB); None in
    # degraded mode (no KMS / CMK) so callbacks skip the new write and fall
    # back to the legacy per-guild secret (R2.6).
    app.extensions["source_credentials"] = services["source_credentials"]
    app.extensions["guild_identity_service"] = services[
        "guild_identity_service"
    ]
    # Global Discord bot-application pool assignment (multi-bot invite links);
    # None in degraded mode (no secrets client).
    app.extensions["bot_app_assignment"] = services["bot_app_assignment"]
    app.extensions["invite_service"] = services["invite_service"]
    # Per-user entitlement control plane (admin-only); None in degraded mode.
    app.extensions["entitlement_service"] = services["entitlement_service"]
    app.extensions["secrets"] = secrets_provider
    # The admin panel manages ALL accounts via Cognito; falls back to the
    # env-built directory (or None → degraded/empty) when not injected.
    app.extensions["admin_directory"] = (
        admin_directory
        if admin_directory is not None
        else build_admin_directory()
    )
    # First-party auth-form services: server-side Cognito calls, JWKS token
    # verification, and a best-effort rate limiter. Each degrades to None when
    # Cognito is unconfigured (auth routes then render "auth unavailable").
    app.extensions["cognito_auth"] = build_cognito_auth()
    app.extensions["cognito_jwt"] = build_verifier(
        user_pool_id=os.getenv("HELLODJ_COGNITO_USER_POOL_ID", ""),
        region=os.getenv("AWS_REGION", "us-east-1"),
        client_id=os.getenv("COGNITO_CLIENT_ID", ""),
    )
    app.extensions["auth_rate_limiter"] = RateLimiter()

    app.register_blueprint(build_auth_blueprint())
    app.register_blueprint(build_pages_blueprint())
    app.register_blueprint(build_invite_public_blueprint())
    app.register_blueprint(build_guild_blueprint())
    app.register_blueprint(build_invite_admin_blueprint())
    app.register_blueprint(build_entitlement_blueprint())

    _register_static_hash(app)
    _register_health(app)
    return app


def _configure_logging() -> None:
    """Send app logs to stdout at ``LOG_LEVEL`` (default INFO).

    Without this the web-ui had no logging config, so module-level ``log.*``
    calls (e.g. the invite-email SES failure warning) never surfaced under
    gunicorn's sync worker — making send failures undiagnosable. Idempotent:
    only configures the root logger once.
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    else:
        root.setLevel(level)


def _configure(app: Flask, overrides: dict[str, Any]) -> None:
    """Populate ``app.config`` from environment then apply overrides."""
    stage = os.getenv("HELLODJ_STAGE", "beta")
    app.config.update(
        SECRET_KEY=os.getenv("FLASK_SECRET_KEY", os.urandom(32)),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("HELLODJ_COOKIE_SECURE", "1") == "1",
        HELLODJ_STAGE=stage,
        PUBLIC_BASE_URL=os.getenv("HELLODJ_PUBLIC_BASE_URL", ""),
        DISCORD_CLIENT_ID=os.getenv("DISCORD_CLIENT_ID", ""),
        DISCORD_CLIENT_SECRET=os.getenv("DISCORD_CLIENT_SECRET", ""),
        COGNITO_DOMAIN=os.getenv("COGNITO_DOMAIN", ""),
        COGNITO_CLIENT_ID=os.getenv("COGNITO_CLIENT_ID", ""),
        TIDAL_STREAM_URL=os.getenv("TIDAL_STREAM_URL", ""),
        # Per-guild source OAuth client ids (Spotify/Tidal secrets stay with
        # the sidecars; YouTube has no per-guild sidecar so the web-ui holds
        # GOOGLE_CLIENT_SECRET and completes the code->refresh-token exchange).
        SPOTIFY_CLIENT_ID=os.getenv("SPOTIFY_CLIENT_ID", ""),
        TIDAL_CLIENT_ID=os.getenv("TIDAL_CLIENT_ID", ""),
        GOOGLE_CLIENT_ID=os.getenv("GOOGLE_CLIENT_ID", ""),
        GOOGLE_CLIENT_SECRET=os.getenv("GOOGLE_CLIENT_SECRET", ""),
        # Lazily resolved from Secrets Manager when the plain env is absent.
        HELLODJ_GOOGLE_OAUTH_SECRET_ARN=os.getenv(
            "HELLODJ_GOOGLE_OAUTH_SECRET_ARN", ""
        ),
        # Discord OAuth client secret is NOT injected as plain env (never in the
        # k8s manifest / cloud assembly); it is resolved lazily from the
        # `hellodj/<stage>/discord-oauth` Secrets Manager secret at callback
        # time (mirrors the Google/Spotify pattern).
        HELLODJ_DISCORD_OAUTH_SECRET_ARN=os.getenv(
            "HELLODJ_DISCORD_OAUTH_SECRET_ARN", ""
        ),
        # In-cluster potoken-server (bgutil-ytdlp-pot-provider) POST /get_pot.
        POTOKEN_SERVER_URL=os.getenv(
            "POTOKEN_SERVER_URL",
            f"http://potoken-server.hellodj-{stage}.svc.cluster.local:4416",
        ),
        # Source-credentials CMK id for envelope-encrypting stored tokens
        # (unified-oauth-and-token-watchdog). Absent -> no unified store wired.
        HELLODJ_SOURCE_CREDS_KMS_KEY_ID=os.getenv(
            "HELLODJ_SOURCE_CREDS_KMS_KEY_ID", ""
        ),
    )
    app.config.update(overrides)


def _register_static_hash(app: Flask) -> None:
    """Register a ``static_hash`` template global for cache-busted assets."""

    @app.template_global()
    def static_hash(filename: str) -> str:  # type: ignore[unused-ignore]
        filepath = STATIC_DIR / filename
        base = url_for("static", filename=filename)
        if filepath.is_file():
            digest = hashlib.md5(
                filepath.read_bytes(), usedforsecurity=False
            ).hexdigest()[:8]
            return f"{base}?v={digest}"
        return base


def _register_health(app: Flask) -> None:
    """Register a liveness endpoint for the load balancer / EKS probes."""

    @app.route("/healthz")
    def healthz():  # type: ignore[unused-ignore]
        return {"status": "ok", "stage": app.config["HELLODJ_STAGE"]}


# Gunicorn entry point: `gunicorn app:app` in the container.
app = create_app()
