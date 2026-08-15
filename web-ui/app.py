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
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import asyncio
import aiohttp
from flask import (
    Flask, render_template, jsonify, request, redirect, url_for, flash,
    session,
)

# ── Logging: console + rotating file under the config dir (NFS shared) ──
def _setup_logging():
    """Configure console + rotating-file logging. File path from WEBUI_LOG_FILE,
    defaulting to the shared NFS mount <cwd>/config/webui.log."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    log_file = os.getenv("WEBUI_LOG_FILE", "./config/webui.log")
    try:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=3,
                encoding="utf-8",
            )
        )
    except OSError as exc:
        # Fall back to console-only if the file can't be opened (e.g. read-only FS).
        logging.getLogger(__name__).warning("Could not enable file logging to %s: %s", log_file, exc)

    logging.basicConfig(level=logging.INFO, handlers=handlers)

_setup_logging()
log = logging.getLogger(__name__)

# Base directory for all relative paths. Resolving against the process working
# directory lets the app run both in the container (cwd=/app) and locally
# without hardcoding /app.
BASE_DIR = os.getcwd()

app = Flask(__name__)

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
    """Write key=value pairs to .env."""
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        for key, val in env.items():
            f.write(f"{key}={val}\n")

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
    """Persist the OAuth binding store atomically to data/oauth.json."""
    os.makedirs(DATA_DIR, exist_ok=True)
    write_json(OAUTH_FILE, data)

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

def oauth_redirect_uri():
    """Return the callback URI (same origin as the request).

    The site is served over HTTPS behind a TLS-terminating ingress, so force
    the scheme to https even though the proxied request appears as http://.
    """
    return url_for("auth_callback", _external=True, _scheme="https")

def discord_credentials():
    env = read_env()
    return env.get("DISCORD_APPID", ""), env.get("DISCORD_CLIENT_SECRET", "")

@app.route("/auth/login")
def auth_login():
    """Begin the OAuth authorization-code flow.

    If an owner is already bound, redirect to the dashboard. Otherwise send the
    browser to Discord with a CSRF-protecting state token stored in the session.
    """
    if current_user() is not None:
        return redirect(url_for("index"))

    client_id, _client_secret = discord_credentials()
    if not client_id:
        return jsonify({"error": "DISCORD_APPID is not configured"}), 500

    state = secrets.token_hex(16)
    session["oauth_state"] = state
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": oauth_redirect_uri(),
        "scope": "identify",
        "state": state,
    }
    url = f"{DISCORD_AUTH_URL}?{urlencode(params)}"
    return redirect(url)

@app.route("/auth/callback")
def auth_callback():
    """Exchange the code for tokens, fetch the user, and bind the owner.

    Only the first caller becomes the owner (owner_user_id). Later logins are
    checked against the owner/admin list before granting a session.
    """
    code = request.args.get("code")
    state = request.args.get("state")

    if not code or not state:
        return jsonify({"error": "Missing code or state"}), 400
    if state != session.get("oauth_state"):
        session.pop("oauth_state", None)
        return jsonify({"error": "Invalid state parameter"}), 400
    session.pop("oauth_state", None)

    client_id, client_secret = discord_credentials()
    if not client_id or not client_secret:
        return jsonify({"error": "Discord OAuth not configured"}), 500

    async def exchange():
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                DISCORD_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": oauth_redirect_uri(),
                },
            ) as token_resp:
                if token_resp.status != 200:
                    body = await token_resp.text()
                    log.error("Token exchange failed: %s %s", token_resp.status, body)
                    return None, None
                token_data = await token_resp.json()

            access_token = token_data.get("access_token")
            if not access_token:
                return None, None

            async with sess.get(
                f"{DISCORD_API_URL}/users/@me",
                headers={"Authorization": f"Bearer {access_token}"},
            ) as user_resp:
                if user_resp.status != 200:
                    body = await user_resp.text()
                    log.error("Failed to fetch user: %s %s", user_resp.status, body)
                    return None, None
                return token_data, await user_resp.json()

    token_data, user = asyncio.run(exchange())
    if token_data is None or user is None:
        return jsonify({"error": "OAuth exchange failed"}), 502

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = token_data.get("expires_in")
    user_id = str(user.get("id"))
    username = user.get("username", "")

    oauth = load_oauth()
    owner = oauth.get("owner_user_id")
    if owner is None:
        # First login binds the owner.
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

    # Later logins: allow the owner or listed admins.
    if is_owner(user_id) or is_admin(user_id):
        session["user_id"] = user_id
        session["username"] = username
        return redirect(url_for("index"))

    return jsonify({"error": "You are not authorized to access this panel"}), 403

@app.route("/auth/logout")
def auth_logout():
    """Clear the Flask session."""
    session.clear()
    return redirect(url_for("index"))

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
            "token": env.get("TIDAL_TOKEN", ""),
            "enabled": env.get("TIDAL_ENABLED", ""),
            "country_code": env.get("TIDAL_COUNTRY_CODE", ""),
            "search_limit": env.get("TIDAL_SEARCH_LIMIT", ""),
        },
        "providers": {
            "youtube": env.get("PROVIDER_YOUTUBE", "true"),
            "youtubemusic": env.get("PROVIDER_YOUTUBEMUSIC", "true"),
            "soundcloud": env.get("PROVIDER_SOUNDCLOUD", "true"),
            "spotify": env.get("PROVIDER_SPOTIFY", "true"),
            "tidal": env.get("PROVIDER_TIDAL", "true"),
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
        if "token" in t:
            env["TIDAL_TOKEN"] = t["token"]
        if "enabled" in t:
            env["TIDAL_ENABLED"] = "true" if t["enabled"] else "false"
        if "country_code" in t:
            env["TIDAL_COUNTRY_CODE"] = t["country_code"]
        if "search_limit" in t:
            env["TIDAL_SEARCH_LIMIT"] = str(t["search_limit"])

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

    Each guild includes its name, icon, member count, and channel list.
    """
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    guilds_data = read_json(GUILDS_FILE, {})
    guilds = []
    for gid, data in guilds_data.items():
        guilds.append({
            "id": gid,
            "name": data.get("name", ""),
            "icon": data.get("icon"),
            "member_count": data.get("member_count", 0),
            "channels": data.get("channels", []),
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

@app.route("/api/blacklist")
def api_get_blacklist():
    """Return the blacklist (read from bot's blacklist.py)."""
    # Blacklist is in-memory in the bot; we read sessions for guild IDs
    # In a real scenario this would come from the bot process or a shared file
    blacklist_path = os.path.join(DATA_DIR, "blacklist.json")
    bl = read_json(blacklist_path, {})
    return jsonify({"blacklist": bl})

@app.route("/api/blacklist", methods=["POST"])
def api_update_blacklist():
    """Update the blacklist (owner/admin only)."""
    if current_user() is None:
        return jsonify({"error": "Authentication required"}), 401
    data = request.json
    if not data or "blacklist" not in data:
        return jsonify({"error": "No blacklist data provided"}), 400
    write_json(os.path.join(DATA_DIR, "blacklist.json"), data["blacklist"])
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

# ── Main ───────────────────────────────────────────────────

if __name__ == "__main__":
    ensure_dirs()
    app.run(host="0.0.0.0", port=8080, debug=True)
