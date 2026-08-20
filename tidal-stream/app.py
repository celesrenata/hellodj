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
SESSION_FILE = None  # Deprecated — all state in credential DB
PORT = int(os.environ.get("TIDAL_STREAM_PORT", "8801"))

# Tidal OAuth app credentials
TIDAL_CLIENT_ID = os.environ.get("TIDAL_CLIENT_ID", "TWDgxSYAcqDo31fj")
TIDAL_CLIENT_SECRET = os.environ.get("TIDAL_CLIENT_SECRET", "")
TIDAL_REDIRECT_URI = os.environ.get(
    "TIDAL_REDIRECT_URI",
    "https://hellodj.celestium.life/auth/tidal/callback",
)

# Quality preference
PREFERRED_QUALITY = os.environ.get("TIDAL_QUALITY", "low_320k")

QUALITY_MAP = {
    "hi_res_lossless": Quality.hi_res_lossless,
    "high_lossless": Quality.high_lossless,
    "low_320k": Quality.low_320k,
    "low_96k": Quality.low_96k,
}

# ── Tidal session management ──────────────────────────────────────────────────

_session: tidalapi.Session | None = None
_session_lock = asyncio.Lock()
_hls_cache: dict[str, str] = {}  # track_id → HLS manifest content
_segment_cache: dict[str, list] = {}  # track_id → list of segment URLs


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
    now = dt.datetime.now(dt.timezone.utc)
    # Handle naive expiry_time by assuming UTC
    expiry = session.expiry_time
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=dt.timezone.utc)
    return now < expiry


async def _get_session() -> tidalapi.Session | None:
    """Get or create a Tidal session from the persisted session file or oauth.json."""
    global _session

    async with _session_lock:
        # If we have a valid session, return it
        if _session is not None and _is_session_valid(_session):
            return _session

        # Read tokens from credential store DB (single source of truth)
        db_file = DATA_DIR / "hellodj.db"
        db_loaded = False
        if db_file.exists():
            try:
                import sys as _sys
                _sys.path.insert(0, "/app")
                from credentials import creds as _creds
                import datetime as dt
                access_token = _creds.get("tidal.access_token")
                refresh_token = _creds.get("tidal.refresh_token", "")
                expires_at_str = _creds.get("tidal.expires_at", "")
                if access_token:
                    session = tidalapi.Session()
                    session.audio_quality = QUALITY_MAP.get(PREFERRED_QUALITY, Quality.high_lossless)
                    session.access_token = access_token
                    session.refresh_token = refresh_token
                    session.token_type = "Bearer"
                    session.is_pkce = True
                    session.country_code = "US"
                    if expires_at_str:
                        try:
                            session.expiry_time = dt.datetime.fromtimestamp(
                                float(expires_at_str), tz=dt.timezone.utc
                            )
                        except (ValueError, TypeError):
                            session.expiry_time = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
                    else:
                        session.expiry_time = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)

                    if _is_session_valid(session):
                        log.info("Authenticated with Tidal via credential DB (direct load)")
                        _session = session
                        return _session
                    else:
                        log.warning("DB tokens present but session expired")
                    db_loaded = True
            except ImportError:
                log.warning("credentials module not available in tidal-stream")
            except Exception as exc:
                log.warning("Failed to load from credential DB: %s", exc)

        log.warning("No valid Tidal session — auth via web-ui (/auth/tidal/login) or /auth/login")
        return None


# ── HTTP Handlers ──────────────────────────────────────────────────────────────

