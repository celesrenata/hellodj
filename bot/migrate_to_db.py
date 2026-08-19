#!/usr/bin/env python3
"""Migrate existing credentials from env vars + oauth.json into the encrypted SQLite store.

Run once after deploying the new credentials module. Safe to re-run (upserts).

Usage:
    HELLODJ_DB_KEY=<your-key> python migrate_to_db.py
"""

import json
import os
import sys
from pathlib import Path

# Ensure we can import credentials
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from credentials import creds

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))


def migrate_env_vars():
    """Migrate known env vars into the credential store."""
    mappings = {
        # Discord
        "DISCORD_TOKEN": "discord.token",
        "DISCORD_APPID": "discord.app_id",
        "DISCORD_PUBKEY": "discord.public_key",
        "BOT_OWNER_ID": "discord.owner_id",

        # Spotify
        "SPOTIFY_CLIENT_ID": "spotify.client_id",
        "SPOTIFY_CLIENT_SECRET": "spotify.client_secret",

        # Tidal
        "TIDAL_CLIENT_ID": "tidal.client_id",
        "TIDAL_CLIENT_SECRET": "tidal.client_secret",
        "TIDAL_TOKEN": "tidal.api_token",
        "TIDAL_COUNTRY_CODE": "tidal.country_code",
        "TIDAL_SEARCH_LIMIT": "tidal.search_limit",

        # YouTube
        "YOUTUBE_OAUTH_ENABLED": "youtube.oauth_enabled",
        "YOUTUBE_OAUTH_REFRESH_TOKEN": "youtube.oauth_refresh_token",
        "POT_TOKEN": "youtube.pot_token",
        "POT_VISITOR_DATA": "youtube.pot_visitor_data",

        # yt-cipher
        "YTCIPHER_URL": "ytcipher.url",
        "YTCIPHER_API_TOKEN": "ytcipher.api_token",

        # Providers (enabled flags)
        "PROVIDER_YOUTUBE": "provider.youtube",
        "PROVIDER_YOUTUBEMUSIC": "provider.youtube_music",
        "PROVIDER_SOUNDCLOUD": "provider.soundcloud",
        "PROVIDER_SPOTIFY": "provider.spotify",
        "PROVIDER_TIDAL": "provider.tidal",

        # Lavalink
        "LAVALINK_HOST": "lavalink.host",
        "LAVALINK_PORT": "lavalink.port",
        "LAVALINK_PASSWORD": "lavalink.password",

        # LLM
        "LLM_API_URL": "llm.api_url",
        "LLM_API_KEY": "llm.api_key",
        "LLM_MODEL": "llm.model",

        # STT
        "STT_ENGINE": "stt.engine",
        "STT_API_KEY": "stt.api_key",
        "STT_URL": "stt.url",
        "STT_MODEL_SIZE": "stt.model_size",
        "STT_WHISPER_ENDPOINT": "stt.whisper_endpoint",
        "STT_BEDROCK_LANGUAGE": "stt.bedrock_language",
        "STT_BEDROCK_TIMEOUT": "stt.bedrock_timeout",

        # TTS
        "TTS_ENGINE": "tts.engine",
        "TTS_API_KEY": "tts.api_key",
        "TTS_VOICE": "tts.voice",
        "TTS_KOKORO_ENDPOINT": "tts.kokoro_endpoint",
        "TTS_SPEACHES_ENDPOINT": "tts.speaches_endpoint",
        "SPEACHES_URL": "tts.speaches_url",
        "KOKORO_URL": "tts.kokoro_url",

        # AWS (for Bedrock STT/TTS)
        "AWS_REGION": "aws.region",
        "AWS_ROLE_ARN": "aws.role_arn",

        # Polly
        "POLLY_VOICE_ID": "polly.voice_id",
        "POLLY_OUTPUT_FORMAT": "polly.output_format",

        # Misc
        "HELLODJ_BASE_URL": "app.base_url",
        "VOICE_ENABLED": "voice.enabled",
        "STEM_MODEL": "stems.model",
        "BEDROCK_S3_BUCKET": "bedrock.s3_bucket",
    }

    migrated = 0
    for env_var, db_key in mappings.items():
        val = os.environ.get(env_var, "")
        if val:  # Only migrate non-empty values
            creds.set(db_key, val)
            migrated += 1
            print(f"  {env_var} -> {db_key}")

    print(f"Migrated {migrated} env vars")
    return migrated


def migrate_oauth_json():
    """Migrate oauth.json tokens into the credential store."""
    oauth_file = DATA_DIR / "oauth.json"
    if not oauth_file.exists():
        print("No oauth.json found — skipping")
        return 0

    data = json.loads(oauth_file.read_text())
    providers = data.get("providers", {})
    migrated = 0

    for provider, tokens in providers.items():
        for token_key, token_val in tokens.items():
            if token_val:  # Only non-empty
                db_key = f"{provider}.{token_key}"
                creds.set(db_key, str(token_val))
                migrated += 1
                print(f"  oauth.json/{provider}.{token_key} -> {db_key}")

    print(f"Migrated {migrated} oauth.json entries")
    return migrated


def main():
    print(f"Migrating credentials to SQLite at {creds._db_path}")
    print()
    print("=== Env Vars ===")
    migrate_env_vars()
    print()
    print("=== oauth.json ===")
    migrate_oauth_json()
    print()
    print("Migration complete. Verify with:")
    print(f"  HELLODJ_DB_KEY=<key> python -c \"from credentials import creds; print(creds.keys())\"")


if __name__ == "__main__":
    main()
