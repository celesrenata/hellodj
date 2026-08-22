"""HelloDJ — Web Configuration UI

Provides a web interface for:
- Viewing and editing bot configuration (token, lavalink, spotify, genius)
- Making and restoring backups of all config/session/playlist data
- Viewing current playback status across guilds
- Managing the blacklist

Configs are stored on NFS at the configured mount point.
"""

import os
import json
import shutil
import glob
import logging
import logging.handlers
import traceback
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import asyncio
import aiohttp
from flask import (
    Flask, render_template, jsonify, request, redirect, url_for, flash,
    session, g,
)
from werkzeug.exceptions import HTTPException

# ── Logging: console + rotating file under the config dir (NFS shared) ──
def _setup_logging():
    """Configure console + rotating-file logging. File path from WEBUI_LOG_FILE,
    defaulting to the shared NFS mount <cwd>/config/webui.log."""
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(fmt, datefmt=datefmt)

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    handlers[0].setFormatter(formatter)

    log_file = os.getenv("WEBUI_LOG_FILE", "./config/webui.log")
    try:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)
    except OSError as exc:
        # Fall back to console-only if the file can't be opened (e.g. read-only FS).
        logging.getLogger(__name__).warning("Could not enable file logging to %s: %s", log_file, exc)

    # basicConfig re-applies the formatter to any provided handlers, so all
    # existing loggers (this module's `log`, aiohttp, werkzeug) get timestamps.
    logging.basicConfig(level=logging.INFO, handlers=handlers, format=fmt, datefmt=datefmt)

_setup_logging()
log = logging.getLogger(__name__)
# Full debug logging for the web UI's own logger (requests/responses/errors).
log.setLevel(logging.DEBUG)

# Base directory for all relative paths. Resolving against the process working
# directory lets the app run both in the container (cwd=/app) and locally
# without hardcoding /app.
BASE_DIR = os.getcwd()

app = Flask(__name__)

# ── Full debug logging for every request/response/error ─────────────
# Sensitive keys/headers are redacted before logging so tokens, passwords,
# and authorization headers never reach the log files.
SENSITIVE_SUBSTRINGS = (
    "password", "token", "secret", "apikey", "api_key", "authorization",
    "credential", "cookie", "device_code", "user_code", "code",
)
SENSITIVE_HEADERS = {
    "authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token",
    "proxy-authorization", "x-forwarded-access-token",
}

def _redact(obj):
    """Recursively redact sensitive keys from dicts/lists before logging."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if any(s in kl for s in SENSITIVE_SUBSTRINGS):
                out[k] = "[REDACTED]"
            elif isinstance(v, (dict, list)):
                out[k] = _redact(v)
            else:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [_redact(x) if isinstance(x, (dict, list)) else x for x in obj]
    return obj

def _log_headers(headers):
    """Return a redacted headers mapping for logging."""
    return {
        k: ("[REDACTED]" if k.lower() in SENSITIVE_HEADERS else v)
        for k, v in headers.items()
    }

@app.before_request
def _log_request():
    """Log method, path, query args, JSON/form body, IP, and user-agent."""
    try:
        g._req_start = _time.perf_counter()
        method = request.method
        path = request.path
        remote = request.remote_addr or request.headers.get("X-Forwarded-For", "")
        ua = request.headers.get("User-Agent", "")
        args = _redact(dict(request.args)) if request.args else {}
        log.debug("REQ %s %s remote=%s ua=%s args=%s",
                  method, path, remote, ua, args)
        if method in ("POST", "PUT", "PATCH"):
            if request.is_json:
                log.debug("REQ JSON %s %s body=%s",
                          method, path, _redact(request.get_json(silent=True)))
            elif request.form:
                log.debug("REQ FORM %s %s form=%s",
                          method, path, _redact(dict(request.form)))
        log.debug("REQ HEADERS %s %s headers=%s",
                  method, path, _log_headers(request.headers))
    except Exception as exc:
        log.warning("Could not log request: %s", exc)

@app.after_request
def _log_response(response):
    """Log status code and processing duration; JSON bodies at DEBUG."""
    try:
        start = getattr(g, "_req_start", None)
        dur = (_time.perf_counter() - start) * 1000.0 if start else 0.0
        method = request.method
        path = request.path
        status = response.status_code
        body = None
        if response.mimetype == "application/json":
            # Only buffer/parse small JSON responses to avoid breaking large ones.
            if response.content_length is None or response.content_length < 1_000_000:
                try:
                    parsed = json.loads(response.get_data(as_text=True))
                    body = _redact(parsed)
                except Exception:
                    body = None
        log.debug("RES %s %s status=%s dur=%.2fms body=%s",
                  method, path, status, dur, body)
    except Exception as exc:
        log.warning("Could not log response: %s", exc)
    return response

@app.errorhandler(Exception)
def _handle_exception(exc):
    """Log a full traceback for every unhandled exception."""
    try:
        if isinstance(exc, HTTPException):
            # Let Flask render HTTP errors (404/401/403/...) normally.
            log.debug("HTTP %s %s -> %s", request.method, request.path, exc.code)
            return exc
        log.exception("EXC %s %s: %s", request.method, request.path, exc)
        try:
            args = _redact(dict(request.args)) if request.args else {}
            form = _redact(dict(request.form)) if request.form else None
            json_body = _redact(request.get_json(silent=True)) if request.is_json else None
            log.debug("EXC REQ %s %s args=%s form=%s json=%s",
                      request.method, request.path, args, form, json_body)
            log.debug("EXC TRACEBACK:\n%s", traceback.format_exc())
        except Exception:
            pass
        return jsonify({"error": "Internal server error", "detail": str(exc)}), 500
    except Exception:
        return jsonify({"error": "Internal server error"}), 500

@app.context_processor
def inject_auth():
    """Expose the current session user to all templates."""
    # current_user is a module-level helper defined below; it resolves at render time.
    return {"current_user": current_user()}
# FLASK_SECRET from env; fall back to a generated value persisted in config so
# sessions survive restarts without a configured secret.
if os.getenv("FLASK_SECRET"):
    app.secret_key = os.getenv("FLASK_SECRET")
else:
    secret_file = os.path.join(os.getenv("HELLODJ_DATA_DIR", os.path.join(BASE_DIR, "data")), "flask_secret")
    if os.path.exists(secret_file):
        with open(secret_file, "r", encoding="utf-8") as f:
            app.secret_key = f.read().strip()
    else:
        app.secret_key = secrets.token_hex(32)
        try:
            os.makedirs(os.path.dirname(secret_file), exist_ok=True)
            if not os.path.isdir('./'): os.makedirs('./', exist_ok=True)
            with open(secret_file, "w", encoding="utf-8") as f:
                f.write(app.secret_key)
        except OSError as exc:
            log.warning("Could not persist generated FLASK_SECRET: %s", exc)

# ── Paths ──────────────────────────────────────────────────
# Defaults resolve relative to BASE_DIR (the process working directory).
DATA_DIR = os.getenv("HELLODJ_DATA_DIR", os.path.join(BASE_DIR, "data"))
CONFIG_DIR = os.getenv("HELLODJ_CONFIG_DIR", os.path.join(BASE_DIR, "config"))
BACKUP_DIR = os.getenv("HELLODJ_BACKUP_DIR", os.path.join(BASE_DIR, "config-backups"))

ENV_FILE = os.path.join(DATA_DIR, ".env")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
PLAYLISTS_FILE = os.path.join(DATA_DIR, "playlists.json")
CONFIG_FILE = os.path.join(CONFIG_DIR, "hellodj-config.json")

# Shared OAuth binding store (written here, read by bot/oauth_store.py)
OAUTH_FILE = os.path.join(DATA_DIR, "oauth.json")
# Bot guilds snapshot (written by bot, read here)
GUILDS_FILE = os.path.join(DATA_DIR, "bot_guilds.json")
# YouTube device-flow pending store. Persisted to the shared NFS data dir so
# all gunicorn workers can see the same pending flow (a flow started in one
# worker must be polled by whichever worker handles the next request).
YOUTUBE_FLOWS_FILE = os.path.join(DATA_DIR, "youtube_flows.json")
# AI usage metrics (written by the bot to the shared NFS data dir; read here)
METRICS_FILE = os.path.join(DATA_DIR, "metrics.json")

# ── Helpers ────────────────────────────────────────────────

def ensure_dirs():
    """Ensure all directories exist."""
    for d in [DATA_DIR, CONFIG_DIR, BACKUP_DIR]:
        os.makedirs(d, exist_ok=True)

def read_json(path, default=None):
    """Read a JSON file, returning default if not found."""
    if not os.path.exists(path):
        return default or {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to read %s: %s", path, exc)
        return default or {}

def write_json(path, data):
    """Write a JSON file atomically."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def read_env():
    """Read the .env file as key=value pairs."""
    if not os.path.exists(ENV_FILE):
        return {}
    env = {}
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip()
    return env

def write_env(env: dict):
    """Write key=value pairs to .env AND to the credential store."""
    # Write .env file (legacy compat)
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        for key, val in env.items():
            f.write(f"{key}={val}\n")
    # Also write to credential store (the canonical source going forward)
    _write_env_to_creds(env)


def _write_env_to_creds(env: dict):
    """Sync env key-value pairs to the encrypted credential store."""
    try:
        from credentials import creds
    except Exception as exc:
        log.warning("Credential store unavailable (%s) — .env only", exc)
        return

    # Env var name -> credential store key
    _mapping = {
        "DISCORD_TOKEN": "discord.token",
        "DISCORD_APPID": "discord.app_id",
        "DISCORD_PUBKEY": "discord.public_key",
        "BOT_OWNER_ID": "discord.owner_id",
        "SPOTIFY_CLIENT_ID": "spotify.client_id",
        "SPOTIFY_CLIENT_SECRET": "spotify.client_secret",
        "TIDAL_CLIENT_ID": "tidal.client_id",
        "TIDAL_CLIENT_SECRET": "tidal.client_secret",
        "TIDAL_TOKEN": "tidal.api_token",
        "TIDAL_COUNTRY_CODE": "tidal.country_code",
        "TIDAL_SEARCH_LIMIT": "tidal.search_limit",
        "TIDAL_ENABLED": "tidal.enabled",
        "YOUTUBE_OAUTH_ENABLED": "youtube.oauth_enabled",
        "YOUTUBE_OAUTH_REFRESH_TOKEN": "youtube.oauth_refresh_token",
        "POT_TOKEN": "youtube.pot_token",
        "POT_VISITOR_DATA": "youtube.pot_visitor_data",
        "YTCIPHER_URL": "ytcipher.url",
        "YTCIPHER_API_TOKEN": "ytcipher.api_token",
        "PROVIDER_YOUTUBE": "provider.youtube",
        "PROVIDER_YOUTUBEMUSIC": "provider.youtube_music",
        "PROVIDER_SOUNDCLOUD": "provider.soundcloud",
        "PROVIDER_SPOTIFY": "provider.spotify",
        "PROVIDER_TIDAL": "provider.tidal",
        "PROVIDER_DEEZER": "provider.deezer",
        "PROVIDER_APPLE_MUSIC": "provider.apple_music",
        "LAVALINK_HOST": "lavalink.host",
        "LAVALINK_PORT": "lavalink.port",
        "LAVALINK_PASSWORD": "lavalink.password",
        "LLM_API_URL": "llm.api_url",
        "LLM_API_KEY": "llm.api_key",
        "LLM_MODEL": "llm.model",
        "STT_ENGINE": "stt.engine",
        "STT_API_KEY": "stt.api_key",
        "STT_URL": "stt.url",
        "STT_MODEL_SIZE": "stt.model_size",
        "STT_WHISPER_ENDPOINT": "stt.whisper_endpoint",
        "TTS_ENGINE": "tts.engine",
        "TTS_API_KEY": "tts.api_key",
        "TTS_VOICE": "tts.voice",
        "SPEACHES_URL": "tts.speaches_url",
        "TTS_SPEACHES_ENDPOINT": "tts.speaches_endpoint",
        "KOKORO_URL": "tts.kokoro_url",
        "TTS_KOKORO_ENDPOINT": "tts.kokoro_endpoint",
        "VOICE_ENABLED": "voice.enabled",
        "WAKE_WORD_MODEL_PATH": "voice.wakeword_model",
        "GENIUS_API_KEY": "genius.api_key",
        "GENIUS_CLIENT_ID": "genius.client_id",
        "GENIUS_CLIENT_SECRET": "genius.client_secret",
        "GENIUS_ACCESS_TOKEN": "genius.access_token",
        "NEWS_API_KEY": "news.api_key",
        "STOCKS_API_KEY": "stocks.api_key",
        "DEEZER_ARL": "deezer.arl",
        "DEEZER_MASTER_KEY": "deezer.master_key",
        "APPLE_MUSIC_MEDIA_API_TOKEN": "applemusic.media_api_token",
        "APPLE_MUSIC_COUNTRY_CODE": "applemusic.country_code",
    }

    for env_key, val in env.items():
        db_key = _mapping.get(env_key)
        if db_key and val:
            creds.set(db_key, val)

