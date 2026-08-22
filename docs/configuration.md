# Configuration

## Credential Store (`credentials.py`)

All secrets and settings live in an encrypted SQLite database at `/app/data/hellodj.db`.

### Encryption

- Algorithm: Fernet (symmetric, AES-128-CBC + HMAC-SHA256)
- Key derivation: SHA-256 of `HELLODJ_DB_KEY` passphrase → base64-encoded 32-byte key
- The `HELLODJ_DB_KEY` environment variable is the ONLY secret that must be provided externally

### Database Schema

```sql
CREATE TABLE credentials (
    key        TEXT PRIMARY KEY,
    value      BLOB NOT NULL,      -- Fernet-encrypted
    updated_at TEXT DEFAULT (datetime('now'))
);
```

SQLite settings: WAL journal mode, 5000ms busy timeout, thread-local connections.

### API (`from credentials import creds`)

```python
creds.get("key")                    # → str | None
creds.get("key", "default")        # → str
creds.set("key", "value")          # Encrypt + upsert
creds.delete("key")                # Remove
creds.get_prefix("tidal.")         # → {"tidal.client_id": "...", ...}
creds.get_bool("key", False)       # → bool
creds.get_int("key", 0)            # → int
creds.get_float("key", 0.0)        # → float
creds.exists("key")                # → bool
creds.keys("prefix")               # → list[str]
```

### Read-Only Mode

The init container mounts `/app/data` read-only. `CredentialStore(read_only=True)` uses `immutable=1` SQLite URI to skip WAL/SHM file access.

## Config Accessor (`config.py`)

Unified configuration interface: `from config import cfg`

```python
cfg("discord.token")               # Read from credential store
cfg("lavalink.host", "localhost")  # With default
cfg.bool("provider.youtube")       # Boolean accessor
cfg.int("tidal.search_limit", 6)   # Integer accessor
cfg.float("crossfade", 0.0)        # Float accessor
cfg.set("key", "value")            # Write to store
```

**Design:** `cfg()` reads EXCLUSIVELY from the SQLite credential store. No env var fallback in production. The `_KEY_TO_ENV` mapping exists only for documentation and migration tooling.

## Key Mapping

Complete mapping of credential store keys to legacy env var names:

### Discord
| Store Key | Env Var | Purpose |
|-----------|---------|---------|
| `discord.token` | `DISCORD_TOKEN` | Bot login token |
| `discord.app_id` | `DISCORD_APPID` | Application ID |
| `discord.public_key` | `DISCORD_PUBKEY` | Interaction verification |
| `discord.owner_id` | `BOT_OWNER_ID` | Bot owner user ID |

### YouTube
| Store Key | Env Var | Purpose |
|-----------|---------|---------|
| `youtube.oauth_refresh_token` | `YOUTUBE_OAUTH_REFRESH_TOKEN` | TV client OAuth |
| `youtube.pot_token` | `POT_TOKEN` | Proof-of-Origin token |
| `youtube.pot_visitor_data` | `POT_VISITOR_DATA` | PoToken visitor data |

### Spotify
| Store Key | Env Var | Purpose |
|-----------|---------|---------|
| `spotify.client_id` | `SPOTIFY_CLIENT_ID` | LavasRC Spotify source |
| `spotify.client_secret` | `SPOTIFY_CLIENT_SECRET` | LavasRC Spotify source |

### Tidal
| Store Key | Env Var | Purpose |
|-----------|---------|---------|
| `tidal.client_id` | `TIDAL_CLIENT_ID` | LavasRC Tidal config |
| `tidal.client_secret` | `TIDAL_CLIENT_SECRET` | LavasRC Tidal config |
| `tidal.access_token` | — | Runtime access token |
| `tidal.refresh_token` | — | Refresh token (PKCE-issued) |
| `tidal.issuing_client_id` | — | Client that issued the refresh token |
| `tidal.expires_at` | — | Token expiry timestamp |

### Lavalink
| Store Key | Env Var | Purpose |
|-----------|---------|---------|
| `lavalink.host` | `LAVALINK_HOST` | Lavalink server host |
| `lavalink.port` | `LAVALINK_PORT` | Lavalink server port |
| `lavalink.password` | `LAVALINK_PASSWORD` | Lavalink auth |

### Voice / AI
| Store Key | Env Var | Purpose |
|-----------|---------|---------|
| `voice.enabled` | `VOICE_ENABLED` | Master voice activation switch |
| `stt.engine` | `STT_ENGINE` | STT backend (local/speaches/bedrock) |
| `tts.engine` | `TTS_ENGINE` | TTS backend (speaches/kokoro/polly) |
| `llm.api_url` | `LLM_API_URL` | LLM endpoint URL |
| `llm.api_key` | `LLM_API_KEY` | LLM API key |
| `llm.model` | `LLM_MODEL` | LLM model name |

### Multi-Instance
| Store Key | Purpose |
|-----------|---------|
| `playback.instance_count` | Number of secondary bot instances |
| `instance.<N>.token` | Discord token for instance N |
| `instance.<N>.app_id` | Application ID for instance N |
| `instance.<N>.name` | Display name for instance N |

### Guild Activation
| Store Key | Purpose |
|-----------|---------|
| `guild.<id>.activated` | "true" if guild has entered activation key |

## Environment Variables (Non-Secret)

These are set via `bot-configmap.yaml` (ConfigMap):

| Variable | Default | Purpose |
|----------|---------|---------|
| `WAKE_WORD_MODEL_PATH` | /app/models/Hello_DJ.onnx | Wake word model location |
| `VOICE_ENABLED` | true | Auto-listen in all guilds |
| `HELLODJ_DEBUG` | 1 | Debug framework master switch |
| `HELLODJ_DEBUG_MODULES` | * | Debug module filter |
| `HELLODJ_DEBUG_LEVEL` | DEBUG | Debug output level |
| `HELLODJ_VOICE_DEBUG` | 1 | Voice connect debug layer |
| `POTOKEN_SERVER_URL` | (cluster URL) | bgutil PoToken server |
| `POTOKEN_REFRESH_INTERVAL` | 3600 | PoToken refresh interval (seconds) |
| `TIDAL_STREAM_URL` | http://localhost:8801 | Tidal sidecar URL |
| `SPOTIFY_STREAM_URL` | http://localhost:8802 | Spotify sidecar URL |

## Migration Tool (`migrate_to_db.py`)

Migrates secrets from environment variables / `.env` files into the encrypted database:

```bash
HELLODJ_DB_KEY=<key> python migrate_to_db.py
```

Reads `_KEY_TO_ENV` mapping and imports any set env vars into the credential store.
