"""tidal-stream: HTTP service that resolves Tidal track IDs to direct audio URLs.

Handles its own OAuth via tidalapi's PKCE flow. On first run, visit /auth/login
to initiate the Tidal login. After that, sessions are persisted and auto-refreshed.

Endpoints:
    GET /stream/<track_id>  -> JSON { "url": "...", "codec": "...", "quality": "..." }
    GET /search?q=<query>   -> JSON { "results": [...] }
    GET /auth/login         -> Redirects to Tidal OAuth (PKCE)
    GET /auth/callback      -> Handles OAuth callback, stores session
    GET /health             -> 200 OK
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import web

import tidalapi
from tidalapi import Quality

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("tidal-stream")

# ── Configuration ──────────────────────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
SESSION_FILE = DATA_DIR / "tidal-session.json"
PORT = int(os.environ.get("TIDAL_STREAM_PORT", "8801"))

# Tidal OAuth app credentials
TIDAL_CLIENT_ID = os.environ.get("TIDAL_CLIENT_ID", "TWDgxSYAcqDo31fj")
TIDAL_CLIENT_SECRET = os.environ.get("TIDAL_CLIENT_SECRET", "")
TIDAL_REDIRECT_URI = os.environ.get(
    "TIDAL_REDIRECT_URI",
    "https://hellodj.celestium.life/auth/tidal/callback",
)

# Quality preference
PREFERRED_QUALITY = os.environ.get("TIDAL_QUALITY", "high_lossless")

QUALITY_MAP = {
    "hi_res_lossless": Quality.hi_res_lossless,
    "high_lossless": Quality.high_lossless,
    "low_320k": Quality.low_320k,
    "low_96k": Quality.low_96k,
}

# ── Tidal session management ──────────────────────────────────────────────────

_session: tidalapi.Session | None = None
_session_lock = asyncio.Lock()


def _is_session_valid(session: tidalapi.Session) -> bool:
    """Check if a session has a valid (non-expired) access token.
    
    Unlike session.check_login(), this doesn't hit the Tidal API — it just
    checks if we have a token that hasn't expired yet.
    """
    import datetime as dt
    if not session.access_token:
        return False
    if session.expiry_time is None:
        return True  # No expiry set, assume valid
    now = dt.datetime.utcnow()
    return now < session.expiry_time


async def _get_session() -> tidalapi.Session | None:
    """Get or create a Tidal session from the persisted session file or oauth.json."""
    global _session

    async with _session_lock:
        # If we have a valid session, return it
        if _session is not None and _is_session_valid(_session):
            return _session

        # Try to restore from tidalapi session file (handles token refresh internally)
        if SESSION_FILE.exists():
            try:
                session = tidalapi.Session()
                session.config.client_id_pkce = TIDAL_CLIENT_ID
                session.audio_quality = QUALITY_MAP.get(PREFERRED_QUALITY, Quality.high_lossless)
                session.login_session_file(SESSION_FILE)
                if _is_session_valid(session):
                    log.info("Restored Tidal session from %s", SESSION_FILE)
                    _session = session
                    return _session
                else:
                    log.warning("Session file exists but session invalid/expired")
            except Exception as exc:
                log.warning("Failed to restore session from file: %s", exc)

        # Fallback: read tokens from data/oauth.json (written by the web-ui)
        oauth_file = DATA_DIR / "oauth.json"
        if oauth_file.exists():
            try:
                data = json.loads(oauth_file.read_text())
                tidal = (data.get("providers") or {}).get("tidal")
                if tidal and tidal.get("access_token"):
                    session = tidalapi.Session()
                    session.config.client_id_pkce = TIDAL_CLIENT_ID
                    session.audio_quality = QUALITY_MAP.get(PREFERRED_QUALITY, Quality.high_lossless)

                    import datetime as dt
                    expiry_time = None
                    if tidal.get("expires_at"):
                        try:
                            expiry_time = dt.datetime.fromtimestamp(
                                float(tidal["expires_at"]), tz=dt.timezone.utc
                            )
                        except (ValueError, TypeError):
                            pass

                    session.load_oauth_session(
                        token_type="Bearer",
                        access_token=tidal["access_token"],
                        refresh_token=tidal.get("refresh_token", ""),
                        expiry_time=expiry_time,
                        is_pkce=True,
                    )
                    if _is_session_valid(session):
                        log.info("Authenticated with Tidal via oauth.json tokens")
                        _session = session
                        return _session
                    else:
                        log.warning("oauth.json tokens present but session invalid/expired")
            except Exception as exc:
                log.warning("Failed to use oauth.json tokens: %s", exc)

        log.warning("No valid Tidal session — auth via web-ui (/auth/tidal/login) or /auth/login")
        return None


# ── HTTP Handlers ──────────────────────────────────────────────────────────────

async def handle_stream(request: web.Request) -> web.Response:
    """Resolve a Tidal track ID to a direct audio stream URL."""
    track_id = request.match_info.get("track_id")
    if not track_id:
        return web.json_response({"error": "Missing track_id"}, status=400)

    session = await _get_session()
    if session is None:
        return web.json_response(
            {"error": "Tidal session unavailable — check OAuth tokens"},
            status=503,
        )

    try:
        track = session.track(int(track_id))
        stream = track.get_stream()

        manifest = stream.get_stream_manifest()
        codec = manifest.get_codecs()
        quality = stream.audio_quality

        # Get the direct URL(s)
        if stream.is_bts:
            # BTS = direct URL available (for qualities below HI_RES_LOSSLESS)
            urls = manifest.get_urls()
            if urls:
                url = urls[0] if isinstance(urls, list) else urls
                log.info(
                    "Resolved track %s: quality=%s codec=%s (direct URL)",
                    track_id, quality, codec,
                )
                return web.json_response({
                    "url": url,
                    "codec": codec,
                    "quality": str(quality),
                    "mime_type": stream.manifest_mime_type,
                    "track_id": track_id,
                    "title": track.name,
                    "artist": track.artist.name if track.artist else "Unknown",
                    "duration_ms": (track.duration or 0) * 1000,
                })
        elif stream.is_mpd:
            # MPD = MPEG-DASH manifest (for HI_RES_LOSSLESS)
            # Convert to HLS m3u8 which Lavalink can consume
            hls = manifest.get_hls()
            if hls:
                log.info(
                    "Resolved track %s: quality=%s codec=%s (MPD/HLS)",
                    track_id, quality, codec,
                )
                return web.json_response({
                    "manifest": hls,
                    "manifest_type": "hls",
                    "codec": codec,
                    "quality": str(quality),
                    "mime_type": "application/vnd.apple.mpegurl",
                    "track_id": track_id,
                    "title": track.name,
                    "artist": track.artist.name if track.artist else "Unknown",
                    "duration_ms": (track.duration or 0) * 1000,
                })

        return web.json_response(
            {"error": f"No playable stream found for track {track_id}"},
            status=404,
        )

    except Exception as exc:
        log.error("Failed to resolve track %s: %s", track_id, exc, exc_info=True)
        return web.json_response(
            {"error": f"Failed to resolve track: {exc}"},
            status=500,
        )


async def handle_search(request: web.Request) -> web.Response:
    """Search Tidal for tracks (used for ISRC/title matching)."""
    query = request.query.get("q")
    if not query:
        return web.json_response({"error": "Missing query parameter 'q'"}, status=400)

    limit = int(request.query.get("limit", "5"))

    session = await _get_session()
    if session is None:
        return web.json_response(
            {"error": "Tidal session unavailable"},
            status=503,
        )

    try:
        results = session.search(query, models=[tidalapi.media.Track], limit=limit)
        tracks = results.get("tracks", []) or []

        items = []
        for track in tracks[:limit]:
            items.append({
                "id": track.id,
                "title": track.name,
                "artist": track.artist.name if track.artist else "Unknown",
                "album": track.album.name if track.album else "",
                "duration_ms": (track.duration or 0) * 1000,
                "isrc": getattr(track, "isrc", None),
            })

        return web.json_response({"results": items})

    except Exception as exc:
        log.error("Search failed: %s", exc, exc_info=True)
        return web.json_response({"error": str(exc)}, status=500)


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    session = await _get_session()
    status = "ok" if session and _is_session_valid(session) else "no_session"
    return web.json_response({"status": status, "service": "tidal-stream"})


# ── Auth Handlers ──────────────────────────────────────────────────────────────

_pkce_session: tidalapi.Session | None = None


async def handle_auth_login(request: web.Request) -> web.Response:
    """Start tidalapi's PKCE login flow.

    If TIDAL_REDIRECT_URI is set to a custom URL (registered on Tidal's developer
    portal), redirects directly to Tidal and the callback completes automatically.
    Otherwise uses the default tidal.com/android redirect and shows a paste form.
    """
    global _pkce_session

    session = tidalapi.Session()
    session.audio_quality = QUALITY_MAP.get(PREFERRED_QUALITY, Quality.high_lossless)

    # Use custom redirect URI if configured
    use_direct_redirect = TIDAL_REDIRECT_URI != "https://tidal.com/android/login/auth"
    if use_direct_redirect:
        session.config.pkce_uri_redirect = TIDAL_REDIRECT_URI
    if TIDAL_CLIENT_ID:
        session.config.client_id_pkce = TIDAL_CLIENT_ID

    url = session.pkce_login_url()
    _pkce_session = session

    if use_direct_redirect:
        # Custom redirect URI registered — Tidal will redirect back to us directly
        log.info("Tidal PKCE auth: redirecting to Tidal (callback: %s)", TIDAL_REDIRECT_URI)
        raise web.HTTPFound(url)
    else:
        # Default redirect — user must paste the URL back
        log.info("Tidal PKCE auth: showing login page with paste form")
        html = f"""<!DOCTYPE html>