# ── OAuth binding store ─────────────────────────────────────

def load_oauth():
    """Read the shared OAuth binding store (written here, read by the bot)."""
    return read_json(OAUTH_FILE, {
        "owner_user_id": None,
        "owner_username": None,
        "admin_user_ids": [],
        "discord_token": None,
    })

def save_oauth(data):
    """Persist the OAuth binding store atomically to data/oauth.json AND credential store."""
    os.makedirs(DATA_DIR, exist_ok=True)
    write_json(OAUTH_FILE, data)
    # Sync provider tokens to the encrypted credential store
    try:
        from credentials import creds
        providers = data.get("providers", {})
        for provider, tokens in providers.items():
            if isinstance(tokens, dict):
                for token_key, token_val in tokens.items():
                    if token_val:
                        creds.set(f"{provider}.{token_key}", str(token_val))
    except Exception as exc:
        log.warning("Could not sync oauth tokens to credential store: %s", exc)

def is_owner_bound():
    oauth = load_oauth()
    return bool(oauth.get("owner_user_id"))

def is_owner(user_id):
    oauth = load_oauth()
    owner = oauth.get("owner_user_id")
    if owner is None or user_id is None:
        return False
    return str(user_id) == str(owner)

def is_admin(user_id):
    """True if the session user is the bound owner or a listed admin."""
    oauth = load_oauth()
    if user_id is None:
        return False
    uid = str(user_id)
    owner = oauth.get("owner_user_id")
    if owner is not None and uid == str(owner):
        return True
    for admin_id in oauth.get("admin_user_ids", []) or []:
        if admin_id is not None and uid == str(admin_id):
            return True
    return False

# ── Auth guards ────────────────────────────────────────────

def current_user():
    """Return the current session user id, or None."""
    uid = session.get("user_id")
    if not uid:
        return None
    return str(uid)

def require_auth():
    """Redirect unauthenticated users to the Discord login flow."""
    if current_user() is None:
        return redirect(url_for("auth_login"))
    return None

def require_owner():
    """401 unless the session user is the bound owner (for admin management)."""
    uid = current_user()
    if uid is None:
        return jsonify({"error": "Authentication required"}), 401
    if not is_owner(uid):
        return jsonify({"error": "Owner only"}), 403
    return None

def get_backup_list():
    """List available backups with timestamps."""
    backups = []
    pattern = os.path.join(BACKUP_DIR, "hellodj-backup-*.tar.gz")
    for path in sorted(glob.glob(pattern), reverse=True):
        fname = os.path.basename(path)
        stamp = fname.replace("hellodj-backup-", "").replace(".tar.gz", "")
        backups.append({
            "name": fname,
            "timestamp": stamp,
            "path": path,
            "size": os.path.getsize(path),
        })
    return backups

def create_backup(name=None):
    """Create a backup tarball of all config/data files."""
    timestamp = name or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.join(BACKUP_DIR, f"hellodj-backup-{timestamp}.tar.gz")

    files_to_backup = []
    for d in [DATA_DIR, CONFIG_DIR]:
        if os.path.isdir(d):
            for root, _, files in os.walk(d):
                for f in files:
                    fpath = os.path.join(root, f)
                    # Skip tmp files and backups
                    if not f.endswith(".tmp") and "backup" not in f:
                        files_to_backup.append(fpath)

    # Create tarball
    import tarfile
    with tarfile.open(backup_path, "w:gz") as tar:
        for fpath in files_to_backup:
            arcname = os.path.relpath(fpath, start=DATA_DIR)
            tar.add(fpath, arcname=arcname)

    log.info("Created backup: %s (%d files)", backup_path, len(files_to_backup))
    return backup_path

# ── Routes ─────────────────────────────────────────────────

@app.route("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.ico")

@app.route("/")
def index():
    """Dashboard — login-required."""
    if require_auth():
        return require_auth()
    return render_template("index.html", active="dashboard")

# ── OAuth / Auth ───────────────────────────────────────────

DISCORD_AUTH_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_API_URL = "https://discord.com/api"

# ── OAuth providers registry ───────────────────────────────
# Each provider is described by its OAuth endpoints, scope, label, and the .env
# keys that supply client_id/client_secret. A provider is "configured" only when
# both client_id AND client_secret are present in the .env file.
PROVIDERS = {
    "discord": {
        "auth_url": "https://discord.com/oauth2/authorize",
        "token_url": "https://discord.com/api/oauth2/token",
        "api_url": "https://discord.com/api",
        "scope": "identify",
        "label": "Discord",
        "client_id_env": "DISCORD_APPID",
        "client_secret_env": "DISCORD_CLIENT_SECRET",
        "user_path": "/users/@me",
        "user_id_field": "id",
    },
    "spotify": {
        "auth_url": "https://accounts.spotify.com/authorize",
        "token_url": "https://accounts.spotify.com/api/token",
        "api_url": "https://api.spotify.com/v1",
        "scope": "user-read-private user-read-email",
        "label": "Spotify",
        "client_id_env": "SPOTIFY_CLIENT_ID",
        "client_secret_env": "SPOTIFY_CLIENT_SECRET",
        "user_path": "/me",
        "user_id_field": "id",
    },
    "tidal": {
        "auth_url": "https://login.tidal.com/authorize",
        "token_url": "https://auth.tidal.com/v1/oauth2/token",
        "api_url": "https://api.tidal.com/v1",
        "scope": "collection.read collection.write entitlements.read playback playlists.read playlists.write recommendations.read search.read search.write",
        "label": "Tidal",
        "client_id_env": "TIDAL_CLIENT_ID",
        "client_secret_env": "TIDAL_CLIENT_SECRET",
        "pkce": True,
        "user_path": None,
        "user_id_field": "id",
    },
    "genius": {
        "auth_url": "https://api.genius.com/oauth/authorize",
        "token_url": "https://api.genius.com/token",
        "api_url": "https://api.genius.com",
        "scope": "",
        "label": "Genius",
        "client_id_env": "GENIUS_CLIENT_ID",
        "client_secret_env": "GENIUS_CLIENT_SECRET",
        "user_path": "/account",
        "user_id_field": "id",
    },
    "deezer": {
        "auth_url": "",  # No OAuth redirect flow
        "token_url": "",
        "api_url": "https://api.deezer.com",
        "scope": "",
        "label": "Deezer",
        "client_id_env": "DEEZER_ARL",  # Uses ARL cookie, not client_id
        "client_secret_env": "DEEZER_MASTER_KEY",  # Uses master key, not client_secret
        "user_path": "",
        "user_id_field": "",
    },
    "applemusic": {
        "auth_url": "",  # No OAuth redirect flow
        "token_url": "",
        "api_url": "https://api.music.apple.com/v1",
        "scope": "",
        "label": "Apple Music",
        "client_id_env": "APPLE_MUSIC_MEDIA_API_TOKEN",
        "client_secret_env": "",  # No client secret
        "user_path": "",
        "user_id_field": "",
    },
    # ── YouTube (youtube-source plugin OAuth, device flow) ──────────────
    # The youtube-source plugin (dev.lavalink.youtube:youtube-plugin:1.18.2)
    # authenticates via a Google OAuth **refresh token** using a device flow —
    # there is no redirect-URI / authorization-code flow. The client_id/secret
    # are hardcoded in the plugin jar (verified from the plugin bytecode):
    #   CLIENT_ID   = 861556708454-d6dlm3lh05idd8npek18k6be8ba3oc68.apps.googleusercontent.com
    #   CLIENT_SECRET = SboVhoG9s0rNafixCSGGKXAT
    #   SCOPES      = "http://gdata.youtube.com https://www.googleapis.com/auth/youtube"
    #   DEVICE_URL  = https://www.youtube.com/o/oauth2/device/code
    #   TOKEN_URL   = https://www.youtube.com/o/oauth2/token
    # So the web-ui does NOT need its own client credentials: it talks directly
    # to Google with the plugin's own client id/secret. The refresh token is
    # stored in data/oauth.json under providers.youtube, and the BOT (which
    # shares the same NFS data mount) reads it and pushes it to the running
    # Lavalink's youtube-source REST endpoint (/youtube).
    "youtube": {
        "auth_url": "https://www.youtube.com/o/oauth2/device/code",
        "token_url": "https://www.youtube.com/o/oauth2/token",
        "api_url": "https://www.googleapis.com/youtube/v3",
        "scope": "http://gdata.youtube.com https://www.googleapis.com/auth/youtube",
        "label": "YouTube",
        "client_id": "861556708454-d6dlm3lh05idd8npek18k6be8ba3oc68.apps.googleusercontent.com",
        "client_secret": "SboVhoG9s0rNafixCSGGKXAT",
        "user_path": "/",
        "user_id_field": "id",
    },
}

# ── YouTube device-flow pending state ──────────────────────────────
# The device_code is held server-side (not just in browser JS) so the flow
# survives page reloads and can run up to DEVICE_FLOW_TTL_SECONDS before the
# code expires. The frontend receives a flow_id and can resume polling later.
#
# The store is persisted to the shared NFS data dir (youtube_flows.json) because
# gunicorn runs multiple worker processes; a flow started in one worker must be
# pollable by whichever worker handles the next request. Disk-backed storage
# makes it worker-agnostic, at the cost of a read/write per poll.
import time as _time

DEVICE_FLOW_TTL_SECONDS = int(os.getenv("YOUTUBE_DEVICE_FLOW_TTL", "300"))


def _yt_load_flows() -> dict:
    """Read the disk-backed flow store (dict of flow_id -> flow dict)."""
    return read_json(YOUTUBE_FLOWS_FILE, {})


