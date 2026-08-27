#!/usr/bin/env python3
"""HelloDJ — Lavalink config renderer.

Reads credentials from the encrypted credential store and renders a complete
application.yml for Lavalink. Runs as an init container before Lavalink starts.

Supports two backends:
  - PostgreSQL (preferred): Set HELLODJ_PG_URI to use CNPG cluster
  - SQLite (fallback): Used when HELLODJ_PG_URI is not set (backwards compat)

Usage:
    HELLODJ_DB_KEY=<key> HELLODJ_PG_URI=<uri> python render_lavalink_config.py /output/application.yml
"""

import asyncio
import base64
import hashlib
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


# ── Fernet decryption (same derivation as credentials.py) ──────────────────────

def _derive_key(passphrase: str) -> bytes:
    """Derive a 32-byte Fernet key from an arbitrary passphrase.

    Uses SHA-256 to normalize any-length input into a valid Fernet key (which
    requires url-safe base64 of 32 bytes).
    """
    raw = hashlib.sha256(passphrase.encode()).digest()
    return base64.urlsafe_b64encode(raw)


def _decrypt(fernet, ciphertext: bytes) -> str:
    """Decrypt a Fernet-encrypted value."""
    from cryptography.fernet import InvalidToken
    try:
        return fernet.decrypt(ciphertext).decode()
    except InvalidToken:
        log.error("Failed to decrypt credential — key may have changed")
        return ""


# ── PostgreSQL credential reader ───────────────────────────────────────────────

class PGCredentialReader:
    """Read-only credential access via PostgreSQL (asyncpg) with Fernet decryption."""

    def __init__(self, pg_uri: str, fernet):
        self._pg_uri = pg_uri
        self._fernet = fernet
        self._conn = None

    async def connect(self):
        """Establish a connection to PostgreSQL with 10s timeout."""
        import asyncpg

        try:
            self._conn = await asyncio.wait_for(
                asyncpg.connect(self._pg_uri),
                timeout=10.0,
            )
            log.info("Connected to PostgreSQL")
        except asyncio.TimeoutError:
            log.error("PostgreSQL connection timed out (10s)")
            raise
        except Exception as exc:
            log.error("PostgreSQL connection failed: %s", exc)
            raise

    async def close(self):
        """Close the PostgreSQL connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def get(self, key: str, default: str | None = None) -> str | None:
        """Get a credential by key. Returns default if not found."""
        row = await self._conn.fetchrow(
            "SELECT value FROM credentials WHERE key = $1", key
        )
        if row is None:
            return default
        return _decrypt(self._fernet, row["value"])


# ── SQLite credential reader (fallback) ────────────────────────────────────────

class SQLiteCredentialReader:
    """Read-only credential access via SQLite with Fernet decryption."""

    def __init__(self):
        self._store = None

    async def connect(self):
        """Initialize the SQLite credential store in read-only mode."""
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        os.environ.setdefault("DATA_DIR", "/app/data")
        from credentials import CredentialStore
        self._store = CredentialStore(read_only=True)
        log.info("Using SQLite credential store (fallback)")

    async def close(self):
        """No-op for SQLite."""
        pass

    async def get(self, key: str, default: str | None = None) -> str | None:
        """Get a credential by key. Returns default if not found."""
        return self._store.get(key, default)


# ── Config renderer ────────────────────────────────────────────────────────────

async def render(creds) -> str:
    """Render the full Lavalink application.yml from the credential store."""
    spotify_id = await creds.get("spotify.client_id", "")
    spotify_secret = await creds.get("spotify.client_secret", "")
    tidal_token = await creds.get("tidal.access_token") or await creds.get("tidal.api_token") or "none"
    tidal_client_id = await creds.get("tidal.td_client_id") or await creds.get("tidal.client_id") or ""
    tidal_client_secret = await creds.get("tidal.td_client_secret") or await creds.get("tidal.client_secret") or ""
    tidal_country = await creds.get("tidal.country_code", "US")
    tidal_limit = await creds.get("tidal.search_limit", "6")
    ytcipher_token = await creds.get("ytcipher.api_token", "")
    yt_refresh_token = await creds.get("youtube.oauth_refresh_token") or await creds.get("youtube.refresh_token") or ""
    yt_pot_token = await creds.get("youtube.pot_token", "")
    yt_visitor_data = await creds.get("youtube.pot_visitor_data", "")

    # Tidal source enabled if we have either client credentials OR a real token
    has_tidal_creds = bool(tidal_client_id and tidal_client_secret)
    has_tidal_token = bool(tidal_token and tidal_token not in ("none", ""))
    tidal_enabled = "true" if (has_tidal_creds or has_tidal_token) else "false"
    # If tidal disabled, still need a non-empty placeholder to avoid lavasrc crash
    tidal_token_val = tidal_token if has_tidal_token else "disabled"

    # Spotify enabled only if credentials exist
    spotify_enabled = "true" if (spotify_id and spotify_secret) else "false"

    # YouTube OAuth enabled if we have a refresh token
    yt_oauth_enabled = "true" if yt_refresh_token else "false"

    return f"""# Auto-generated by render_lavalink_config.py — DO NOT EDIT