<html><head><title>Tidal Login</title></head>
<body style="font-family: sans-serif; max-width: 600px; margin: 40px auto; padding: 20px;">
<h2>Tidal Login</h2>
<p><a href="{url}" target="_blank" style="font-size: 1.2em; color: #0070eb;">Click here to log in to Tidal</a></p>
<p>After logging in, Tidal will show an error page. <strong>Copy the entire URL</strong> from your browser's address bar and paste it below:</p>
<form action="/auth/callback" method="get">
<input type="text" name="redirect_url" style="width:100%; padding:8px; font-size:14px; box-sizing:border-box;"
       placeholder="Paste the tidal.com/android/login/auth?code=... URL here">
<br><br>
<button type="submit" style="padding:10px 20px; font-size:16px; cursor:pointer;">Complete Login</button>
</form>
</body></html>"""
        return web.Response(text=html, content_type="text/html")


async def handle_auth_callback(request: web.Request) -> web.Response:
    """Complete the PKCE flow. Handles both direct Tidal redirect and proxied calls."""
    global _session, _pkce_session

    # Build the full redirect URL that tidalapi needs to extract the code from.
    # Either we got it as a query param (proxied from web-ui) or we ARE the redirect target.
    redirect_url = request.query.get("redirect_url")
    if not redirect_url:
        # We're the direct callback target — reconstruct the full URL
        redirect_url = str(request.url)

    if _pkce_session is None:
        return web.json_response({"error": "No pending auth flow — visit /auth/login first"}, status=400)

    try:
        # Exchange the redirect URL for tokens
        token_json = _pkce_session.pkce_get_auth_token(redirect_url)

        # Manually set token fields instead of calling process_auth_token(),
        # which tries to hit /v1/sessions (requires r_usr scope that developer
        # apps don't have).
        import datetime
        _pkce_session.access_token = token_json["access_token"]
        _pkce_session.expiry_time = datetime.datetime.utcnow() + datetime.timedelta(
            seconds=token_json["expires_in"]
        )
        _pkce_session.refresh_token = token_json["refresh_token"]
        _pkce_session.token_type = token_json["token_type"]
        _pkce_session.is_pkce = True
        _pkce_session.country_code = "US"

        # Save session
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            _pkce_session.save_session_file(SESSION_FILE)
        except Exception as save_exc:
            log.warning("Could not save session file: %s", save_exc)

        _session = _pkce_session
        _pkce_session = None
        log.info("Tidal PKCE auth complete — session saved")

        return web.Response(
            text="<h2>\u2713 Tidal authenticated successfully!</h2><p>You can close this page.</p>",
            content_type="text/html",
        )

    except Exception as exc:
        log.error("Tidal PKCE callback failed: %s", exc, exc_info=True)
        return web.json_response({"error": f"Auth failed: {exc}"}, status=500)


# ── App setup ──────────────────────────────────────────────────────────────────

def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/stream/{track_id}", handle_stream)
    app.router.add_get("/search", handle_search)
    app.router.add_get("/auth/login", handle_auth_login)
    app.router.add_get("/auth/callback", handle_auth_callback)
    app.router.add_get("/auth/tidal/callback", handle_auth_callback)
    app.router.add_get("/health", handle_health)
    return app


if __name__ == "__main__":
    log.info("Starting tidal-stream service on port %d", PORT)
    log.info("Data dir: %s", DATA_DIR)
    log.info("Preferred quality: %s", PREFERRED_QUALITY)
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT)