async def handle_stream(request: web.Request) -> web.Response:
    """Resolve a Tidal track ID to a direct audio stream URL using tidalapi."""
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

        if stream.is_bts:
            # BTS gives direct downloadable URLs — return the first one
            urls = manifest.get_urls()
            if urls:
                url = urls[0] if isinstance(urls, list) else urls
                log.info("Resolved track %s: quality=%s codec=%s (BTS direct)", track_id, quality, codec)
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
            # MPD has multiple segments — serve HLS manifest via local endpoint
            # Lavalink's HTTP source now supports fMP4 HLS natively
            hls_content = manifest.get_hls()
            if hls_content:
                _hls_cache[track_id] = hls_content
                hls_url = f"http://localhost:{PORT}/hls/{track_id}.m3u8"
                log.info("Resolved track %s: quality=%s codec=%s (MPD→HLS, %d segments)",
                         track_id, quality, codec, hls_content.count("#EXTINF"))
                return web.json_response({
                    "url": hls_url,
                    "codec": codec,
                    "quality": str(quality),
                    "mime_type": "application/vnd.apple.mpegurl",
                    "track_id": track_id,
                    "title": track.name,
                    "artist": track.artist.name if track.artist else "Unknown",
                    "duration_ms": (track.duration or 0) * 1000,
                })

        return web.json_response({"error": f"No playable stream for track {track_id}"}, status=404)

    except Exception as exc:
        log.error("Failed to resolve track %s: %s", track_id, exc, exc_info=True)
        return web.json_response({"error": f"Failed to resolve track: {exc}"}, status=500)


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
    
    NOTE: We use tidalapi's default internal client (NOT the developer
    portal client) because only the internal client grants full streaming access.
    The internal client only accepts tidal.com/android/login/auth as redirect,
    so the user must paste the redirect URL back after login.
    """
    global _pkce_session

    session = tidalapi.Session()
    session.audio_quality = QUALITY_MAP.get(PREFERRED_QUALITY, Quality.high_lossless)

    # Keep tidalapi's default internal client_id AND redirect URI for full access.
    url = session.pkce_login_url()
    _pkce_session = session

    log.info("Tidal PKCE auth: showing login page (internal client)")
    html = f"""<!DOCTYPE html>
<html><head><title>Tidal Login — HelloDJ</title></head>
<body style="font-family: sans-serif; max-width: 600px; margin: 40px auto; padding: 20px;">
<h2>Tidal Login</h2>
<p><a href="{url}" target="_blank" style="font-size: 1.2em; color: #0070eb;">Click here to log in to Tidal</a></p>
<p>After logging in, you'll land on a page that says "Something went wrong" or shows a blank page.
<strong>Copy the entire URL</strong> from your browser's address bar (it contains <code>?code=...</code>) and paste it below:</p>
<form action="/auth/callback" method="get">
<input type="text" name="redirect_url" style="width:100%; padding:8px; font-size:14px; box-sizing:border-box;"
       placeholder="Paste the tidal.com/android/login/auth?code=... URL here">
<br><br>
<button type="submit" style="padding:10px 20px; font-size:16px; cursor:pointer; background:#0070eb; color:white; border:none; border-radius:4px;">Complete Login</button>
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

    # Handle double-proxying: if redirect_url points back to our own callback,
    # extract the nested redirect_url from it
    from urllib.parse import urlsplit, parse_qs, unquote
    if redirect_url and "/auth/callback" in redirect_url and "redirect_url=" in redirect_url:
        parsed = urlsplit(redirect_url)
        nested = parse_qs(parsed.query).get("redirect_url", [None])[0]
        if nested:
            redirect_url = unquote(nested)
            log.info("Tidal PKCE callback: extracted nested redirect_url (len=%d)", len(redirect_url))

    if _pkce_session is None:
        return web.json_response({"error": "No pending auth flow — visit /auth/login first"}, status=400)

    try:
        # Exchange the redirect URL for tokens
        log.info("Tidal PKCE callback: exchanging code from URL (len=%d)", len(redirect_url))
        token_json = _pkce_session.pkce_get_auth_token(redirect_url)
        log.info("Tidal PKCE callback: got token response keys: %s", list(token_json.keys()) if isinstance(token_json, dict) else type(token_json))

        # Manually set token fields instead of calling process_auth_token(),
        # which tries to hit /v1/sessions (requires r_usr scope that developer
        # apps don't have).
        import datetime
        _pkce_session.access_token = token_json["access_token"]
        _pkce_session.expiry_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            seconds=token_json["expires_in"]
        )
        _pkce_session.refresh_token = token_json["refresh_token"]
        _pkce_session.token_type = token_json["token_type"]
        _pkce_session.is_pkce = True
        _pkce_session.country_code = "US"

        # Save to credential DB — single source of truth
        import time as _time
        try:
            import sys as _sys
            _sys.path.insert(0, "/app")
            from credentials import creds as _creds
            _creds.set("tidal.access_token", token_json["access_token"])
            _creds.set("tidal.api_token", token_json["access_token"])
            _creds.set("tidal.refresh_token", token_json["refresh_token"])
            _creds.set("tidal.expires_at", str(_time.time() + token_json["expires_in"]))
            _creds.set("tidal.updated_at", _time.strftime('%Y-%m-%dT%H:%M:%S+00:00', _time.gmtime()))
            log.info("Tidal session saved to credential DB")
        except Exception as save_exc:
            log.error("Could not save to credential DB: %s", save_exc)

        _session = _pkce_session
        _pkce_session = None
        log.info("Tidal PKCE auth complete — session active")

        return web.Response(
            text="<h2>\u2713 Tidal authenticated successfully!</h2><p>You can close this page.</p>",
            content_type="text/html",
        )

    except Exception as exc:
        log.error("Tidal PKCE callback failed: %s", exc, exc_info=True)
        return web.json_response({"error": f"Auth failed: {exc}"}, status=500)


