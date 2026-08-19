"""HelloDJ — Unified configuration accessor.

Reads from the encrypted credential store (SQLite) first, falls back to
environment variables for backward compatibility during migration. Once fully
migrated, env var fallback can be removed.

Usage:
    from config import cfg

    token = cfg("discord.token")              # reads creds DB, falls back to DISCORD_TOKEN env
    host = cfg("lavalink.host", "localhost")   # with default
    enabled = cfg.bool("provider.youtube")     # boolean accessor
    limit = cfg.int("tidal.search_limit", 6)   # int accessor
"""

from __future__ import annotations

import os
import logging

log = logging.getLogger(__name__)

# Lazy import to avoid circular deps at module load
_creds = None


def _get_creds():
    global _creds
    if _creds is None:
        try:
            from credentials import creds
            _creds = creds
        except Exception as exc:
            log.warning("Credential store unavailable (%s) — using env vars only", exc)
            _creds = False  # sentinel: don't retry
    return _creds if _creds else None


# Mapping: credential store key -> env var name (for fallback)
_KEY_TO_ENV = {
    # Discord
    "discord.token": "DISCORD_TOKEN",
    "discord.app_id": "DISCORD_APPID",
    "discord.public_key": "DISCORD_PUBKEY",
    "discord.owner_id": "BOT_OWNER_ID",

    # Spotify
    "spotify.client_id": "SPOTIFY_CLIENT_ID",
    "spotify.client_secret": "SPOTIFY_CLIENT_SECRET",

    # Tidal
    "tidal.client_id": "TIDAL_CLIENT_ID",
    "tidal.client_secret": "TIDAL_CLIENT_SECRET",
    "tidal.api_token": "TIDAL_TOKEN",
    "tidal.country_code": "TIDAL_COUNTRY_CODE",
    "tidal.search_limit": "TIDAL_SEARCH_LIMIT",
    "tidal.td_client_id": "TD_CLIENT_ID",
    "tidal.td_client_secret": "TD_CLIENT_SECRET",

    # YouTube
    "youtube.oauth_enabled": "YOUTUBE_OAUTH_ENABLED",
    "youtube.oauth_refresh_token": "YOUTUBE_OAUTH_REFRESH_TOKEN",
    "youtube.pot_token": "POT_TOKEN",
    "youtube.pot_visitor_data": "POT_VISITOR_DATA",

    # yt-cipher
    "ytcipher.url": "YTCIPHER_URL",
    "ytcipher.api_token": "YTCIPHER_API_TOKEN",

    # Providers
    "provider.youtube": "PROVIDER_YOUTUBE",
    "provider.youtube_music": "PROVIDER_YOUTUBEMUSIC",
    "provider.soundcloud": "PROVIDER_SOUNDCLOUD",
    "provider.spotify": "PROVIDER_SPOTIFY",
    "provider.tidal": "PROVIDER_TIDAL",

    # Lavalink
    "lavalink.host": "LAVALINK_HOST",
    "lavalink.port": "LAVALINK_PORT",
    "lavalink.password": "LAVALINK_PASSWORD",

    # LLM
    "llm.api_url": "LLM_API_URL",
    "llm.api_key": "LLM_API_KEY",
    "llm.model": "LLM_MODEL",

    # STT
    "stt.engine": "STT_ENGINE",
    "stt.api_key": "STT_API_KEY",
    "stt.url": "STT_URL",
    "stt.model_size": "STT_MODEL_SIZE",
    "stt.whisper_endpoint": "STT_WHISPER_ENDPOINT",
    "stt.bedrock_language": "STT_BEDROCK_LANGUAGE",
    "stt.bedrock_timeout": "STT_BEDROCK_TIMEOUT",

    # TTS
    "tts.engine": "TTS_ENGINE",
    "tts.api_key": "TTS_API_KEY",
    "tts.voice": "TTS_VOICE",
    "tts.kokoro_endpoint": "TTS_KOKORO_ENDPOINT",
    "tts.speaches_endpoint": "TTS_SPEACHES_ENDPOINT",
    "tts.speaches_url": "SPEACHES_URL",
    "tts.kokoro_url": "KOKORO_URL",

    # AWS
    "aws.region": "AWS_REGION",
    "aws.role_arn": "AWS_ROLE_ARN",

    # Polly
    "polly.voice_id": "POLLY_VOICE_ID",
    "polly.output_format": "POLLY_OUTPUT_FORMAT",

    # Stream services
    "stream.tidal_url": "TIDAL_STREAM_URL",
    "stream.spotify_url": "SPOTIFY_STREAM_URL",

    # Misc
    "app.base_url": "HELLODJ_BASE_URL",
    "voice.enabled": "VOICE_ENABLED",
    "stems.model": "STEM_MODEL",
    "bedrock.s3_bucket": "BEDROCK_S3_BUCKET",

    # Genius
    "genius.client_id": "GENIUS_CLIENT_ID",
    "genius.client_secret": "GENIUS_CLIENT_SECRET",
    "genius.access_token": "GENIUS_ACCESS_TOKEN",
    "genius.api_key": "GENIUS_API_KEY",

    # News / Stocks
    "news.api_key": "NEWS_API_KEY",
    "stocks.api_key": "STOCKS_API_KEY",

    # Debug (these stay as env vars — they're process config, not secrets)
    "debug.enabled": "HELLODJ_DEBUG",
    "debug.trace": "HELLODJ_DEBUG_TRACE",
    "debug.level": "HELLODJ_DEBUG_LEVEL",
    "debug.modules": "HELLODJ_DEBUG_MODULES",
    "debug.voice": "HELLODJ_VOICE_DEBUG",

    # Metrics
    "metrics.retention_days": "METRICS_RETENTION_DAYS",

    # Wake word
    "voice.wakeword_model": "WAKE_WORD_MODEL_PATH",
}

# Reverse mapping for convenience
_ENV_TO_KEY = {v: k for k, v in _KEY_TO_ENV.items()}


class Config:
    """Unified config accessor: reads from encrypted credential store only.

    The only env vars consulted are HELLODJ_DB_KEY (encryption key),
    DATA_DIR (data path), and HELLODJ_BASE_URL (callback URL).
    Everything else comes from the SQLite credential store.
    """

    def __call__(self, key: str, default: str | None = None) -> str | None:
        """Get a config value by credential store key."""
        store = _get_creds()
        if store:
            val = store.get(key)
            if val is not None:
                return val
        return default

    def bool(self, key: str, default: bool = False) -> bool:
        """Get a boolean config value."""
        val = self(key)
        if val is None:
            return default
        return val.lower() in ("true", "1", "yes", "on", "t")

    def int(self, key: str, default: int = 0) -> int:
        """Get an integer config value."""
        val = self(key)
        if val is None:
            return default
        try:
            return int(val)
        except (ValueError, TypeError):
            return default

    def float(self, key: str, default: float = 0.0) -> float:
        """Get a float config value."""
        val = self(key)
        if val is None:
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    def set(self, key: str, value: str) -> None:
        """Write a value to the credential store."""
        store = _get_creds()
        if store:
            store.set(key, value)
        else:
            log.warning("Cannot write config %s — credential store unavailable", key)

    def env(self, env_var: str, default: str | None = None) -> str | None:
        """Get by env var name (maps to credential store key internally)."""
        key = _ENV_TO_KEY.get(env_var)
        if key:
            return self(key, default)
        return default


cfg = Config()