def _yt_save_flows(flows: dict) -> None:
    """Persist the flow store atomically to youtube_flows.json."""
    os.makedirs(DATA_DIR, exist_ok=True)
    write_json(YOUTUBE_FLOWS_FILE, flows)


def _yt_evict_expired(flows: dict, now: float) -> dict:
    """Return flows with entries older than DEVICE_FLOW_TTL_SECONDS removed."""
    return {
        fid: f for fid, f in flows.items()
        if (now - f.get("_created", 0)) <= DEVICE_FLOW_TTL_SECONDS
    }


def _yt_store_flow(device_code, user_code, verification_url,
                   verification_url_complete) -> str:
    """Persist a pending device flow, evicting stale entries. Returns a flow_id."""
    flows = _yt_evict_expired(_yt_load_flows(), _time.monotonic())
    flow_id = secrets.token_urlsafe(8)
    flows[flow_id] = {
        "device_code": device_code,
        "user_code": user_code,
        "verification_url": verification_url,
        "verification_url_complete": verification_url_complete,
        "_created": _time.monotonic(),
    }
    _yt_save_flows(flows)
    return flow_id


def _yt_get_flow(flow_id: str):
    """Return the pending flow dict (or None) if still within TTL."""
    flows = _yt_evict_expired(_yt_load_flows(), _time.monotonic())
    flow = flows.get(flow_id)
    if flow is None:
        return None
    # Refresh the file (drop expired entries) opportunistically.
    _yt_save_flows(flows)
    return flow


def provider_config(name):
    """Return the registry entry for a provider, or None if unknown."""
    return PROVIDERS.get(name)

def provider_credentials(name):
    """Return (client_id, client_secret) for a provider.

    Reads from the credential store first, falls back to .env file, then
    process environment variables (injected by k8s secrets).
    """
    prov = provider_config(name)
    if prov is None:
        return None, None

    # Try credential store first
    cid, csec = None, None
    try:
        from credentials import creds
        cid = creds.get(f"{name}.client_id") or None
        csec = creds.get(f"{name}.client_secret") or None
    except Exception:
        pass

    # Fall back to .env / os.environ
    if not cid or not csec:
        env = read_env()
        cid_env = prov.get("client_id_env")
        csec_env = prov.get("client_secret_env")
        if not cid:
            cid = ((env.get(cid_env, "") or os.environ.get(cid_env, "")) if cid_env else prov.get("client_id", "")) or None
        if not csec:
            csec = ((env.get(csec_env, "") or os.environ.get(csec_env, "")) if csec_env else prov.get("client_secret", "")) or None

    return cid, csec

def provider_is_configured(name):
    """True when credentials are present for a provider.

    PKCE providers only need client_id; others need both client_id and client_secret.
    """
    prov = provider_config(name)
    cid, csec = provider_credentials(name)
    if prov and prov.get("pkce"):
        return bool(cid)
    return bool(cid and csec)

def oauth_redirect_uri(provider="discord"):
    """Return the callback URI for a provider (same origin as the request).

    The site is served over HTTPS behind a TLS-terminating ingress, so force
    the scheme to https even though the proxied request appears as http://.
    """
    if provider == "discord":
        return url_for("auth_callback", _external=True, _scheme="https")
    return url_for("provider_callback", provider=provider, _external=True, _scheme="https")

def _token_expires_at(expires_in):
    """Return an epoch-seconds expiry timestamp from `expires_in`, or None."""
    if expires_in is None:
        return None
    try:
        return datetime.now(timezone.utc).timestamp() + int(expires_in)
    except (TypeError, ValueError):
        return None

def _refresh_provider_token(provider):
    """Attempt a refresh_token exchange for a provider and persist the result.

    Returns (True, None) on success after updating oauth.json, or
    (False, error_message) on failure. Never raises.
    """
    prov = provider_config(provider)
    if prov is None:
        return False, "Unknown provider"
    oauth = load_oauth()
    entry = (oauth.get("providers") or {}).get(provider)
    if not entry or not entry.get("access_token") or not entry.get("refresh_token"):
        return False, "No stored token or refresh_token"
    client_id, client_secret = provider_credentials(provider)
    if not client_id or not client_secret:
        return False, f"{prov['label']} OAuth not configured"
    refresh_token = entry["refresh_token"]

    async def exchange():
        async with aiohttp.ClientSession() as sess:
            log.debug("OUT POST %s provider=%s (refresh exchange)",
                      prov["token_url"], provider)
            async with sess.post(
                prov["token_url"],
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.debug("OUT POST %s -> status=%s body=%s",
                              prov["token_url"], resp.status, _redact(body))
                    return None, f"Refresh failed ({resp.status}): {body}"
                token_data = await resp.json()
                log.debug("OUT POST %s -> status=%s body=%s",
                          prov["token_url"], resp.status, _redact(token_data))
        return token_data, None

    try:
        token_data, err = asyncio.run(exchange())
    except Exception as exc:
        log.error("Token refresh error (%s): %s", provider, exc)
        return False, f"Refresh error: {exc}"

    if token_data is None:
        return False, err or "Refresh failed"
    access_token = token_data.get("access_token")
    if not access_token:
        return False, "No access_token in refresh response"

    new_refresh = token_data.get("refresh_token")
    expires_at = _token_expires_at(token_data.get("expires_in"))
    entry["access_token"] = access_token
    if new_refresh:
        entry["refresh_token"] = new_refresh
    entry["expires_at"] = expires_at
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    oauth.setdefault("providers", {})[provider] = entry
    save_oauth(oauth)
    return True, None

@app.route("/auth/<provider>/login")
def provider_login(provider):
    """Begin an OAuth authorization-code flow for any registered provider.

    Unknown or unconfigured providers are rejected. Sends the browser to the
    provider's authorize URL with a CSRF-protecting state token stored in the
    session under oauth_state_<provider>.

    For providers that support PKCE (e.g. Tidal), only client_id is needed —
    client_secret is not required.
    """
    prov = provider_config(provider)
    if prov is None:
        return jsonify({"error": f"Unknown provider: {provider}"}), 400
    client_id, _client_secret = provider_credentials(provider)
    if not client_id:
        return jsonify({"error": f"{prov['label']} OAuth is not configured"}), 500

    state = secrets.token_hex(16)
    session[f"oauth_state_{provider}"] = state
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": oauth_redirect_uri(provider),
        "state": state,
    }
    if prov.get("scope"):
        params["scope"] = prov["scope"]

    # PKCE support: generate code_verifier/challenge for providers that need it
    if prov.get("pkce"):
        import hashlib, base64
        code_verifier = secrets.token_urlsafe(64)[:128]
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).decode().rstrip("=")
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
        session[f"oauth_pkce_{provider}"] = code_verifier

    url = f"{prov['auth_url']}?{urlencode(params)}"
    return redirect(url)

@app.route("/auth/login")
def auth_login():
    """Begin the Discord OAuth flow (backward-compatible wrapper)."""
    if current_user() is not None:
        return redirect(url_for("index"))
    return provider_login("discord")

@app.route("/auth/<provider>/callback")
def provider_callback(provider):
    """Exchange an OAuth code for tokens and store them under providers.

    `state` is optional-tolerant: if present it is validated against the session
    token; if absent a warning is logged and the flow proceeds. This accepts
    Discord bot-invite callbacks (which omit state but carry valid code, plus
    guild_id/permissions params). Those params are persisted in a `bot_invite`
    record alongside the per-provider token so the bot can act on them.
    """
    prov = provider_config(provider)
    if prov is None:
        return jsonify({"error": f"Unknown provider: {provider}"}), 400

    code = request.args.get("code")
    state = request.args.get("state")

    if not code:
        return jsonify({"error": "Missing code"}), 400
    if state is not None:
        if state != session.get(f"oauth_state_{provider}"):
            session.pop(f"oauth_state_{provider}", None)
            return jsonify({"error": "Invalid state parameter"}), 400
        session.pop(f"oauth_state_{provider}", None)
    else:
        log.warning(
            "OAuth callback for %s received no state param (code present); "
            "treating as bot-invite callback", provider,
        )

    client_id, client_secret = provider_credentials(provider)
    if not client_id:
        return jsonify({"error": f"{prov['label']} OAuth not configured"}), 500
    # PKCE providers don't require client_secret
    if not client_secret and not prov.get("pkce"):
        return jsonify({"error": f"{prov['label']} OAuth not configured (missing client_secret)"}), 500

    async def exchange():
        async with aiohttp.ClientSession() as sess:
            log.debug("OUT POST %s provider=%s (auth-code exchange)",
                      prov["token_url"], provider)
            token_data = {
                "client_id": client_id,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": oauth_redirect_uri(provider),
            }
            # PKCE: use code_verifier instead of client_secret
            code_verifier = session.pop(f"oauth_pkce_{provider}", None)
            if code_verifier:
                token_data["code_verifier"] = code_verifier
            elif client_secret:
                token_data["client_secret"] = client_secret

            async with sess.post(
                prov["token_url"],
                data=token_data,
            ) as token_resp:
                if token_resp.status != 200:
                    body = await token_resp.text()
                    log.error("Token exchange failed (%s): %s %s", provider, token_resp.status, body)
                    return None, None
                token_data = await token_resp.json()
                log.debug("OUT POST %s -> status=%s body=%s",
                          prov["token_url"], token_resp.status, _redact(token_data))

            access_token = token_data.get("access_token")
            if not access_token:
                return None, None

            user = {}
            if prov.get("user_path"):
                user_url = f"{prov['api_url']}{prov['user_path']}"
                log.debug("OUT GET %s provider=%s (auth header redacted)",
                          user_url, provider)
                async with sess.get(
                    user_url,
                    headers={"Authorization": f"Bearer {access_token}"},
                ) as user_resp:
                    if user_resp.status != 200:
                        body = await user_resp.text()
                        log.error("Failed to fetch user (%s): %s %s", provider, user_resp.status, body)
                        return None, None
                    user = await user_resp.json()
                    log.debug("OUT GET %s -> status=%s body=%s",
                              user_url, user_resp.status, _redact(user))
            return token_data, user

    token_data, user = asyncio.run(exchange())
    if token_data is None:
        return jsonify({"error": "OAuth exchange failed"}), 502

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in")

    oauth = load_oauth()
    # Per-provider token store under a "providers" key.
    providers = oauth.setdefault("providers", {})
    providers[provider] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": _token_expires_at(expires_in),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    # Bot-invite params (Discord): persist guild_id + permissions if present.
    guild_id = request.args.get("guild_id")
    permissions = request.args.get("permissions")
    if guild_id or permissions:
        oauth["bot_invite"] = {
            "provider": provider,
            "guild_id": guild_id,
            "permissions": permissions,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        log.info(
            "Stored bot-invite for %s guild=%s perms=%s",
            provider, guild_id, permissions,
        )

    # Discord-only owner binding (backward compat): the first Discord login
    # binds the owner; later logins are checked against the owner/admin list.
    if provider == "discord" and user:
        user_id = str(user.get("id"))
        username = user.get("username", "")
        owner = oauth.get("owner_user_id")
        if owner is None:
            oauth["owner_user_id"] = user_id
            oauth["owner_username"] = username
            oauth["discord_token"] = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_in": expires_in,
            }
            save_oauth(oauth)
            session["user_id"] = user_id
            session["username"] = username
            log.info("Bound owner %s (%s)", user_id, username)
            return redirect(url_for("index"))

        if is_owner(user_id) or is_admin(user_id):
            session["user_id"] = user_id
            session["username"] = username
            return redirect(url_for("index"))
        save_oauth(oauth)
        return jsonify({"error": "You are not authorized to access this panel"}), 403

    save_oauth(oauth)
    return redirect(url_for("config_page"))

