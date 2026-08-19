"""spotify-stream: HTTP service that streams Spotify tracks directly via librespot.

Handles OAuth authentication end-to-end:
- On startup, if no stored credentials exist, automatically starts the OAuth flow
- Exposes the OAuth URL via GET /auth/status for the web-ui or user to open
- After successful auth, stores credentials for future restarts
- On restart, restores session from stored credentials automatically

Endpoints:
    GET /stream/<track_id>  -> Raw audio stream (OGG Vorbis)
    GET /health             -> Service health + session status
    GET /auth/status        -> Current auth state (pending URL or authenticated)
    POST /auth/reset        -> Force re-authentication
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("spotify-stream")

# ── Configuration ──────────────────────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
CREDENTIALS_FILE = DATA_DIR / "spotify-credentials.json"
PORT = int(os.environ.get("SPOTIFY_STREAM_PORT", "8802"))

# ── librespot imports ──────────────────────────────────────────────────────────

from librespot.core import Session
from librespot.audio import PlayableContentFeeder, SuperAudioFormat
from librespot.metadata import TrackId

# ── Session management ─────────────────────────────────────────────────────────

_session: Session | None = None
_session_lock = threading.Lock()
_oauth_url: str | None = None
_oauth_in_progress = False


def _build_conf() -> Session.Configuration:
    """Build a librespot Configuration with our credentials path."""
    conf = Session.Configuration.Builder()
    conf.set_store_credentials(True)
    conf.set_stored_credential_file(str(CREDENTIALS_FILE))
    return conf.build()


def _get_session() -> Session | None:
    """Return the current valid session, or None."""
    global _session
    with _session_lock:
        if _session is not None and _session.is_valid():
            return _session
        return None


def _restore_session() -> bool:
    """Try to restore a session from stored credentials. Returns True on success."""
    global _session
    with _session_lock:
        if _session is not None and _session.is_valid():
            return True

        if not CREDENTIALS_FILE.exists():
            return False

        try:
            session = (
                Session.Builder(conf=_build_conf())
                .stored_file(str(CREDENTIALS_FILE))
                .create()
            )
            if session.is_valid():
                log.info("Restored Spotify session from stored credentials")
                _session = session
                return True
        except Exception as exc:
            log.warning("Failed to restore from stored credentials: %s", exc)

        return False


def _run_oauth_flow() -> bool:
    """Run the OAuth flow (blocking). Returns True on success.

    Sets _oauth_url so the /auth/status endpoint can expose it.
    The OAuth flow starts a callback server on port 5588 inside the container.
    """
    global _session, _oauth_url, _oauth_in_progress

    _oauth_in_progress = True
    _oauth_url = None

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        builder = Session.Builder(conf=_build_conf())

        def _url_callback(url: str):
            global _oauth_url
            _oauth_url = url
            log.info("═══════════════════════════════════════════════════════════")
            log.info("  Spotify OAuth: Authorize at this URL:")
            log.info("  %s", url)
            log.info("═══════════════════════════════════════════════════════════")
            return url

        builder.oauth(oauth_url_callback=_url_callback)
        session = builder.create()

        if session and session.is_valid():
            with _session_lock:
                _session = session
            log.info("Spotify OAuth completed — authenticated successfully")
            _oauth_url = None
            _oauth_in_progress = False
            return True

        log.error("OAuth flow completed but session is invalid")
        _oauth_in_progress = False
        return False

    except Exception as exc:
        log.error("OAuth flow failed: %s", exc)
        _oauth_in_progress = False
        return False


async def _startup_auth(app: web.Application):
    """Background task: authenticate on startup.

    1. Try stored credentials first (instant)
    2. If no credentials, start OAuth flow and wait for browser callback
    """
    # Give the HTTP server a moment to start so /auth/status is reachable
    await asyncio.sleep(1)

    if _restore_session():
        return

    log.info("No stored credentials — starting OAuth flow automatically")
    log.info("Monitor /auth/status or container logs for the OAuth URL")

    # Run OAuth in a thread (it blocks waiting for the browser callback)
    success = await asyncio.to_thread(_run_oauth_flow)
    if not success:
        log.error(
            "OAuth flow did not complete. The service will retry on the next "
            "request, or you can POST /auth/reset to restart the flow."
        )


# ── Audio quality picker ───────────────────────────────────────────────────────

class PreferredQualityPicker:
    """Pick the highest available audio quality."""

    def get_file(self, files):
        """Select preferred audio file from available options."""
        from librespot.audio import Metadata

        # Prefer highest quality OGG Vorbis — our patched Lavalink handles it
        preferred_order = [
            Metadata.AudioFile.OGG_VORBIS_320,
            Metadata.AudioFile.OGG_VORBIS_160,
            Metadata.AudioFile.OGG_VORBIS_96,
            Metadata.AudioFile.MP3_320,
            Metadata.AudioFile.MP3_256,
            Metadata.AudioFile.MP3_160,
            Metadata.AudioFile.MP3_96,
            Metadata.AudioFile.FLAC_FLAC,
            Metadata.AudioFile.AAC_48,
            Metadata.AudioFile.AAC_24,
        ]

        if not files:
            return None

        for fmt in preferred_order:
            for f in files:
                if f.format == fmt:
                    return f

        return files[0] if files else None


# ── Track preload cache ─────────────────────────────────────────────────────────

import collections

# Cache of loaded tracks: track_id -> (audio_bytes, codec, timestamp)
_track_cache: dict[str, tuple] = collections.OrderedDict()
_track_cache_lock = threading.Lock()
_CACHE_MAX = 10  # Keep at most 10 tracks cached
_CACHE_TTL = 300  # Expire after 5 minutes


def _cache_put(track_id: str, audio_data: bytes, codec):
    """Add a loaded track to the cache."""
    with _track_cache_lock:
        _track_cache[track_id] = (audio_data, codec, time.time())
        # Evict oldest if over capacity
        while len(_track_cache) > _CACHE_MAX:
            _track_cache.popitem(last=False)


def _cache_get(track_id: str) -> tuple | None:
    """Get cached audio data, or None if expired/missing."""
    with _track_cache_lock:
        entry = _track_cache.get(track_id)
        if entry is None:
            return None
        audio_data, codec, ts = entry
        if time.time() - ts > _CACHE_TTL:
            del _track_cache[track_id]
            return None
        return (audio_data, codec)


def _load_and_cache_track(track_id_str: str, session) -> tuple | None:
    """Load a track from Spotify, transcode to MP3, and cache. Returns (audio_data, codec) or None."""
    import subprocess

    track_id = TrackId.from_base62(track_id_str)
    content_feeder = session.content_feeder()
    loaded = content_feeder.load_track(track_id, PreferredQualityPicker(), False, None)
    audio_stream = loaded.input_stream
    stream_impl = audio_stream.stream()
    total_size = stream_impl.size()
    codec = audio_stream.codec()

    # Read entire track into memory
    data = bytearray()
    while len(data) < total_size:
        chunk = stream_impl.read(min(131072, total_size - len(data)))
        if not chunk:
            break
        data.extend(chunk)

    raw_audio = bytes(data)

    # Transcode to MP3 via ffmpeg — Spotify's OGG Vorbis uses non-standard
    # headers that lavaplayer's native decoder can't handle, but ffmpeg can.
    try:
        proc = subprocess.run(
            ["ffmpeg", "-i", "pipe:0", "-f", "mp3", "-ab", "320k", "-v", "quiet", "pipe:1"],
            input=raw_audio,
            capture_output=True,
            timeout=30,
        )
        if proc.returncode == 0 and len(proc.stdout) > 0:
            audio_data = proc.stdout
            log.info("Transcoded track %s: %d -> %d bytes (ogg->mp3)",
                     track_id_str, len(raw_audio), len(audio_data))
            _cache_put(track_id_str, audio_data, SuperAudioFormat.MP3)
            return (audio_data, SuperAudioFormat.MP3)
        else:
            log.error("ffmpeg transcode failed for %s (rc=%d, stderr=%s)",
                      track_id_str, proc.returncode, proc.stderr[:200])
    except Exception as exc:
        log.error("ffmpeg transcode error for %s: %s", track_id_str, exc)

    return None


# ── HTTP Handlers ──────────────────────────────────────────────────────────────

async def handle_preload(request: web.Request) -> web.Response:
    """Preload a track into cache so /stream responds instantly."""
    track_id_str = request.match_info.get("track_id")
    if not track_id_str:
        return web.json_response({"error": "Missing track_id"}, status=400)

    if track_id_str.startswith("spotify:track:"):
        track_id_str = track_id_str.split(":")[-1]

    # Already cached?
    cached = _cache_get(track_id_str)
    if cached:
        audio_data, codec = cached
        return web.json_response({"status": "ok", "track_id": track_id_str, "size": len(audio_data), "cached": True})

    session = _get_session()
    if session is None:
        return web.json_response({"error": "No session"}, status=503)

    try:
        audio_data, codec = await asyncio.to_thread(_load_and_cache_track, track_id_str, session)
        log.info("Preloaded track %s: %d bytes, codec=%s", track_id_str, len(audio_data), codec)
        return web.json_response({"status": "ok", "track_id": track_id_str, "size": len(audio_data)})
    except Exception as exc:
        log.error("Failed to preload track %s: %s", track_id_str, exc)
        return web.json_response({"error": str(exc)}, status=500)


async def handle_stream(request: web.Request) -> web.Response:
    """Stream a Spotify track as raw audio.

    Serves from cache if available (preloaded or previously loaded).
    Lavalink may request the same track multiple times — cache ensures
    consistent data without re-fetching from Spotify.
    """
    track_id_str = request.match_info.get("track_id")
    if not track_id_str:
        return web.json_response({"error": "Missing track_id"}, status=400)

    if track_id_str.startswith("spotify:track:"):
        track_id_str = track_id_str.split(":")[-1]

    # Try cache first (covers preloaded + previously streamed tracks)
    cached = _cache_get(track_id_str)
    if cached:
        audio_data, codec = cached
    else:
        # Load on demand and cache
        session = _get_session()
        if session is None:
            return web.json_response(
                {"error": "Spotify session unavailable — check /auth/status"},
                status=503,
            )
        try:
            result = await asyncio.to_thread(_load_and_cache_track, track_id_str, session)
            if result is None:
                return web.json_response({"error": "Failed to load track"}, status=500)
            audio_data, codec = result
        except Exception as exc:
            log.error("Failed to stream track %s: %s", track_id_str, exc, exc_info=True)
            return web.json_response({"error": f"Failed to stream track: {exc}"}, status=500)

    content_type_map = {
        SuperAudioFormat.VORBIS: "audio/ogg",
        SuperAudioFormat.MP3: "audio/mpeg",
        SuperAudioFormat.AAC: "audio/aac",
        SuperAudioFormat.FLAC: "audio/flac",
    }
    content_type = content_type_map.get(codec, "audio/ogg")

    log.info("Serving track %s: %d bytes, codec=%s", track_id_str, len(audio_data), codec)
    return web.Response(
        body=audio_data,
        content_type=content_type,
        headers={
            "Content-Length": str(len(audio_data)),
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache",
        },
    )


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    session = _get_session()
    if session:
        status = "ok"
    elif _oauth_in_progress:
        status = "authenticating"
    else:
        status = "no_session"
    return web.json_response({"status": status, "service": "spotify-stream"})


async def handle_auth_status(request: web.Request) -> web.Response:
    """Return current authentication state.

    If OAuth is in progress, returns the URL the user needs to visit.
    If authenticated, returns success.
    """
    session = _get_session()
    if session:
        return web.json_response({
            "status": "authenticated",
            "message": "Spotify session is active.",
        })

    if _oauth_in_progress and _oauth_url:
        return web.json_response({
            "status": "pending",
            "message": "OAuth flow in progress — visit the URL to authorize.",
            "oauth_url": _oauth_url,
        })

    if _oauth_in_progress:
        return web.json_response({
            "status": "starting",
            "message": "OAuth flow is starting, URL will be available shortly.",
        })

    return web.json_response({
        "status": "no_session",
        "message": "No active session. POST /auth/reset to start OAuth flow.",
    })


async def handle_auth_reset(request: web.Request) -> web.Response:
    """Force re-authentication by deleting stored credentials and restarting OAuth."""
    global _session, _oauth_url, _oauth_in_progress

    if _oauth_in_progress:
        return web.json_response({
            "status": "already_in_progress",
            "message": "OAuth flow already running. Check /auth/status for the URL.",
        }, status=409)

    # Invalidate current session
    with _session_lock:
        _session = None

    # Remove stored credentials
    if CREDENTIALS_FILE.exists():
        CREDENTIALS_FILE.unlink()
        log.info("Deleted stored credentials")

    # Start OAuth flow in background
    async def _background_oauth():
        await asyncio.sleep(0.5)
        await asyncio.to_thread(_run_oauth_flow)

    asyncio.create_task(_background_oauth())

    return web.json_response({
        "status": "started",
        "message": "OAuth flow started. Check /auth/status for the URL.",
    })


# ── App setup ──────────────────────────────────────────────────────────────────

def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/stream/{track_id}", handle_stream)
    app.router.add_get("/preload/{track_id}", handle_preload)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/auth/status", handle_auth_status)
    app.router.add_post("/auth/reset", handle_auth_reset)

    async def _on_startup(app: web.Application):
        # Fire and forget — don't await
        app["auth_task"] = asyncio.create_task(_startup_auth(app))

    app.on_startup.append(_on_startup)
    return app


if __name__ == "__main__":
    log.info("Starting spotify-stream service on port %d", PORT)
    log.info("Data dir: %s", DATA_DIR)
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT)