# Credentials sourced from encrypted credential store.

lavalink:
  server:
    host: "0.0.0.0"
    port: 2333
    password: "youshallnotpass"
    pluginsDir: "./plugins"
    sources:
      youtube: false
  buffer:
    period: 500
    periodMilliseconds: 500
  limits:
    memory: 0
    cpu: 0

  plugins:
    # All plugins baked into Lavalink image — no maven downloads needed

plugins:
  youtube:
    enabled: true
    allowSearch: true
    allowDirectVideoIds: true
    allowDirectPlaylistIds: true
    clients:
      - TV
      - TVHTML5_SIMPLY
      - ANDROID_VR
      - MUSIC
      - WEB
    clientOptions:
      MUSIC:
        playback: false
        videoLoading: false
    oauth:
      enabled: {yt_oauth_enabled}
      refreshToken: "{yt_refresh_token}"
    pot:
      token: "{yt_pot_token}"
      visitorData: "{yt_visitor_data}"
    remoteCipher:
      url: "http://yt-cipher.hellodj-service.svc.cluster.local:8001"
      password: "{ytcipher_token}"
      userAgent: "hellodj"
  lavasrc:
    providers:
      - "scsearch:%QUERY%"
      - "ytsearch:\\"%ISRC%\\""
      - "ytsearch:%QUERY%"
    sources:
      spotify: {spotify_enabled}
      tidal: {tidal_enabled}
      youtube: true
    tidal:
      countryCode: "{tidal_country}"
      searchLimit: {tidal_limit}
      clientId: "{tidal_client_id}"
      clientSecret: "{tidal_client_secret}"
      token: "{tidal_token_val}"
    spotify:
      clientId: "{spotify_id}"
      clientSecret: "{spotify_secret}"
      countryCode: "US"
      playlistLoadLimit: 6
      albumLoadLimit: 6
      resolveArtistsInSearch: false

sources:
  youtube:
    enabled: true
  youtubemusic:
    enabled: true
  soundcloud:
    enabled: true
  spotify:
    enabled: {spotify_enabled}

filters:
  enabled: true
  volume:
    enabled: true
  equalizer:
    enabled: true
  karaoke:
    enabled: true
  timescale:
    enabled: true
  tremolo:
    enabled: true
  vibrato:
    enabled: true
  distortion:
    enabled: true
  rotation:
    enabled: true
  lowPass:
    enabled: true
  channelMix:
    enabled: true

server:
  port: 2333
"""


async def async_main(output_path: str):
    """Async entry point for the renderer."""
    # Validate HELLODJ_DB_KEY
    db_key = os.environ.get("HELLODJ_DB_KEY", "")
    if not db_key:
        log.error("HELLODJ_DB_KEY environment variable is required")
        sys.exit(1)

    from cryptography.fernet import Fernet
    fernet = Fernet(_derive_key(db_key))

    # Determine backend: PostgreSQL if HELLODJ_PG_URI is set, else SQLite fallback
    pg_uri = os.environ.get("HELLODJ_PG_URI", "")

    if pg_uri:
        # PostgreSQL path
        reader = PGCredentialReader(pg_uri, fernet)
        try:
            await reader.connect()
        except Exception:
            log.error("Failed to connect to PostgreSQL — init container cannot proceed")
            sys.exit(1)
    else:
        # SQLite fallback (backwards compatibility during migration)
        log.info("HELLODJ_PG_URI not set, falling back to SQLite credential store")
        reader = SQLiteCredentialReader()
        try:
            await reader.connect()
        except Exception as exc:
            log.error("Failed to initialize SQLite credential store: %s", exc)
            sys.exit(1)

    try:
        config = await render(reader)
    finally:
        await reader.close()

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        f.write(config)
    log.info("Rendered Lavalink config to %s", output_path)


def main():
    if len(sys.argv) < 2:
        output_path = "/opt/Lavalink/application.yml"
    else:
        output_path = sys.argv[1]

    asyncio.run(async_main(output_path))


if __name__ == "__main__":
    main()