@app.route("/auth/callback")
def auth_callback():
    """Backward-compatible callback wrapper.
    
    Routes to Tidal stream callback if redirect_url is present (Tidal PKCE flow),
    otherwise handles as Discord OAuth callback.
    """
    if request.args.get("redirect_url"):
        return tidal_stream_callback()
    return provider_callback("discord")

@app.route("/auth/logout")
def auth_logout():
    """Clear the Flask session."""
    session.clear()
    return redirect(url_for("index"))


@app.route("/auth/tidal/stream-login")
def tidal_stream_login():
    """Redirect to tidal-stream which redirects to Tidal login."""
    import requests as req
    try:
        resp = req.get(
            "http://hellodj.hellodj-service.svc.cluster.local:8801/auth/login",
            timeout=10,
            allow_redirects=False,
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            return redirect(resp.headers["Location"])
        return resp.text, resp.status_code, {"Content-Type": resp.headers.get("Content-Type", "text/html")}
    except Exception as exc:
        return jsonify({"error": f"Cannot reach tidal-stream service: {exc}"}), 502


@app.route("/auth/tidal/stream-callback")
@app.route("/auth/tidal/callback")
def tidal_stream_callback():
    """Handle Tidal OAuth redirect — pass the full callback URL to tidal-stream."""
    import requests as req

    # Reconstruct the full redirect URL that Tidal sent us.
    # Force https:// since we're behind a TLS-terminating reverse proxy.
    full_redirect_url = request.url.replace("http://", "https://", 1)
    try:
        resp = req.get(
            "http://hellodj.hellodj-service.svc.cluster.local:8801/auth/callback",
            params={"redirect_url": full_redirect_url},
            timeout=30,
        )
        return resp.text, resp.status_code, {"Content-Type": resp.headers.get("Content-Type", "text/html")}
    except Exception as exc:
        return jsonify({"error": f"Tidal auth callback failed: {exc}"}), 502

@app.route("/auth/status")
def auth_status():
    """Return whether an owner is bound and the current session user."""
    oauth = load_oauth()
    uid = current_user()
    return jsonify({
        "owner_bound": is_owner_bound(),
        "owner_username": oauth.get("owner_username"),
        "current_user": uid,
        "authenticated": uid is not None,
        "is_owner": is_owner(uid),
        "is_admin": is_admin(uid),
    })

@app.route("/api/providers/<provider>/refresh", methods=["POST"])
def api_provider_refresh(provider):
    """Attempt a refresh_token exchange for a provider and persist the result.

    400 for unknown providers; 404 when no stored token/refresh_token exists;
    200 on success; 500 on network/exchange error.
    """
    if provider_config(provider) is None:
        return jsonify({"error": f"Unknown provider: {provider}"}), 400
    oauth = load_oauth()
    entry = (oauth.get("providers") or {}).get(provider)
    if not entry or not entry.get("access_token") or not entry.get("refresh_token"):
        return jsonify({"error": "No stored token or refresh_token"}), 404
    ok, err = _refresh_provider_token(provider)
    if not ok:
        return jsonify({"error": err}), 500
    return jsonify({"status": "ok", "message": f"{provider} token refreshed"}), 200

@app.route("/api/providers/status")
def api_providers_status():
    """Report OAuth provider configuration and token status.

    For each registered provider returns whether it is configured (client_id +
    client_secret present), whether a token exists and is expired, and when it
    was last authenticated. Expired tokens are refreshed first; if a refresh
    fails, token_expired=true is reported with the error message.
    Supports the health-strip / provider verification UI.
    """
    now = datetime.now(timezone.utc).timestamp()
    oauth = load_oauth()
    providers_store = oauth.get("providers", {})
    status = {}
    for name, prov in PROVIDERS.items():
        entry = providers_store.get(name)
        token_present = bool(entry and entry.get("access_token"))
        expires_at = entry.get("expires_at") if entry else None
        expired = False
        error = None

        # For Tidal: check the credential store (source of truth for tokens)
        # LavasRC uses client_credentials when clientId+clientSecret are configured,
        # managing its own token lifecycle. The PKCE user token (tidal.access_token)
        # is only used by the tidal-stream direct streaming sidecar.
        if name == "tidal":
            try:
                from credentials import creds as _creds
                tidal_client_id = _creds.get("tidal.client_id", "")
                tidal_client_secret = _creds.get("tidal.client_secret", "")
                tidal_token = _creds.get("tidal.access_token", "")
                tidal_expires = _creds.get("tidal.expires_at", "")
                tidal_updated = _creds.get("tidal.updated_at", "")

                # LavasRC self-manages tokens when both client credentials are present
                has_client_creds = bool(tidal_client_id and tidal_client_secret)

                # PKCE user token status (for direct streaming sidecar)
                pkce_token_present = bool(tidal_token)
                pkce_expires_at = float(tidal_expires) if tidal_expires else None
                pkce_expired = (pkce_expires_at is not None and now > pkce_expires_at) if pkce_token_present else False

                # Overall status: Tidal works if LavasRC has client creds OR a valid PKCE token
                configured = has_client_creds or pkce_token_present
                token_present = has_client_creds or pkce_token_present
                expired = False if has_client_creds else pkce_expired
                error = None
            except Exception as e:
                log.warning("Failed to read Tidal status from credential store: %s", e)
                has_client_creds = False
                pkce_token_present = False
                pkce_expires_at = None
                pkce_expired = False
                token_present = False
                expired = True
                error = str(e)
                configured = False
                tidal_updated = None
            status[name] = {
                "configured": configured,
                "label": prov["label"],
                "token_present": token_present,
                "token_expired": expired,
                "expires_at": pkce_expires_at if not has_client_creds else None,
                "updated_at": tidal_updated,
                "refresh_error": error,
                "mode": "client_credentials" if has_client_creds else "pkce",
            }
            continue

        if token_present and expires_at is not None:
            try:
                expired = now > float(expires_at)
            except (TypeError, ValueError):
                expired = False
            # Auto-refresh an expired token before reporting status.
            if expired:
                ok, err = _refresh_provider_token(name)
                if ok:
                    entry = (load_oauth().get("providers") or {}).get(name)
                    expires_at = entry.get("expires_at") if entry else None
                    expired = False
                else:
                    error = err
        status[name] = {
            "configured": provider_is_configured(name),
            "label": prov["label"],
            "token_present": token_present,
            "token_expired": expired,
            "expires_at": expires_at,
            "updated_at": entry.get("updated_at") if entry else None,
            "refresh_error": error,
        }
    return jsonify({"providers": status})

# ── YouTube device-flow OAuth (youtube-source plugin) ─────────────────
# The youtube-source plugin has NO redirect-URI flow — it authenticates via a
# Google OAuth device flow and consumes an OAuth refresh token. These endpoints
# talk directly to Google with the plugin's own (hardcoded, verified) client
# id/secret, then store the refresh token in data/oauth.json under
# providers.youtube.refresh_token. The bot (sharing the same NFS data mount)
# reads it and pushes it to the running Lavalink's /youtube REST endpoint.

@app.route("/api/youtube/device", methods=["POST"])
def api_youtube_device():
    """Request a Google device code for the YouTube scope.

    Returns a flow_id (the server holds the device_code up to
    DEVICE_FLOW_TTL_SECONDS), the user_code, the verification_url, and a direct
    authorize link (verification_url_complete) that pre-fills the code so the
    user can authorize with a single click instead of typing the code.
    """
    prov = provider_config("youtube")
    if prov is None:
        return jsonify({"error": "YouTube provider not registered"}), 500
    payload = {
        "client_id": prov["client_id"],
        "scope": prov["scope"],
        "device_id": "-",
        "device_model": "ytlr::",
    }

    async def fetch():
        async with aiohttp.ClientSession() as sess:
            log.debug("OUT POST %s payload=%s (youtube device)",
                      prov["auth_url"], _redact(payload))
            async with sess.post(prov["auth_url"], data=payload) as resp:
                body = await resp.json()
                log.debug("OUT POST %s -> status=%s body=%s",
                          prov["auth_url"], resp.status, _redact(body))
                return resp.status, body

    try:
        status, body = asyncio.run(fetch())
    except Exception as exc:
        log.error("YouTube device code fetch error: %s", exc)
        return jsonify({"error": f"Device code fetch error: {exc}"}), 502
    if status != 200:
        return jsonify({"error": f"Device code request failed ({status})", "body": body}), 502

    device_code = body.get("device_code")
    user_code = body.get("user_code")
    verification_url = body.get("verification_url") or "https://www.google.com/device"
    verification_url_complete = body.get("verification_url_complete")
    # Build a direct authorize link even if Google omits the pre-filled variant.
    if not verification_url_complete:
        verification_url_complete = f"{verification_url}?user_code={user_code}"
    flow_id = _yt_store_flow(
        device_code, user_code, verification_url, verification_url_complete
    )
    log.info("YouTube device flow started flow_id=%s ttl=%ds user_code=%s",
             flow_id, DEVICE_FLOW_TTL_SECONDS, user_code)
    return jsonify({
        "flow_id": flow_id,
        "device_code": device_code,
        "user_code": user_code,
        "verification_url": verification_url,
        "verification_url_complete": verification_url_complete,
        "expires_in": body.get("expires_in"),
        "interval": body.get("interval"),
        "ttl": DEVICE_FLOW_TTL_SECONDS,
    })

@app.route("/api/youtube/token", methods=["POST"])
def api_youtube_token():
    """Poll Google with the device_code to obtain the OAuth refresh token.

    The device flow returns the tokens only after the user authorizes at the
    verification URL. On success we persist providers.youtube.refresh_token in
    data/oauth.json so the bot can push it to Lavalink.

    The caller supplies a flow_id (from /api/youtube/device); the device_code is
    held server-side so the flow survives page reloads up to
    DEVICE_FLOW_TTL_SECONDS. Accepts an optional explicit device_code for
    back-compat.
    """
    data = request.get_json(silent=True) or {}
    device_code = data.get("device_code")
    flow_id = data.get("flow_id")
    flow = _yt_get_flow(flow_id) if flow_id else None
    if not device_code and flow:
        device_code = flow["device_code"]
    if not device_code:
        return jsonify({"error": "Missing device_code or flow_id"}), 400
    prov = provider_config("youtube")
    if prov is None:
        return jsonify({"error": "YouTube provider not registered"}), 500
    payload = {
        "client_id": prov["client_id"],
        "client_secret": prov["client_secret"],
        "grant_type": "http://oauth.net/grant_type/device/1.0",
        "code": device_code,
    }

    async def fetch():
        async with aiohttp.ClientSession() as sess:
            log.debug("OUT POST %s payload=%s (youtube token poll)",
                      prov["token_url"], _redact(payload))
            async with sess.post(prov["token_url"], data=payload) as resp:
                body = await resp.json()
                log.debug("OUT POST %s -> status=%s body=%s",
                          prov["token_url"], resp.status, _redact(body))
                return resp.status, body

    try:
        status, body = asyncio.run(fetch())
    except Exception as exc:
        log.error("YouTube token poll error: %s", exc)
        return jsonify({"error": f"Token poll error: {exc}"}), 502
    # Google's device token endpoint returns HTTP 200 with an error field
    # ({"error":"authorization_pending"}) while the user has not yet authorized,
    # so we must check the body's error field regardless of HTTP status.
    err = body.get("error")
    if status != 200 or err:
        # authorization_pending / slow_down / expired_token / invalid_grant are
        # expected while the user has not yet authorized or the code expired.
        return jsonify({
            "error": f"Token poll failed (status={status})",
            "error_code": err or body.get("error_description"),
            "body": body,
        }), 202
    access_token = body.get("access_token")
    refresh_token = body.get("refresh_token")
    if not refresh_token:
        return jsonify({"error": "No refresh_token in device response"}), 502

    oauth = load_oauth()
    providers = oauth.setdefault("providers", {})
    providers["youtube"] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": _token_expires_at(body.get("expires_in")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    save_oauth(oauth)
    log.info("YouTube OAuth refresh token stored in oauth.json (provider=youtube)")
    return jsonify({
        "status": "ok",
        "message": "YouTube OAuth refresh token stored. The bot will push it to Lavalink.",
        "refresh_token": refresh_token,
    })

@app.route("/api/youtube/token/clear", methods=["POST"])
def api_youtube_clear():
    """Clear the stored YouTube refresh token (revoke binding)."""
    oauth = load_oauth()
    providers = oauth.get("providers", {})
    if "youtube" in providers:
        del providers["youtube"]
        save_oauth(oauth)
        return jsonify({"status": "ok", "message": "YouTube OAuth token cleared"})
    return jsonify({"status": "ok", "message": "No YouTube OAuth token stored"})


@app.route("/api/youtube/potoken/generate", methods=["POST"])
def api_youtube_potoken_generate():
    """Generate a fresh YouTube poToken via the potoken-server service.

    Calls the bgutil-ytdlp-pot-provider HTTP server running in the cluster,
    which generates tokens on demand without memory issues.
    """
    import requests as http_requests

    potoken_url = "http://potoken-server.hellodj-service.svc.cluster.local:4416/get_pot"

    try:
        resp = http_requests.post(potoken_url, json={}, timeout=60)
        if resp.status_code != 200:
            log.error("poToken server returned %d: %s", resp.status_code, resp.text[:200])
            return jsonify({
                "error": f"poToken server returned {resp.status_code}",
                "detail": resp.text[:300],
            }), 500

        data = resp.json()
        po_token = data.get("poToken", "")
        visitor_data = data.get("contentBinding") or data.get("visitorData") or ""

        if not po_token:
            return jsonify({
                "error": "Server returned no poToken",
                "detail": json.dumps(data)[:200],
            }), 500

        # Store in credential store
        try:
            from credentials import creds
            creds.set("youtube.pot_token", po_token)
            if visitor_data:
                creds.set("youtube.pot_visitor_data", visitor_data)
        except Exception as exc:
            log.error("Failed to store poToken in credential store: %s", exc)
            return jsonify({"error": f"Storage failed: {exc}"}), 500

        log.info("poToken generated and stored (token=%s... visitor=%s...)",
                 po_token[:20], visitor_data[:20] if visitor_data else "none")
        return jsonify({
            "status": "ok",
            "message": "poToken generated and stored",
            "poToken": po_token[:30] + "...",
            "visitorData": (visitor_data[:30] + "...") if visitor_data else "not provided",
        })

    except http_requests.Timeout:
        return jsonify({"error": "poToken server timed out (60s)"}), 504
    except http_requests.ConnectionError as exc:
        return jsonify({
            "error": "Cannot reach poToken server",
            "detail": str(exc)[:200],
        }), 503
    except Exception as exc:
        log.exception("poToken generation error")
        return jsonify({"error": str(exc)}), 500

# ── Metrics ────────────────────────────────────────────────
# AI usage metrics are written by the bot to data/metrics.json on the shared
# NFS mount. The web UI reads that file directly (separate process) and
# recomputes the summaries here rather than importing bot/metrics.py.

def _metrics_raw() -> dict:
    """Return the raw metrics payload from the bot, or empty defaults."""
    return read_json(METRICS_FILE, {})


def _period_start(period: str) -> float:
    """Epoch start of a period bucket: today | week | month | all."""
    now = datetime.now()
    if period == "today":
        return datetime(now.year, now.month, now.day).timestamp()
    if period == "week":
        monday = now - timedelta(days=now.weekday())
        return datetime(monday.year, monday.month, monday.day).timestamp()
    if period == "month":
        return datetime(now.year, now.month, 1).timestamp()
    if period in ("all", "alltime"):
        return 0.0
    return datetime(now.year, now.month, now.day).timestamp()


def _metrics_summary(period: str = "today") -> dict:
    """Aggregate raw metrics for the requested period (mirrors bot metrics.py)."""
    data = _metrics_raw()
    start = _period_start(period)
    llm = [r for r in data.get("llm", []) if r.get("ts", 0) >= start]
    stt = [r for r in data.get("stt", []) if r.get("ts", 0) >= start]
    tts = [r for r in data.get("tts", []) if r.get("ts", 0) >= start]
    wake = [r for r in data.get("wakeword", []) if r.get("ts", 0) >= start]

    total_tokens = sum(r.get("input_tokens", 0) + r.get("output_tokens", 0) for r in llm)
    total_input = sum(r.get("input_tokens", 0) for r in llm)
    total_output = sum(r.get("output_tokens", 0) for r in llm)
    latency_ms = sum(r.get("latency_ms", 0) for r in llm)

    def _engine_breakdown(records: list) -> dict:
        by_engine: dict = {}
        for r in records:
            eng = r.get("engine", "unknown") or "unknown"
            bucket = by_engine.setdefault(eng, {"calls": 0, "total": 0})
            bucket["calls"] += 1
            bucket["total"] += r.get("duration_ms", 0) or r.get("chars", 0) or 0
        return by_engine

    def _model_breakdown(records: list) -> dict:
        by_model: dict = {}
        for r in records:
            model = r.get("model", "unknown") or "unknown"
            bucket = by_model.setdefault(model, {"calls": 0, "tokens": 0})
            bucket["calls"] += 1
            bucket["tokens"] += r.get("input_tokens", 0) + r.get("output_tokens", 0)
        return by_model

    return {
        "period": period,
        "llm": {
            "calls": len(llm),
            "tokens": total_tokens,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "avg_latency_ms": round(latency_ms / len(llm), 2) if llm else 0,
            "models": _model_breakdown(llm),
        },
        "stt": {
            "calls": len(stt),
            "duration_ms": sum(r.get("duration_ms", 0) for r in stt),
            "engines": _engine_breakdown(stt),
        },
        "tts": {
            "calls": len(tts),
            "chars": sum(r.get("chars", 0) for r in tts),
            "engines": _engine_breakdown(tts),
        },
        "wakeword": {"detections": len(wake)},
    }


def _metrics_daily(days: int = 14) -> list:
    """Per-day usage for the last ``days`` days (oldest first)."""
    data = _metrics_raw()
    today = datetime.now().date()
    start_day = today - timedelta(days=days - 1)
    start_ts = datetime(start_day.year, start_day.month, start_day.day).timestamp()

    buckets = {}
    for i in range(days):
        day = start_day + datetime.timedelta(days=i)
        buckets[day.strftime("%Y-%m-%d")] = {
            "date": "", "llm_calls": 0, "tokens": 0,
            "stt_calls": 0, "tts_calls": 0, "wakewords": 0,
        }

    def _day_key(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

    for r in data.get("llm", []):
        if r.get("ts", 0) < start_ts:
            continue
        b = buckets.setdefault(_day_key(r["ts"]), {
            "date": "", "llm_calls": 0, "tokens": 0,
            "stt_calls": 0, "tts_calls": 0, "wakewords": 0,
        })
        b["llm_calls"] += 1
        b["tokens"] += r.get("input_tokens", 0) + r.get("output_tokens", 0)
    for r in data.get("stt", []):
        if r.get("ts", 0) < start_ts:
            continue
        buckets.setdefault(_day_key(r["ts"]), {
            "date": "", "llm_calls": 0, "tokens": 0,
            "stt_calls": 0, "tts_calls": 0, "wakewords": 0,
        })["stt_calls"] += 1
    for r in data.get("tts", []):
        if r.get("ts", 0) < start_ts:
            continue
        buckets.setdefault(_day_key(r["ts"]), {
            "date": "", "llm_calls": 0, "tokens": 0,
            "stt_calls": 0, "tts_calls": 0, "wakewords": 0,
        })["tts_calls"] += 1
    for r in data.get("wakeword", []):
        if r.get("ts", 0) < start_ts:
            continue
        buckets.setdefault(_day_key(r["ts"]), {
            "date": "", "llm_calls": 0, "tokens": 0,
            "stt_calls": 0, "tts_calls": 0, "wakewords": 0,
        })["wakewords"] += 1

    ordered = []
    for i in range(days):
        day = start_day + datetime.timedelta(days=i)
        key = day.strftime("%Y-%m-%d")
        b = buckets.get(key) or {
            "date": "", "llm_calls": 0, "tokens": 0,
            "stt_calls": 0, "tts_calls": 0, "wakewords": 0,
        }
        b["date"] = key
        ordered.append(b)
    return ordered


@app.route("/metrics")
def metrics_page():
    """Metrics dashboard — login-required."""
    if require_auth():
        return require_auth()
    return render_template("metrics.html", active="metrics")


@app.route("/api/metrics")
def api_metrics():
    """Return aggregated AI usage metrics (auth required)."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    period = request.args.get("period", "today")
    if period not in ("today", "week", "month", "all"):
        period = "today"
    days = request.args.get("days", "14")
    try:
        days = max(1, min(int(days), 90))
    except (TypeError, ValueError):
        days = 14
    return jsonify({
        "summary": _metrics_summary(period),
        "daily": _metrics_daily(days),
        "days": days,
    })


@app.route("/api/status")
def api_status():
    """Return current HelloDJ status overview."""
    sessions = read_json(SESSIONS_FILE, {})
    playlists = read_json(PLAYLISTS_FILE, {})
    env = read_env()
    config = read_json(CONFIG_FILE, {})

    guild_count = len(sessions)
    playlist_count = sum(len(g) for g in playlists.values())
    backup_count = len(get_backup_list())

    return jsonify({
        "guilds_active": guild_count,
        "playlists_total": playlist_count,
        "backups_available": backup_count,
        "env_configured": {
            "discord_token": bool(env.get("DISCORD_TOKEN")),
            "lavalink_host": bool(env.get("LAVALINK_HOST")),
            "spotify": bool(env.get("SPOTIFY_CLIENT_ID")),
            "genius": bool(env.get("GENIUS_API_KEY")),
        },
        "nfs_mounted": os.path.isdir(CONFIG_DIR),
        "data_dir": DATA_DIR,
        "config_dir": CONFIG_DIR,
        "backup_dir": BACKUP_DIR,
    })

# ── Configuration ──────────────────────────────────────────

@app.route("/config")
def config_page():
    """Config page — login-required."""
    if require_auth():
        return require_auth()
    return render_template("config.html", active="config")

@app.route("/api/config")
def api_get_config():
    """Return the full configuration (owner/admin only)."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    env = read_env()
    config = read_json(CONFIG_FILE, {})

    # Merge env into a structured config
    full = {
        "discord": {
            "token": env.get("DISCORD_TOKEN", ""),
            "app_id": env.get("DISCORD_APPID", ""),
            "pub_key": env.get("DISCORD_PUBKEY", ""),
        },
        "lavalink": {
            "host": env.get("LAVALINK_HOST", "losingtime.dpaste.org"),
            "port": int(env.get("LAVALINK_PORT", "2124")),
            "password": env.get("LAVALINK_PASSWORD", "SleepingOnTrains"),
        },
        "spotify": {
            "client_id": env.get("SPOTIFY_CLIENT_ID", ""),
            "client_secret": env.get("SPOTIFY_CLIENT_SECRET", ""),
        },
        "genius": {
            "api_key": env.get("GENIUS_API_KEY", ""),
            "client_id": env.get("GENIUS_CLIENT_ID", ""),
            "client_secret": env.get("GENIUS_CLIENT_SECRET", ""),
            "access_token": env.get("GENIUS_ACCESS_TOKEN", ""),
        },
        "tidal": {
            "client_id": env.get("TIDAL_CLIENT_ID", ""),
            "client_secret": env.get("TIDAL_CLIENT_SECRET", ""),
            "token": env.get("TIDAL_TOKEN", ""),
            "enabled": env.get("TIDAL_ENABLED", ""),
            "country_code": env.get("TIDAL_COUNTRY_CODE", ""),
            "search_limit": env.get("TIDAL_SEARCH_LIMIT", ""),
        },
        "deezer": {
            "arl": env.get("DEEZER_ARL", ""),
            "master_key": env.get("DEEZER_MASTER_KEY", ""),
        },
        "applemusic": {
            "media_api_token": env.get("APPLE_MUSIC_MEDIA_API_TOKEN", ""),
            "country_code": env.get("APPLE_MUSIC_COUNTRY_CODE", "US"),
        },
        "providers": {
            "youtube": env.get("PROVIDER_YOUTUBE", "true"),
            "youtubemusic": env.get("PROVIDER_YOUTUBEMUSIC", "true"),
            "soundcloud": env.get("PROVIDER_SOUNDCLOUD", "true"),
            "spotify": env.get("PROVIDER_SPOTIFY", "true"),
            "tidal": env.get("PROVIDER_TIDAL", "true"),
            "deezer": env.get("PROVIDER_DEEZER", "true"),
            "applemusic": env.get("PROVIDER_APPLE_MUSIC", "true"),
        },
        "voice": {
            "model_path": env.get("WAKE_WORD_MODEL_PATH", os.path.join(BASE_DIR, "models", "Hello_DJ.onnx")),
            "stt_model_size": env.get("STT_MODEL_SIZE", "base"),
            "stt_engine": env.get("STT_ENGINE", "local"),
            "stt_url": env.get("STT_URL", ""),
            "tts_engine": env.get("TTS_ENGINE", "kokoro"),
            "tts_voice": env.get("TTS_VOICE", "af_heart"),
            "speaches_url": env.get("SPEACHES_URL", ""),
            "enabled": env.get("VOICE_ENABLED", "true") == "true",
            "llm_api_url": env.get("LLM_API_URL", "https://api.openai.com/v1"),
            "llm_model": env.get("LLM_MODEL", "gpt-4o-mini"),
            "llm_api_key": env.get("LLM_API_KEY", ""),
            "news_api_key": env.get("NEWS_API_KEY", ""),
            "stocks_api_key": env.get("STOCKS_API_KEY", ""),
        },
        "bot": config.get("bot", {}),
    }
    return jsonify(full)

@app.route("/api/config", methods=["POST"])
def api_update_config():
    """Update configuration values (owner/admin only)."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    data = request.json
    if not data:
        return jsonify({"error": "No data provided"}), 400

    env = read_env()

    # Update env from request
    if "discord" in data:
        d = data["discord"]
        if "token" in d:
            env["DISCORD_TOKEN"] = d["token"]
        if "app_id" in d:
            env["DISCORD_APPID"] = d["app_id"]
        if "pub_key" in d:
            env["DISCORD_PUBKEY"] = d["pub_key"]

    if "lavalink" in data:
        l = data["lavalink"]
        if "host" in l:
            env["LAVALINK_HOST"] = l["host"]
        if "port" in l:
            env["LAVALINK_PORT"] = str(l["port"])
        if "password" in l:
            env["LAVALINK_PASSWORD"] = l["password"]

    if "spotify" in data:
        s = data["spotify"]
        if "client_id" in s:
            env["SPOTIFY_CLIENT_ID"] = s["client_id"]
        if "client_secret" in s:
            env["SPOTIFY_CLIENT_SECRET"] = s["client_secret"]

    if "genius" in data:
        g = data["genius"]
        if "api_key" in g:
            env["GENIUS_API_KEY"] = g["api_key"]
        if "client_id" in g:
            env["GENIUS_CLIENT_ID"] = g["client_id"]
        if "client_secret" in g:
            env["GENIUS_CLIENT_SECRET"] = g["client_secret"]
        if "access_token" in g:
            env["GENIUS_ACCESS_TOKEN"] = g["access_token"]
    if "tidal" in data:
        t = data["tidal"]
        if "client_id" in t:
            env["TIDAL_CLIENT_ID"] = t["client_id"]
        if "client_secret" in t:
            env["TIDAL_CLIENT_SECRET"] = t["client_secret"]
        if "token" in t:
            env["TIDAL_TOKEN"] = t["token"]
        if "enabled" in t:
            env["TIDAL_ENABLED"] = "true" if t["enabled"] else "false"
        if "country_code" in t:
            env["TIDAL_COUNTRY_CODE"] = t["country_code"]
        if "search_limit" in t:
            env["TIDAL_SEARCH_LIMIT"] = str(t["search_limit"])

    if "deezer" in data:
        d = data["deezer"]
        if "arl" in d:
            env["DEEZER_ARL"] = str(d["arl"])
        if "master_key" in d:
            env["DEEZER_MASTER_KEY"] = str(d["master_key"])

    if "applemusic" in data:
        am = data["applemusic"]
        if "media_api_token" in am:
            env["APPLE_MUSIC_MEDIA_API_TOKEN"] = str(am["media_api_token"])
        if "country_code" in am:
            env["APPLE_MUSIC_COUNTRY_CODE"] = str(am["country_code"])

    if "providers" in data:
        p = data["providers"]
        if "youtube" in p:
            env["PROVIDER_YOUTUBE"] = "true" if p["youtube"] else "false"
        if "youtubemusic" in p:
            env["PROVIDER_YOUTUBEMUSIC"] = "true" if p["youtubemusic"] else "false"
        if "soundcloud" in p:
            env["PROVIDER_SOUNDCLOUD"] = "true" if p["soundcloud"] else "false"
        if "spotify" in p:
            env["PROVIDER_SPOTIFY"] = "true" if p["spotify"] else "false"
        if "tidal" in p:
            env["PROVIDER_TIDAL"] = "true" if p["tidal"] else "false"
        if "deezer" in p:
            env["PROVIDER_DEEZER"] = "true" if p["deezer"] else "false"
        if "applemusic" in p:
            env["PROVIDER_APPLE_MUSIC"] = "true" if p["applemusic"] else "false"

    if "voice" in data:
        v = data["voice"]
        if "model_path" in v:
            env["WAKE_WORD_MODEL_PATH"] = v["model_path"]
        if "stt_model_size" in v:
            env["STT_MODEL_SIZE"] = v["stt_model_size"]
        if "stt_engine" in v:
            env["STT_ENGINE"] = v["stt_engine"]
        if "stt_url" in v:
            env["STT_URL"] = v["stt_url"]
        if "tts_engine" in v:
            env["TTS_ENGINE"] = v["tts_engine"]
        if "tts_voice" in v:
            env["TTS_VOICE"] = v["tts_voice"]
        if "speaches_url" in v:
            env["SPEACHES_URL"] = v["speaches_url"]
        if "enabled" in v:
            env["VOICE_ENABLED"] = "true" if v["enabled"] else "false"
        if "llm_api_url" in v:
            env["LLM_API_URL"] = v["llm_api_url"]
        if "llm_model" in v:
            env["LLM_MODEL"] = v["llm_model"]
        if "llm_api_key" in v:
            env["LLM_API_KEY"] = v["llm_api_key"]
        if "news_api_key" in v:
            env["NEWS_API_KEY"] = v["news_api_key"]
        if "stocks_api_key" in v:
            env["STOCKS_API_KEY"] = v["stocks_api_key"]

    write_env(env)

    # Update bot config
    config = read_json(CONFIG_FILE, {})
    if "bot" in data:
        config["bot"] = data["bot"]
    write_json(CONFIG_FILE, config)

    log.info("Configuration updated by web UI")
    return jsonify({"status": "ok", "message": "Configuration saved"})

# ── Sessions / Guilds ──────────────────────────────────────

@app.route("/guilds")
def guilds_page():
    """Guilds page — login-required."""
    if require_auth():
        return require_auth()
    return render_template("guilds.html", active="guilds")

@app.route("/api/guilds")
def api_get_guilds():
    """Return the bot's authoritative guild list from bot_guilds.json.

    Each guild includes its name, icon, member count, channel list,
    activation status, and its unique activation key.
    """
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    guilds_data = read_json(GUILDS_FILE, {})

    try:
        from credentials import creds
    except Exception:
        creds = None

    guilds = []
    for gid, data in guilds_data.items():
        icon = data.get("icon")
        if icon and isinstance(icon, str) and not icon.startswith(("http://", "https://")):
            icon = f"https://cdn.discordapp.com/icons/{gid}/{icon}.webp?size=128"

        activated = False
        activation_key = ""
        if creds:
            activated = creds.get(f"guild.{gid}.activated", "") == "true"
            # Get or generate a unique activation key for this guild
            activation_key = creds.get(f"guild.{gid}.activation_key", "")
            if not activation_key:
                activation_key = secrets.token_urlsafe(16)
                creds.set(f"guild.{gid}.activation_key", activation_key)

        guilds.append({
            "id": gid,
            "name": data.get("name", ""),
            "icon": icon,
            "member_count": data.get("member_count", 0),
            "channels": data.get("channels", []),
            "permissions_ok": data.get("permissions_ok"),
            "missing_permissions": data.get("missing_permissions", []),
            "activated": activated,
            "activation_key": activation_key,
        })
    return jsonify({"guilds": guilds})

@app.route("/api/guilds/<gid>", methods=["DELETE"])
def api_clear_guild(gid):
    """Clear a guild's session (owner/admin only)."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    sessions = read_json(SESSIONS_FILE, {})
    if gid in sessions:
        del sessions[gid]
        write_json(SESSIONS_FILE, sessions)
        return jsonify({"status": "ok", "message": f"Guild {gid} session cleared"})
    return jsonify({"error": "Guild not found"}), 404

@app.route("/api/guilds/<gid>/deactivate", methods=["POST"])
def api_deactivate_guild(gid):
    """Deactivate a guild — revoke its activation and regenerate its key."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    if not is_admin(current_user()):
        return jsonify({"error": "Admin only"}), 403
    try:
        from credentials import creds
        creds.delete(f"guild.{gid}.activated")
        # Regenerate the key so the old one can't be reused
        new_key = secrets.token_urlsafe(16)
        creds.set(f"guild.{gid}.activation_key", new_key)
        log.info("api: guild %s deactivated by user %s (key regenerated)", gid, current_user())
        return jsonify({"status": "ok", "message": f"Guild {gid} deactivated. A new key has been generated."})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

@app.route("/api/guilds/<gid>/reset-session", methods=["POST"])
def api_reset_guild_session(gid):
    """Reset a guild's playback session (clear queue, current track)."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    sessions = read_json(SESSIONS_FILE, {})
    if gid in sessions:
        del sessions[gid]
        write_json(SESSIONS_FILE, sessions)
        log.info("api: guild %s session reset by user %s", gid, current_user())
        return jsonify({"status": "ok", "message": f"Guild {gid} session reset"})
    return jsonify({"status": "ok", "message": f"Guild {gid} had no session to reset"})

# ── Playlists ───────────────────────────────────────────────

@app.route("/playlists")
def playlists_page():
    """Playlists page — login-required."""
    if require_auth():
        return require_auth()
    return render_template("playlists.html", active="playlists")

@app.route("/api/playlists")
def api_get_playlists():
    """Return all playlists grouped by guild."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    playlists = read_json(PLAYLISTS_FILE, {})
    return jsonify({"playlists": playlists})

@app.route("/api/playlists/<gid>/<name>", methods=["DELETE"])
def api_delete_playlist(gid, name):
    """Delete a playlist (owner/admin only)."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    playlists = read_json(PLAYLISTS_FILE, {})
    guild = playlists.get(gid)
    if guild and name in guild:
        del guild[name]
        write_json(PLAYLISTS_FILE, playlists)
        return jsonify({"status": "ok", "message": f"Playlist {name} deleted"})
    return jsonify({"error": "Playlist not found"}), 404

# ── Backups ────────────────────────────────────────────────

@app.route("/backups")
def backups_page():
    """Backups page — login-required."""
    if require_auth():
        return require_auth()
    return render_template("backups.html", active="backups")

@app.route("/api/backups")
def api_get_backups():
    """List all backups."""
    return jsonify({"backups": get_backup_list()})

@app.route("/api/backups", methods=["POST"])
def api_create_backup():
    """Create a new backup (owner/admin only)."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    try:
        path = create_backup()
        fname = os.path.basename(path)
        return jsonify({
            "status": "ok",
            "message": f"Backup {fname} created",
            "backup": fname,
        })
    except Exception as exc:
        log.error("Backup creation failed: %s", exc)
        return jsonify({"error": str(exc)}), 500

@app.route("/api/backups/<name>/restore", methods=["POST"])
def api_restore_backup(name):
    """Restore a backup by name (owner/admin only)."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    import tarfile
    backup_path = os.path.join(BACKUP_DIR, name)
    if not os.path.exists(backup_path):
        return jsonify({"error": f"Backup {name} not found"}), 404

    try:
        # Extract to a temp location first, then copy
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with tarfile.open(backup_path, "r:gz") as tar:
                tar.extractall(path=tmp)

            # Copy extracted files to data dir
            for root, _, files in os.walk(tmp):
                for f in files:
                    src = os.path.join(root, f)
                    dst = os.path.join(DATA_DIR, f)
                    shutil.copy2(src, dst)

        log.info("Restored backup: %s", name)
        return jsonify({"status": "ok", "message": f"Backup {name} restored"})
    except Exception as exc:
        log.error("Backup restore failed: %s", exc)
        return jsonify({"error": str(exc)}), 500

@app.route("/api/backups/<name>", methods=["DELETE"])
def api_delete_backup(name):
    """Delete a backup (owner/admin only)."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    backup_path = os.path.join(BACKUP_DIR, name)
    if os.path.exists(backup_path):
        os.remove(backup_path)
        return jsonify({"status": "ok", "message": f"Backup {name} deleted"})
    return jsonify({"error": f"Backup {name} not found"}), 404

# ── Blacklist ──────────────────────────────────────────────

@app.route("/blacklist")
def blacklist_page():
    """Blacklist page — login-required."""
    if require_auth():
        return require_auth()
    return render_template("blacklist.html", active="blacklist")

# data/blacklist.json is the single source of truth for the blacklist. The bot
# reads this file (bot-side sync is wired up in the bot subtask); the web UI
# writes it here. Shape: { "<guild_id>": [user_id, ...], ... }.
BLACKLIST_FILE = os.path.join(DATA_DIR, "blacklist.json")

@app.route("/api/blacklist")
def api_get_blacklist():
    """Return the blacklist (read from data/blacklist.json)."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    bl = read_json(BLACKLIST_FILE, {})
    return jsonify({"blacklist": bl})

@app.route("/api/blacklist", methods=["POST"])
def api_update_blacklist():
    """Update the blacklist (owner/admin only)."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    data = request.json
    if not data or "blacklist" not in data:
        return jsonify({"error": "No blacklist data provided"}), 400
    write_json(BLACKLIST_FILE, data["blacklist"])
    return jsonify({"status": "ok", "message": "Blacklist updated"})

# ── Admins ─────────────────────────────────────────────────

@app.route("/admins")
def admins_page():
    """Admins management page — owner-only."""
    if require_auth():
        return require_auth()
    if not is_owner(current_user()):
        return jsonify({"error": "Owner only"}), 403
    return render_template("admins.html", active="admins")

@app.route("/api/admins")
def api_get_admins():
    """List the bound owner and all admin user ids (owner/admin only)."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    oauth = load_oauth()
    return jsonify({
        "owner_user_id": oauth.get("owner_user_id"),
        "owner_username": oauth.get("owner_username"),
        "admin_user_ids": oauth.get("admin_user_ids", []),
    })

@app.route("/api/admins", methods=["POST"])
def api_add_admin():
    """Add an admin by Discord user_id (owner only)."""
    if require_owner():
        return require_owner()
    data = request.json or {}
    admin_id = data.get("user_id")
    if not admin_id:
        return jsonify({"error": "user_id is required"}), 400

    oauth = load_oauth()
    admin_ids = oauth.get("admin_user_ids", []) or []
    admin_ids = [str(a) for a in admin_ids]
    if str(admin_id) not in admin_ids:
        admin_ids.append(str(admin_id))
    oauth["admin_user_ids"] = admin_ids
    save_oauth(oauth)
    log.info("Added admin %s", admin_id)
    return jsonify({"status": "ok", "message": f"Admin {admin_id} added"})

@app.route("/api/admins/<admin_id>", methods=["DELETE"])
def api_remove_admin(admin_id):
    """Remove an admin by Discord user_id (owner only)."""
    if require_owner():
        return require_owner()
    oauth = load_oauth()
    admin_ids = oauth.get("admin_user_ids", []) or []
    oauth["admin_user_ids"] = [a for a in admin_ids if str(a) != str(admin_id)]
    save_oauth(oauth)
    log.info("Removed admin %s", admin_id)
    return jsonify({"status": "ok", "message": f"Admin {admin_id} removed"})

# ── NFS Status ─────────────────────────────────────────────

@app.route("/api/nfs-status")
def api_nfs_status():
    """Check NFS mount status."""
    info = {
        "config_dir": CONFIG_DIR,
        "data_dir": DATA_DIR,
        "config_exists": os.path.isdir(CONFIG_DIR),
        "data_exists": os.path.isdir(DATA_DIR),
        "config_writable": os.access(CONFIG_DIR, os.W_OK) if os.path.isdir(CONFIG_DIR) else False,
        "data_writable": os.access(DATA_DIR, os.W_OK) if os.path.isdir(DATA_DIR) else False,
        "config_contents": [],
        "nfs_mount_info": "",
    }

    # List config directory
    if os.path.isdir(CONFIG_DIR):
        for entry in sorted(os.listdir(CONFIG_DIR)):
            info["config_contents"].append(entry)

    # Check if it's an NFS mount
    try:
        import subprocess
        result = subprocess.run(["stat", "-f", CONFIG_DIR], capture_output=True, text=True, timeout=5)
        info["nfs_mount_info"] = result.stdout.strip()
    except Exception:
        pass

    return jsonify(info)

# ── Logs ───────────────────────────────────────────────────

WEBUI_LOG_PATH = os.getenv("WEBUI_LOG_FILE", os.path.join(BASE_DIR, "config", "webui.log"))
BOT_LOG_PATH = os.getenv("BOT_LOG_FILE", os.path.join(BASE_DIR, "config", "bot.log"))

def tail_file(path, lines):
    """Return the last `lines` lines of a text file efficiently."""
    result = []
    try:
        with open(path, "rb") as f:
            # Start at end and walk backwards to gather up to `lines` newline
            # delimited records.
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 4096
            buf = b""
            pos = size
            newlines = 0
            while pos > 0 and newlines < lines:
                take = min(block, pos)
                pos -= take
                f.seek(pos)
                chunk = f.read(take)
                buf = chunk + buf
                newlines += buf.count(b"\n")
            # Split the buffered tail into lines and keep the last `lines`.
            data = buf.decode("utf-8", errors="replace")
            parts = data.splitlines()
            result = parts[-lines:] if len(parts) > lines else parts
    except OSError as exc:
        log.warning("Could not read log file %s: %s", path, exc)
        result = []
    return result

@app.route("/logs")
def logs_page():
    """Logs viewer page — login-required."""
    if require_auth():
        return require_auth()
    return render_template("logs.html", active="logs")

@app.route("/api/logs")
def api_get_logs():
    """Return the tail of the webui and bot log files as JSON."""
    lines = request.args.get("lines", default=200, type=int)
    lines = max(1, min(lines, 2000))
    return jsonify({
        "webui": tail_file(WEBUI_LOG_PATH, lines),
        "bot": tail_file(BOT_LOG_PATH, lines),
    })

# ── Instances ──────────────────────────────────────────────

@app.route("/instances")
def instances_page():
    """Bot Instances management page — login-required."""
    if require_auth():
        return require_auth()
    return render_template("instances.html", active="instances")


@app.route("/api/instances")
def api_get_instances():
    """List all configured bot instances with status.

    Reads instance credentials from the credential store and returns
    index, display name, app_id, and status for each.
    """
    guard = require_auth()
    if guard:
        return guard

    try:
        from credentials import creds
    except Exception as exc:
        return jsonify({"error": f"Credential store unavailable: {exc}"}), 500

    count = creds.get_int("playback.instance_count", 0)
    instances = []

    for i in range(count):
        prefix = f"instance.{i}"
        token = creds.get(f"{prefix}.token")
        app_id = creds.get(f"{prefix}.app_id", "")
        name = creds.get(f"{prefix}.name", f"HelloDJ #{i + 2}")
        # Status comes from the live status endpoint; here we report config state
        status = "available" if token else "unknown"
        channel_id = None  # Live data from orchestrator — not persisted

        if token:
            instances.append({
                "index": i,
                "name": name,
                "app_id": app_id,
                "status": status,
                "channel_id": channel_id,
                "guild_id": None,
            })

    return jsonify({"instances": instances, "count": count})


@app.route("/api/instances", methods=["POST"])
def api_add_instance():
    """Add a new bot instance to the credential store.

    Expects JSON body: { "token": "...", "app_id": "...", "name": "..." }
    Appends the instance at the next available index and increments instance_count.
    """
    guard = require_auth()
    if guard:
        return guard
    if not is_admin(current_user()):
        return jsonify({"error": "Admin access required"}), 403

    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    app_id = (data.get("app_id") or "").strip()
    name = (data.get("name") or "").strip()

    if not token:
        return jsonify({"error": "Token is required"}), 400
    if not app_id:
        return jsonify({"error": "Application ID is required"}), 400

    # Validate app_id looks like a snowflake
    if not app_id.isdigit() or len(app_id) < 17:
        return jsonify({"error": "Application ID must be a valid Discord snowflake (17+ digits)"}), 400

    try:
        from credentials import creds
    except Exception as exc:
        return jsonify({"error": f"Credential store unavailable: {exc}"}), 500

    # Determine the next index
    count = creds.get_int("playback.instance_count", 0)
    # Cap at 10 instances
    if count >= 10:
        return jsonify({"error": "Maximum of 10 instances reached"}), 400

    new_index = count
    default_name = name or f"HelloDJ #{new_index + 2}"

    # Store credentials
    prefix = f"instance.{new_index}"
    creds.set(f"{prefix}.token", token)
    creds.set(f"{prefix}.app_id", app_id)
    creds.set(f"{prefix}.name", default_name)

    # Increment instance count
    creds.set("playback.instance_count", str(count + 1))

    log.info("Added bot instance %d (%s) via web UI", new_index, default_name)
    return jsonify({
        "status": "ok",
        "message": f"Instance '{default_name}' added at index {new_index}.",
        "index": new_index,
    })


@app.route("/api/instances/<int:index>", methods=["DELETE"])
def api_remove_instance(index):
    """Remove a bot instance by index.

    Deletes the instance's credentials from the store and decrements instance_count.
    If the removed instance is not the last, shifts subsequent instances down.
    """
    guard = require_auth()
    if guard:
        return guard
    if not is_admin(current_user()):
        return jsonify({"error": "Admin access required"}), 403

    try:
        from credentials import creds
    except Exception as exc:
        return jsonify({"error": f"Credential store unavailable: {exc}"}), 500

    count = creds.get_int("playback.instance_count", 0)
    if index < 0 or index >= count:
        return jsonify({"error": f"Instance index {index} out of range (0–{count - 1})"}), 404

    # Get the name before removing for the response message
    name = creds.get(f"instance.{index}.name", f"Instance #{index}")

    # Remove the instance credentials
    prefix = f"instance.{index}"
    for suffix in ("token", "app_id", "name"):
        creds.delete(f"{prefix}.{suffix}")

    # Shift subsequent instances down to fill the gap
    for i in range(index + 1, count):
        src_prefix = f"instance.{i}"
        dst_prefix = f"instance.{i - 1}"
        for suffix in ("token", "app_id", "name"):
            val = creds.get(f"{src_prefix}.{suffix}")
            if val:
                creds.set(f"{dst_prefix}.{suffix}", val)
            creds.delete(f"{src_prefix}.{suffix}")

    # Decrement count
    new_count = max(0, count - 1)
    creds.set("playback.instance_count", str(new_count))

    log.info("Removed bot instance %d (%s) via web UI, new count=%d", index, name, new_count)
    return jsonify({
        "status": "ok",
        "message": f"Instance '{name}' removed. {new_count} instance(s) remaining.",
    })


@app.route("/api/instances/status")
def api_instances_status():
    """Return live health/status of all instances.

    Returns each instance's current status and channel assignments.
    In production, this will query the InstanceOrchestrator's in-memory state
    via an IPC mechanism. For now, returns config-based data with placeholder
    status.
    """
    guard = require_auth()
    if guard:
        return guard

    try:
        from credentials import creds
    except Exception as exc:
        return jsonify({"error": f"Credential store unavailable: {exc}"}), 500

    count = creds.get_int("playback.instance_count", 0)
    instances = []
    assignments = []

    for i in range(count):
        prefix = f"instance.{i}"
        token = creds.get(f"{prefix}.token")
        if not token:
            continue

        app_id = creds.get(f"{prefix}.app_id", "")
        name = creds.get(f"{prefix}.name", f"HelloDJ #{i + 2}")

        # Live status would come from orchestrator IPC — for now, report as available
        # The orchestrator runs in the bot container; the web-ui reads a shared
        # status file or will use a future IPC channel.
        status_file = os.path.join(DATA_DIR, "instance_status.json")
        live_status = "available"
        channel_id = None
        guild_id = None

        # Try to read live status from shared data volume
        try:
            if os.path.exists(status_file):
                status_data = read_json(status_file, {})
                inst_status = status_data.get(str(i), {})
                live_status = inst_status.get("status", "available")
                channel_id = inst_status.get("channel_id")
                guild_id = inst_status.get("guild_id")
        except Exception:
            pass

        inst_info = {
            "index": i,
            "name": name,
            "app_id": app_id,
            "status": live_status,
            "channel_id": channel_id,
            "guild_id": guild_id,
        }
        instances.append(inst_info)

        # Track assignments (instances currently connected to channels)
        if live_status == "connected" and channel_id:
            assignments.append({
                "index": i,
                "instance_name": name,
                "guild_id": guild_id,
                "channel_id": channel_id,
                "status": live_status,
            })

    return jsonify({
        "instances": instances,
        "assignments": assignments,
        "total": count,
        "available": sum(1 for inst in instances if inst["status"] == "available"),
        "connected": sum(1 for inst in instances if inst["status"] == "connected"),
        "unhealthy": sum(1 for inst in instances if inst["status"] == "unhealthy"),
    })


# ── Moderation (Content Filters & User Bans) ──────────────

# File paths for moderation data (shared via NFS data volume with the bot)
CONTENT_FILTERS_FILE = os.path.join(DATA_DIR, "content_filters.json")
USER_BANS_FILE = os.path.join(DATA_DIR, "user_bans.json")


@app.route("/moderation")
def moderation_page():
    """Moderation management page — login-required, admin-only."""
    if require_auth():
        return require_auth()
    return render_template("moderation.html", active="moderation")


@app.route("/api/moderation/<int:guild_id>/filters")
def api_get_filters(guild_id):
    """Return all content filter rules for a guild."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    if not is_admin(current_user()):
        return jsonify({"error": "Admin access required"}), 403

    data = read_json(CONTENT_FILTERS_FILE, {})
    guild_data = data.get(str(guild_id), {})
    rules = guild_data.get("rules", [])
    return jsonify({"rules": rules, "guild_id": guild_id})


@app.route("/api/moderation/<int:guild_id>/filters", methods=["POST"])
def api_add_filter(guild_id):
    """Add a content filter rule to a guild.

    Expects JSON: {"type": "artist|track|domain|keyword", "value": "..."}
    """
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    if not is_admin(current_user()):
        return jsonify({"error": "Admin access required"}), 403

    body = request.get_json(silent=True) or {}
    rule_type = body.get("type", "").strip()
    value = body.get("value", "").strip()

    valid_types = {"artist", "track", "domain", "keyword"}
    if rule_type not in valid_types:
        return jsonify({"error": f"Invalid type. Must be one of: {', '.join(sorted(valid_types))}"}), 400
    if not value:
        return jsonify({"error": "Value is required"}), 400

    import uuid as _uuid
    rule_id = str(_uuid.uuid4())
    rule = {
        "id": rule_id,
        "type": rule_type,
        "value": value,
        "added_by": int(current_user()),
        "added_at": datetime.now(timezone.utc).isoformat(),
    }

    data = read_json(CONTENT_FILTERS_FILE, {})
    gid_str = str(guild_id)
    if gid_str not in data:
        data[gid_str] = {"rules": []}
    data[gid_str]["rules"].append(rule)
    write_json(CONTENT_FILTERS_FILE, data)

    log.info("Moderation: added %s filter %r for guild %s by user %s",
             rule_type, value, guild_id, current_user())
    return jsonify({"status": "ok", "rule_id": rule_id})


@app.route("/api/moderation/<int:guild_id>/filters/<rule_id>", methods=["DELETE"])
def api_delete_filter(guild_id, rule_id):
    """Remove a content filter rule by its ID."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    if not is_admin(current_user()):
        return jsonify({"error": "Admin access required"}), 403

    data = read_json(CONTENT_FILTERS_FILE, {})
    gid_str = str(guild_id)
    guild_data = data.get(gid_str)
    if guild_data is None:
        return jsonify({"error": "No filters found for this guild"}), 404

    rules = guild_data.get("rules", [])
    for i, rule in enumerate(rules):
        if rule.get("id") == rule_id:
            rules.pop(i)
            # Clean up empty guild entries
            if not rules:
                del data[gid_str]
            else:
                data[gid_str]["rules"] = rules
            write_json(CONTENT_FILTERS_FILE, data)
            log.info("Moderation: removed filter %s from guild %s by user %s",
                     rule_id, guild_id, current_user())
            return jsonify({"status": "ok"})

    return jsonify({"error": "Rule not found"}), 404


@app.route("/api/moderation/<int:guild_id>/bans")
def api_get_bans(guild_id):
    """Return all banned users for a guild."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    if not is_admin(current_user()):
        return jsonify({"error": "Admin access required"}), 403

    data = read_json(USER_BANS_FILE, {})
    guild_data = data.get(str(guild_id), {})
    bans = guild_data.get("banned_users", [])
    return jsonify({"bans": bans, "guild_id": guild_id})


@app.route("/api/moderation/<int:guild_id>/bans", methods=["POST"])
def api_add_ban(guild_id):
    """Ban a user from playback in a guild.

    Expects JSON: {"user_id": 123456789}
    """
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    if not is_admin(current_user()):
        return jsonify({"error": "Admin access required"}), 403

    body = request.get_json(silent=True) or {}
    user_id = body.get("user_id")

    if not user_id or not isinstance(user_id, int):
        return jsonify({"error": "user_id (integer) is required"}), 400

    data = read_json(USER_BANS_FILE, {})
    gid_str = str(guild_id)
    if gid_str not in data:
        data[gid_str] = {"banned_users": []}

    banned_users = data[gid_str]["banned_users"]

    # Check if already banned
    for entry in banned_users:
        if entry.get("user_id") == user_id:
            return jsonify({"error": "User is already banned"}), 409

    banned_users.append({
        "user_id": user_id,
        "banned_by": int(current_user()),
        "banned_at": datetime.now(timezone.utc).isoformat(),
    })
    write_json(USER_BANS_FILE, data)

    log.info("Moderation: banned user %s in guild %s by user %s",
             user_id, guild_id, current_user())
    return jsonify({"status": "ok"})


@app.route("/api/moderation/<int:guild_id>/bans/<int:user_id>", methods=["DELETE"])
def api_delete_ban(guild_id, user_id):
    """Remove a user ban."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    if not is_admin(current_user()):
        return jsonify({"error": "Admin access required"}), 403

    data = read_json(USER_BANS_FILE, {})
    gid_str = str(guild_id)
    guild_data = data.get(gid_str)
    if guild_data is None:
        return jsonify({"error": "No bans found for this guild"}), 404

    banned_users = guild_data.get("banned_users", [])
    for i, entry in enumerate(banned_users):
        if entry.get("user_id") == user_id:
            banned_users.pop(i)
            # Clean up empty guild entries
            if not banned_users:
                del data[gid_str]
            else:
                data[gid_str]["banned_users"] = banned_users
            write_json(USER_BANS_FILE, data)
            log.info("Moderation: unbanned user %s in guild %s by user %s",
                     user_id, guild_id, current_user())
            return jsonify({"status": "ok"})

    return jsonify({"error": "User not found in ban list"}), 404


# ── Main ───────────────────────────────────────────────────

if __name__ == "__main__":
    ensure_dirs()
    app.run(host="0.0.0.0", port=8080, debug=True)
