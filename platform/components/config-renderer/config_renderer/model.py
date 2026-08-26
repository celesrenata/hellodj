"""Typed inputs for rendering the Lavalink ``application.yml``.

Two dataclasses separate *secret* material (from AWS Secrets Manager) from
*non-secret* configuration (from DynamoDB), so the renderer stays a pure
function of well-typed inputs and never touches SQLite or a local credential
database.

Requirements: 6.1, 7.3, 15.1
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "LavalinkCredentials",
    "LavalinkSettings",
]


def _as_str(value: Any, default: str = "") -> str:
    """Coerce a Secrets Manager / DynamoDB value to a stripped string."""
    if value is None:
        return default
    return str(value).strip()


@dataclass(frozen=True)
class LavalinkCredentials:
    """Secret material sourced from AWS Secrets Manager.

    Every field defaults to an empty string so a partially-populated secret
    still renders a valid (if feature-reduced) config. Sources are enabled by
    the renderer only when the credentials they need are present.
    """

    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    tidal_token: str = ""
    tidal_client_id: str = ""
    tidal_client_secret: str = ""
    ytcipher_token: str = ""
    youtube_oauth_refresh_token: str = ""
    youtube_pot_token: str = ""
    youtube_pot_visitor_data: str = ""

    @classmethod
    def from_secret(cls, secret: dict[str, Any]) -> LavalinkCredentials:
        """Build credentials from a parsed Secrets Manager JSON blob.

        Accepts both flat keys (``spotify_client_id``) and the legacy
        dotted keys (``spotify.client_id``) so an operator can migrate the
        secret shape without breaking the renderer.
        """

        def pick(*keys: str) -> str:
            for key in keys:
                if key in secret:
                    return _as_str(secret[key])
            return ""

        return cls(
            spotify_client_id=pick("spotify_client_id", "spotify.client_id"),
            spotify_client_secret=pick(
                "spotify_client_secret", "spotify.client_secret"
            ),
            tidal_token=pick(
                "tidal_token", "tidal.access_token", "tidal.api_token"
            ),
            tidal_client_id=pick(
                "tidal_client_id", "tidal.td_client_id", "tidal.client_id"
            ),
            tidal_client_secret=pick(
                "tidal_client_secret",
                "tidal.td_client_secret",
                "tidal.client_secret",
            ),
            ytcipher_token=pick("ytcipher_token", "ytcipher.api_token"),
            youtube_oauth_refresh_token=pick(
                "youtube_oauth_refresh_token",
                "youtube.oauth_refresh_token",
                "youtube.refresh_token",
            ),
            youtube_pot_token=pick("youtube_pot_token", "youtube.pot_token"),
            youtube_pot_visitor_data=pick(
                "youtube_pot_visitor_data", "youtube.pot_visitor_data"
            ),
        )


@dataclass(frozen=True)
class LavalinkSettings:
    """Non-secret Lavalink configuration sourced from DynamoDB.

    Holds tunables that are safe to store as plaintext config (host/port,
    passwords are treated as secrets and live in :class:`LavalinkCredentials`
    only when required — here ``server_password`` is a low-sensitivity shared
    cluster password with a sane default and may be overridden via config).
    """

    host: str = "0.0.0.0"
    port: int = 2333
    server_password: str = "youshallnotpass"
    plugins_dir: str = "./plugins"
    tidal_country_code: str = "US"
    tidal_search_limit: int = 6
    spotify_country_code: str = "US"
    spotify_playlist_load_limit: int = 6
    spotify_album_load_limit: int = 6
    ytcipher_url: str = (
        "http://yt-cipher.hellodj-service.svc.cluster.local:8001"
    )
    ytcipher_user_agent: str = "hellodj"
    lavasrc_providers: tuple[str, ...] = field(
        default_factory=lambda: (
            "scsearch:%QUERY%",
            'ytsearch:"%ISRC%"',
            "ytsearch:%QUERY%",
        )
    )

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> LavalinkSettings:
        """Build settings from a DynamoDB config ``data`` payload.

        Unknown keys are ignored; missing keys fall back to the architecture
        defaults so a fresh DynamoDB table still renders a working config.
        """
        defaults = cls()

        def as_int(key: str, fallback: int) -> int:
            raw = config.get(key)
            if raw is None:
                return fallback
            try:
                return int(raw)
            except (TypeError, ValueError):
                return fallback

        providers = config.get("lavasrc_providers")
        if isinstance(providers, list | tuple) and providers:
            provider_tuple = tuple(str(item) for item in providers)
        else:
            provider_tuple = defaults.lavasrc_providers

        return cls(
            host=_as_str(config.get("host"), defaults.host),
            port=as_int("port", defaults.port),
            server_password=_as_str(
                config.get("server_password"), defaults.server_password
            ),
            plugins_dir=_as_str(
                config.get("plugins_dir"), defaults.plugins_dir
            ),
            tidal_country_code=_as_str(
                config.get("tidal_country_code"), defaults.tidal_country_code
            ),
            tidal_search_limit=as_int(
                "tidal_search_limit", defaults.tidal_search_limit
            ),
            spotify_country_code=_as_str(
                config.get("spotify_country_code"),
                defaults.spotify_country_code,
            ),
            spotify_playlist_load_limit=as_int(
                "spotify_playlist_load_limit",
                defaults.spotify_playlist_load_limit,
            ),
            spotify_album_load_limit=as_int(
                "spotify_album_load_limit", defaults.spotify_album_load_limit
            ),
            ytcipher_url=_as_str(
                config.get("ytcipher_url"), defaults.ytcipher_url
            ),
            ytcipher_user_agent=_as_str(
                config.get("ytcipher_user_agent"),
                defaults.ytcipher_user_agent,
            ),
            lavasrc_providers=provider_tuple,
        )
