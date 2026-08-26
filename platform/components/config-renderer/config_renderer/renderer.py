"""Pure rendering of the Lavalink ``application.yml``.

This module builds the complete Lavalink configuration document from a
:class:`~config_renderer.model.LavalinkCredentials` and a
:class:`~config_renderer.model.LavalinkSettings` and serializes it to YAML with
PyYAML. It is a pure function of its inputs: no AWS calls, no SQLite, no I/O.

The YouTube client cascade (``TV``, ``TVHTML5_SIMPLY``, ``ANDROID_VR``,
``MUSIC``, ``WEB``) and the LavaSrc provider order (SoundCloud first, then
YouTube ISRC/text) are preserved from the platform architecture — see the
HelloDJ architecture steering and the legacy ``render_lavalink_config.py``.

Requirements: 6.1, 7.3, 15.1
"""

from __future__ import annotations

from typing import Any

import yaml

from .model import LavalinkCredentials, LavalinkSettings

__all__ = [
    "YOUTUBE_CLIENTS",
    "build_config",
    "render_yaml",
]

#: The canonical YouTube client cascade shared across every HelloDJ config.
#: Order matters: TV (OAuth) → TVHTML5_SIMPLY → ANDROID_VR → MUSIC → WEB.
YOUTUBE_CLIENTS: tuple[str, ...] = (
    "TV",
    "TVHTML5_SIMPLY",
    "ANDROID_VR",
    "MUSIC",
    "WEB",
)


def _tidal_enabled(creds: LavalinkCredentials) -> bool:
    """Tidal is enabled with client credentials or a real access token."""
    has_creds = bool(creds.tidal_client_id and creds.tidal_client_secret)
    token = creds.tidal_token
    has_token = bool(token) and token not in ("none", "disabled")
    return has_creds or has_token


def _spotify_enabled(creds: LavalinkCredentials) -> bool:
    """Spotify is enabled only when both client credentials are present."""
    return bool(creds.spotify_client_id and creds.spotify_client_secret)


def build_config(
    creds: LavalinkCredentials,
    settings: LavalinkSettings,
) -> dict[str, Any]:
    """Build the complete Lavalink config document as a plain dict.

    The result is JSON/YAML-serializable and deterministic for a given input,
    which makes it straightforward to unit-test without serializing.
    """
    spotify_on = _spotify_enabled(creds)
    tidal_on = _tidal_enabled(creds)
    yt_oauth_on = bool(creds.youtube_oauth_refresh_token)

    # LavaSrc requires a non-empty token placeholder even when Tidal is off.
    tidal_token_value = creds.tidal_token if tidal_on else "disabled"

    youtube_plugin: dict[str, Any] = {
        "enabled": True,
        "allowSearch": True,
        "allowDirectVideoIds": True,
        "allowDirectPlaylistIds": True,
        "clients": list(YOUTUBE_CLIENTS),
        "clientOptions": {
            "MUSIC": {"playback": False, "videoLoading": False},
        },
        "oauth": {
            "enabled": yt_oauth_on,
            "skipInitialization": True,
            "refreshToken": creds.youtube_oauth_refresh_token,
        },
        "pot": {
            "token": creds.youtube_pot_token,
            "visitorData": creds.youtube_pot_visitor_data,
        },
        "remoteCipher": {
            "url": settings.ytcipher_url,
            "password": creds.ytcipher_token,
            "userAgent": settings.ytcipher_user_agent,
        },
    }

    lavasrc_plugin: dict[str, Any] = {
        "providers": list(settings.lavasrc_providers),
        "sources": {
            "spotify": spotify_on,
            "tidal": tidal_on,
            "youtube": True,
        },
        "tidal": {
            "countryCode": settings.tidal_country_code,
            "searchLimit": settings.tidal_search_limit,
            "clientId": creds.tidal_client_id,
            "clientSecret": creds.tidal_client_secret,
            "token": tidal_token_value,
        },
        "spotify": {
            "clientId": creds.spotify_client_id,
            "clientSecret": creds.spotify_client_secret,
            "countryCode": settings.spotify_country_code,
            "playlistLoadLimit": settings.spotify_playlist_load_limit,
            "albumLoadLimit": settings.spotify_album_load_limit,
            "resolveArtistsInSearch": False,
        },
    }

    return {
        "lavalink": {
            "server": {
                "host": settings.host,
                "port": settings.port,
                "password": settings.server_password,
                "pluginsDir": settings.plugins_dir,
                # Native YouTube source disabled: the youtube plugin handles it.
                "sources": {"youtube": False},
            },
            "buffer": {"period": 500, "periodMilliseconds": 500},
            "limits": {"memory": 0, "cpu": 0},
        },
        "plugins": {
            "youtube": youtube_plugin,
            "lavasrc": lavasrc_plugin,
        },
        "sources": {
            "youtube": {"enabled": True},
            "youtubemusic": {"enabled": True},
            "soundcloud": {"enabled": True},
            "spotify": {"enabled": spotify_on},
        },
        "filters": {
            name: {"enabled": True}
            for name in (
                "enabled",
                "volume",
                "equalizer",
                "karaoke",
                "timescale",
                "tremolo",
                "vibrato",
                "distortion",
                "rotation",
                "lowPass",
                "channelMix",
            )
        },
        "server": {"port": settings.port},
    }


def render_yaml(
    creds: LavalinkCredentials,
    settings: LavalinkSettings,
) -> str:
    """Render the full Lavalink ``application.yml`` as a YAML string.

    A header comment marks the file as machine-generated. The document is
    dumped with a stable key order (``sort_keys=False``) so the output mirrors
    the structure built in :func:`build_config`.
    """
    document = build_config(creds, settings)
    body = yaml.safe_dump(
        document,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    header = (
        "# Auto-generated by config-renderer — DO NOT EDIT.\n"
        "# Credentials sourced from AWS Secrets Manager; config from DynamoDB.\n"
    )
    return header + body