# ── App setup ──────────────────────────────────────────────────────────────────

async def handle_hls(request: web.Request) -> web.Response:
    """Serve a cached HLS manifest for a track."""
    filename = request.match_info.get("filename", "")
    track_id = filename.replace(".m3u8", "")
    content = _hls_cache.get(track_id)
    if not content:
        return web.Response(text="Not found", status=404)
    return web.Response(
        text=content,
        content_type="application/vnd.apple.mpegurl",
        headers={"Access-Control-Allow-Origin": "*"},
    )


async def handle_proxy(request: web.Request) -> web.StreamResponse:
    """Proxy all audio segments for a track as a single streaming HTTP response.
    
    This concatenates all MP4/FLAC segments into one continuous stream
    that Lavalink can consume as a regular audio file.
    """
    import aiohttp as _aiohttp

    track_id = request.match_info.get("track_id")
    if not track_id:
        return web.json_response({"error": "Missing track_id"}, status=400)

    urls = _segment_cache.get(track_id)
    if not urls:
        return web.json_response({"error": "No cached segments — call /stream first"}, status=404)

    # Stream all segments as a single response
    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "audio/mp4",
            "Transfer-Encoding": "chunked",
        },
    )
    await response.prepare(request)

    async with _aiohttp.ClientSession() as http_session:
        for url in urls:
            try:
                async with http_session.get(url) as seg_resp:
                    if seg_resp.status == 200:
                        async for chunk in seg_resp.content.iter_chunked(65536):
                            await response.write(chunk)
            except Exception as exc:
                log.warning("Failed to fetch segment: %s", exc)
                break

    await response.write_eof()
    return response


async def _token_persist_task(app: web.Application) -> None:
    """Periodically persist the in-memory Tidal session tokens back to the credential DB.

    tidalapi refreshes tokens internally but never writes them back.
    This task syncs the in-memory session state to the DB every 5 minutes
    so the bot and web-ui always see current token/expiry values.
    """
    import time as _time
    await asyncio.sleep(30)  # Initial delay — let session load first
    while True:
        try:
            if _session is not None and _session.access_token:
                import sys as _sys
                _sys.path.insert(0, "/app")
                from credentials import creds as _creds
                import datetime as dt

                _creds.set("tidal.access_token", _session.access_token)
                _creds.set("tidal.api_token", _session.access_token)
                if _session.refresh_token:
                    _creds.set("tidal.refresh_token", _session.refresh_token)

                # Compute expires_at from session.expiry_time
                if _session.expiry_time is not None:
                    expiry = _session.expiry_time
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=dt.timezone.utc)
                    _creds.set("tidal.expires_at", str(expiry.timestamp()))
                else:
                    # No expiry known — assume 4 hours from now
                    _creds.set("tidal.expires_at", str(_time.time() + 14400))

                _creds.set("tidal.issuing_client_id", "fX2JxdmntZWK0ixT")
                _creds.set("tidal.updated_at", _time.strftime('%Y-%m-%dT%H:%M:%S+00:00', _time.gmtime()))
                log.debug("Token persisted to credential DB")
        except Exception as exc:
            log.warning("Token persist failed: %s", exc)
        await asyncio.sleep(300)  # Every 5 minutes


async def _start_background_tasks(app: web.Application) -> None:
    """Start background tasks on app startup."""
    app["token_persist_task"] = asyncio.ensure_future(_token_persist_task(app))


async def _cleanup_background_tasks(app: web.Application) -> None:
    """Cancel background tasks on app shutdown."""
    task = app.get("token_persist_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/stream/{track_id}", handle_stream)
    app.router.add_get("/hls/{filename}", handle_hls)
    app.router.add_get("/proxy/{track_id}", handle_proxy)
    app.router.add_get("/search", handle_search)
    app.router.add_get("/auth/login", handle_auth_login)
    app.router.add_get("/auth/callback", handle_auth_callback)
    app.router.add_get("/auth/tidal/callback", handle_auth_callback)
    app.router.add_get("/health", handle_health)
    app.on_startup.append(_start_background_tasks)
    app.on_cleanup.append(_cleanup_background_tasks)
    return app


if __name__ == "__main__":
    log.info("Starting tidal-stream service on port %d", PORT)
    log.info("Data dir: %s", DATA_DIR)
    log.info("Preferred quality: %s", PREFERRED_QUALITY)
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=PORT)
