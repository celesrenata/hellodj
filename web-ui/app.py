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
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, render_template, jsonify, request, redirect, url_for, flash

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", os.urandom(24).hex())

# ── Paths ──────────────────────────────────────────────────
DATA_DIR = os.getenv("HELLODJ_DATA_DIR", "/app/data")
CONFIG_DIR = os.getenv("HELLODJ_CONFIG_DIR", "/app/config")
BACKUP_DIR = os.getenv("HELLODJ_BACKUP_DIR", "/app/config-backups")

ENV_FILE = os.path.join(DATA_DIR, ".env")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
PLAYLISTS_FILE = os.path.join(DATA_DIR, "playlists.json")
CONFIG_FILE = os.path.join(CONFIG_DIR, "hellodj-config.json")

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
    return render_template("index.html", active="dashboard")

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
    return render_template("config.html", active="config")

@app.route("/api/config")
def api_get_config():
    """Return the full configuration."""
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
        },
        "voice": {
            "model_path": env.get("WAKE_WORD_MODEL_PATH", "/app/models/Hello_DJ.onnx"),
            "stt_model_size": env.get("STT_MODEL_SIZE", "base"),
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
    """Update configuration values."""
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

    if "voice" in data:
        v = data["voice"]
        if "model_path" in v:
            env["WAKE_WORD_MODEL_PATH"] = v["model_path"]
        if "stt_model_size" in v:
            env["STT_MODEL_SIZE"] = v["stt_model_size"]
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
    return render_template("guilds.html", active="guilds")

@app.route("/api/guilds")
def api_get_guilds():
    """Return all active guild sessions."""
    sessions = read_json(SESSIONS_FILE, {})
    guilds = []
    for gid, data in sessions.items():
        guilds.append({
            "id": gid,
            "voice_channel": data.get("voice_channel_id"),
            "text_channel": data.get("text_channel_id"),
            "current": data.get("current"),
            "queue_length": len(data.get("queue", [])),
            "auto_resume": data.get("auto_resume", False),
            "autoplay": data.get("autoplay_enabled", False),
            "repeat": data.get("repeat_mode", "off"),
            "source": data.get("source_provider", "youtube"),
            "updated_at": data.get("updated_at", ""),
        })
    return jsonify({"guilds": guilds})

@app.route("/api/guilds/<gid>", methods=["DELETE"])
def api_clear_guild(gid):
    """Clear a guild's session."""
    sessions = read_json(SESSIONS_FILE, {})
    if gid in sessions:
        del sessions[gid]
        write_json(SESSIONS_FILE, sessions)
        return jsonify({"status": "ok", "message": f"Guild {gid} session cleared"})
    return jsonify({"error": "Guild not found"}), 404

# ── Playlists ───────────────────────────────────────────────

@app.route("/playlists")
def playlists_page():
    return render_template("playlists.html", active="playlists")

@app.route("/api/playlists")
def api_get_playlists():
    """Return all playlists grouped by guild."""
    playlists = read_json(PLAYLISTS_FILE, {})
    return jsonify({"playlists": playlists})

@app.route("/api/playlists/<gid>/<name>", methods=["DELETE"])
def api_delete_playlist(gid, name):
    """Delete a playlist."""
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
    return render_template("backups.html", active="backups")

@app.route("/api/backups")
def api_get_backups():
    """List all backups."""
    return jsonify({"backups": get_backup_list()})

@app.route("/api/backups", methods=["POST"])
def api_create_backup():
    """Create a new backup."""
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
    """Restore a backup by name."""
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
    """Delete a backup."""
    backup_path = os.path.join(BACKUP_DIR, name)
    if os.path.exists(backup_path):
        os.remove(backup_path)
        return jsonify({"status": "ok", "message": f"Backup {name} deleted"})
    return jsonify({"error": f"Backup {name} not found"}), 404

# ── Blacklist ──────────────────────────────────────────────

@app.route("/blacklist")
def blacklist_page():
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
    """Update the blacklist."""
    data = request.json
    if not data or "blacklist" not in data:
        return jsonify({"error": "No blacklist data provided"}), 400
    write_json(os.path.join(DATA_DIR, "blacklist.json"), data["blacklist"])
    return jsonify({"status": "ok", "message": "Blacklist updated"})

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

# ── Main ───────────────────────────────────────────────────

if __name__ == "__main__":
    ensure_dirs()
    app.run(host="0.0.0.0", port=8080, debug=True)
