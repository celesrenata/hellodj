"""HelloDJ — Entry point: loads cogs, configures Lavalink/wavelink, syncs slash commands, runs the bot."""

import asyncio
import logging
import logging.handlers
import os
import time

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv
import wavelink

import player
import session
import storage
import metrics as _metrics
import blacklist as _blacklist
import allowlist as _allowlist
import guild_settings as _guild_settings
import sleep_settings as _sleep_settings
import oauth_store
import permissions
import voice_debug
import file_handler
import guild_policy as _guild_policy
from debug import get_debug_logger, log_debug_config

# ── Unified playback imports (optional — bot works without them) ──────────
_unified_import_exc_msg: str = ""
try:
    from playback.session_registry import SessionRegistry
    from playback.orchestrator import InstanceOrchestrator
    from playback.content_filter import ContentFilter
    from playback.user_bans import UserBans
    import playback.classifier as content_classifier
    import playback.persistence as unified_persistence

    _UNIFIED_PLAYBACK_AVAILABLE = True
except ImportError as _unified_import_exc:
    _UNIFIED_PLAYBACK_AVAILABLE = False
    _unified_import_exc_msg = str(_unified_import_exc)

load_dotenv()
dbg = get_debug_logger("bot")

# ── Configuration: unified accessor (credential store + env fallback) ──
from config import cfg

# ── Logging: console + rotating file under /app/config (NFS shared) ──
def _setup_logging():
    """Configure console + rotating-file logging. File path from BOT_LOG_FILE,
    defaulting to the shared NFS mount /app/config/bot.log."""
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    log_file = os.getenv("BOT_LOG_FILE", "/app/config/bot.log")
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

    # Timestamp every record (basicConfig's format= only applies to handlers it
    # creates, so set the formatter explicitly on the pre-built handlers).
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for handler in handlers:
        handler.setFormatter(fmt)

    logging.basicConfig(level=logging.INFO, handlers=handlers)

_setup_logging()
log = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True
intents.guild_typing = False
bot = commands.Bot(command_prefix="!", intents=intents)
player.set_bot(bot)

# ── Unified playback components (optional — non-fatal if unavailable) ──────

if _UNIFIED_PLAYBACK_AVAILABLE:
    _unified_registry = SessionRegistry()
    _content_filter = ContentFilter()
    _user_bans = UserBans()
    _orchestrator = InstanceOrchestrator(bot, _unified_registry)

    # PlaybackRouter imported lazily — router.py uses bot.playback.xxx paths
    # that may not resolve depending on sys.path configuration at startup.
    # If the router fails to import, the playback cog will create its own
    # stub router from bot.playback_router being unset.
    try:
        from playback.router import PlaybackRouter

        _playback_router = PlaybackRouter(
            classifier=content_classifier,
            registry=_unified_registry,
            orchestrator=_orchestrator,
            activity_backend=None,  # Wired after video cog loads
            primary_bot=bot,
            content_filter=_content_filter,
            user_bans=_user_bans,
        )
        # Store on bot for cog access
        bot.playback_router = _playback_router
    except ImportError as _router_exc:
        logging.getLogger(__name__).debug(
            "PlaybackRouter not importable at startup (cog will handle): %s", _router_exc
        )
        _playback_router = None  # type: ignore[assignment]

    bot.unified_registry = _unified_registry
    bot.content_filter = _content_filter
    bot.user_bans = _user_bans
else:
    logging.getLogger(__name__).warning(
        "Unified playback modules not available: %s", _unified_import_exc_msg
    )

# ── Lavalink config ────────────────────────────────────────

LAVALINK_HOST = cfg("lavalink.host", "losingtime.dpaste.org")
LAVALINK_PORT = cfg.int("lavalink.port", 2124)
LAVALINK_PASSWORD = cfg("lavalink.password", "SleepingOnTrains")
LAVALINK_URI = f"http://{LAVALINK_HOST}:{LAVALINK_PORT}"

# ── YouTube poToken (Proof of Origin) ───────────────────────
# Optional values supplied to the youtube-source plugin to defeat YouTube's
# bot-detection ("Sign in to confirm you're not a bot" / "The page needs to be
# reloaded" / "No supported audio streams available") for the non-OAuth
# WEB-family clients. Generate a fresh token via
# https://github.com/iv-org/youtube-trusted-session-generator and set these env
# vars (bot-configmap / docker-compose). Blank values disable the push.
# ── Spotify credentials (optional) ─────────────────────────

SPOTIFY_CLIENT_ID = cfg("spotify.client_id", "")
SPOTIFY_CLIENT_SECRET = cfg("spotify.client_secret", "")

# ── Guild blacklist / allowlist / settings (shared with cogs/admin.py) ───

blacklist = _blacklist.blacklist
is_blacklisted = _blacklist.is_blacklisted
allowlist = _allowlist.allowlist
is_allowed = _allowlist.is_allowed
get_guild_mode = _guild_settings.get_guild_mode


# ── Lavalink node management ───────────────────────────────

async def connect_lavalink():
    """Connect to the Lavalink node using wavelink 3.5 Pool."""
    # Wait for Lavalink to become reachable before connecting. The Lavalink
    # sidecar shares the pod network namespace but may not be ready yet, so
    # poll the REST API with retries instead of failing fast on
    # connection-refused (Errno 111 on ::1 / 127.0.0.1).
    max_attempts = 30
    delay = 2
    rest_url = f"http://{LAVALINK_HOST}:{LAVALINK_PORT}/v4/session"

    for attempt in range(1, max_attempts + 1):
        last_err: Exception | None = None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    rest_url, timeout=aiohttp.ClientTimeout(total=2)
                ) as resp:
                    if resp.status < 500:
                        log.info(
                            "Lavalink is reachable at %s (attempt %d)",
                            rest_url, attempt,
                        )
                        break
                    last_err = RuntimeError(f"HTTP {resp.status}")
        except Exception as exc:  # connection refused until sidecar is ready
            last_err = exc

        log.debug(
            "Lavalink not ready at %s (attempt %d/%d): %s",
            rest_url, attempt, max_attempts,
            last_err if last_err else "no response",
        )
        await asyncio.sleep(delay)
    else:
        log.warning(
            "Lavalink still unreachable after %d attempts; connecting anyway",
            max_attempts,
        )

    node = wavelink.Node(
        uri=LAVALINK_URI,
        password=LAVALINK_PASSWORD,
    )
    await wavelink.Pool.connect(nodes=[node], client=bot)
    log.info("HelloDJ connected to Lavalink at %s", LAVALINK_URI)

    # Push any stored YouTube OAuth refresh token to the youtube-source plugin.
    # The plugin's REST endpoint (/youtube) is served by Lavalink's own Spring
    # server on the same port, authenticated with the Lavalink password.
    # Retry up to 5 times with 2s delay — the NFS mount may not have the
    # oauth.json file visible yet on a fresh pod start.
    for _oauth_attempt in range(1, 6):
        pushed = await push_youtube_oauth()
        if pushed:
            break
        dbg.debug("youtube-oauth push attempt %d: no token yet, retrying in 2s", _oauth_attempt)
        await asyncio.sleep(2)

    # After pushing OAuth, give Lavalink time to exchange the refresh token for
    # an access token (Google token endpoint round-trip). Without this wait, the
    # session resume fires immediately and tracks fail because the TV client's
    # access token hasn't been obtained yet. 5 seconds covers the typical 1-2s
    # token exchange plus network variance.
    if pushed:
        log.info("youtube-oauth: waiting 5s for Lavalink to exchange refresh token for access token")
        await asyncio.sleep(5)

    # Push the optional poToken/visitorData (Proof of Origin) to the same
    # endpoint. poToken defeats YouTube bot-detection for the non-OAuth
    # WEB-family clients and complements OAuth (TV). Blank values are a no-op.
    await push_youtube_pot()

    # Refresh Tidal token at startup (it expires every ~4 hours)
    await refresh_tidal_token()
    await push_tidal_token()


async def push_youtube_oauth() -> bool:
    """Push YouTube OAuth + poToken to Lavalink in a SINGLE request.

    The youtube-source plugin's POST /youtube replaces ALL fields each call,
    so we must send both refreshToken AND poToken together.
    """
    token = cfg("youtube.oauth_refresh_token") or cfg("youtube.refresh_token") or oauth_store.get_youtube_refresh_token()
    pot_token = cfg("youtube.pot_token", "")
    pot_visitor_data = cfg("youtube.pot_visitor_data", "")

    if not token and not pot_token:
        log.info("youtube-oauth: no refresh token or poToken — skipping push")
        return False

    # Keep DB in sync if token came from oauth.json fallback
    if token:
        from credentials import creds as _creds
        if token != (_creds.get("youtube.oauth_refresh_token") or ""):
            _creds.set("youtube.oauth_refresh_token", token)
            _creds.set("youtube.refresh_token", token)

    url = f"{LAVALINK_URI}/youtube"
    payload = {"skipInitialization": False}
    if token:
        payload["refreshToken"] = token
    if pot_token and pot_visitor_data:
        payload["poToken"] = pot_token
        payload["visitorData"] = pot_visitor_data

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers={"Authorization": LAVALINK_PASSWORD},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.text()
                if resp.status in (200, 204):
                    parts = []
                    if token:
                        parts.append("oauth")
                    if pot_token:
                        parts.append("poToken")
                    log.info(
                        "youtube-auth: pushed %s to Lavalink %s (status=%s)",
                        "+".join(parts), url, resp.status,
                    )
                    return True
                log.warning(
                    "youtube-auth: Lavalink push failed (status=%s) body=%s",
                    resp.status, body,
                )
    except Exception as exc:
        log.warning("youtube-auth: push to Lavalink failed: %s", exc)
    return False


async def push_youtube_pot() -> bool:
    """Now a no-op — push_youtube_oauth sends both together."""
    return True


# ── PoToken auto-refresh from bgutil server ─────────────────
# The bgutil-ytdlp-pot-provider HTTP server (deployed at potoken-server:4416)
# generates fresh Proof-of-Origin tokens on demand. This task periodically
# fetches a new token, stores it in the credential store, and re-pushes
# to Lavalink so the WEB-family clients stay authenticated.

POTOKEN_SERVER_URL = os.environ.get(
    "POTOKEN_SERVER_URL",
    "http://potoken-server.hellodj-service.svc.cluster.local:4416",
)
POTOKEN_REFRESH_INTERVAL = int(os.environ.get("POTOKEN_REFRESH_INTERVAL", "3600"))  # 1 hour


async def fetch_and_push_potoken() -> bool:
    """Fetch a fresh poToken from the bgutil server and push to Lavalink.

    The bgutil server's POST /get_pot endpoint generates a BotGuard challenge
    response (poToken) and returns it alongside the visitorData (content_binding).
    We store these in the credential store and then re-push the full YouTube
    auth payload (OAuth + poToken) to Lavalink in a single request.
    """
    try:
        async with aiohttp.ClientSession() as session:
            # Request a new poToken; omit content_binding to let the server
            # generate its own visitor_data.
            async with session.post(
                f"{POTOKEN_SERVER_URL}/get_pot",
                json={},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning(
                        "potoken-refresh: bgutil server returned %s: %s",
                        resp.status, body[:200],
                    )
                    return False
                data = await resp.json()

        po_token = data.get("poToken", "")
        visitor_data = data.get("contentBinding", "")

        if not po_token or not visitor_data:
            log.warning("potoken-refresh: empty poToken or contentBinding in response")
            return False

        # Persist to credential store
        from credentials import creds as _creds
        _creds.set("youtube.pot_token", po_token)
        _creds.set("youtube.pot_visitor_data", visitor_data)

        log.info(
            "potoken-refresh: stored new poToken (len=%d) + visitorData (len=%d)",
            len(po_token), len(visitor_data),
        )

        # Re-push full auth payload (OAuth + poToken) to Lavalink
        pushed = await push_youtube_oauth()
        if pushed:
            log.info("potoken-refresh: successfully pushed new poToken to Lavalink")
        return pushed

    except aiohttp.ClientConnectorError:
        log.debug("potoken-refresh: bgutil server not reachable (service may not be deployed)")
        return False
    except Exception as exc:
        log.warning("potoken-refresh: failed: %s", exc)
        return False


_potoken_task_started = False


async def _potoken_refresh_task() -> None:
    """Background task: refresh poToken from bgutil server periodically."""
    # Initial delay — give the cluster time to start the potoken-server pod
    await asyncio.sleep(30)
    # Try an immediate fetch at startup
    await fetch_and_push_potoken()
    while True:
        await asyncio.sleep(POTOKEN_REFRESH_INTERVAL)
        try:
            await fetch_and_push_potoken()
        except Exception:
            log.exception("potoken-refresh: task iteration error")


async def refresh_tidal_token() -> bool:
    """Refresh the Tidal access token using the stored refresh token and client_id.

    Writes the new access token back to the credential store and updates
    tidal.expires_at. Returns True on success.

    Note: The refresh token may have been issued by tidalapi's internal client
    (fX2JxdmntZWK0ixT) during the PKCE login flow in tidal-stream. The refresh
    request MUST use the same client_id that issued the token.
    """
    import time as _time
    from credentials import creds as _creds

    refresh_token = cfg("tidal.refresh_token", "")
    if not refresh_token:
        return False

    # Determine which client_id to use for refresh.
    # Priority: issuing_client_id (stored during PKCE) > tidalapi PKCE client > developer portal
    client_id = (
        cfg("tidal.issuing_client_id", "")
        or "6BDSRdpK9hqEBTgU"  # tidalapi PKCE client (all PKCE tokens use this)
    )

    # Check if token is still valid (with 5 min buffer)
    expires_at = float(cfg("tidal.expires_at", "0") or "0")
    if expires_at > _time.time() + 300:
        return True  # Still valid

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://auth.tidal.com/v1/oauth2/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    # If the primary client_id fails, try the developer portal one as fallback
                    dev_client_id = cfg("tidal.client_id", "")
                    if dev_client_id and dev_client_id != client_id:
                        log.debug("tidal-refresh: retrying with developer portal client_id")
                        async with session.post(
                            "https://auth.tidal.com/v1/oauth2/token",
                            data={
                                "grant_type": "refresh_token",
                                "refresh_token": refresh_token,
                                "client_id": dev_client_id,
                            },
                            timeout=aiohttp.ClientTimeout(total=15),
                        ) as resp2:
                            if resp2.status != 200:
                                body2 = await resp2.text()
                                log.warning("tidal-refresh: failed (status=%s): %s", resp.status, body[:200])
                                return False
                            data = await resp2.json()
                    else:
                        log.warning("tidal-refresh: failed (status=%s): %s", resp.status, body[:200])
                        return False
                else:
                    data = await resp.json()

                new_access = data.get("access_token", "")
                new_refresh = data.get("refresh_token", "")
                expires_in = data.get("expires_in", 86400)
                if new_access:
                    _creds.set("tidal.access_token", new_access)
                    _creds.set("tidal.api_token", new_access)
                    _creds.set("tidal.expires_at", str(_time.time() + expires_in))
                    # Store which client_id successfully refreshed (for next time)
                    _creds.set("tidal.issuing_client_id", client_id)
                    if new_refresh:
                        _creds.set("tidal.refresh_token", new_refresh)
                    log.info("tidal-refresh: token refreshed (expires_in=%s, client=%s)", expires_in, client_id[:8])
                    return True
                log.warning("tidal-refresh: no access_token in response")
                return False
    except Exception as exc:
        log.warning("tidal-refresh: failed: %s", exc)
        return False


async def push_tidal_token() -> bool:
    """Push the current Tidal access token to LavasRC via PATCH /v4/lavasrc/config.

    LavasRC's TidalTokenManager in static-token mode never refreshes on its own.
    After the bot refreshes the token (via refresh_tidal_token), this function
    pushes the fresh token to the running Lavalink instance so LavasRC can
    continue resolving Tidal tracks without a pod restart.
    """
    from credentials import creds as _creds

    token = _creds.get("tidal.access_token") or _creds.get("tidal.api_token") or ""
    if not token:
        log.debug("tidal-push: no access token in DB — skipping push")
        return False

    url = f"{LAVALINK_URI}/v4/lavasrc/config"
    payload = {"tidal": {"token": token}}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.patch(
                url,
                json=payload,
                headers={"Authorization": LAVALINK_PASSWORD},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 204):
                    log.info("tidal-push: pushed fresh token to LavasRC (status=%s)", resp.status)
                    return True
                body = await resp.text()
                log.warning(
                    "tidal-push: LavasRC push failed (status=%s): %s",
                    resp.status, body[:200],
                )
    except Exception as exc:
        log.warning("tidal-push: failed to push to LavasRC: %s", exc)
    return False


async def _token_refresh_watchdog() -> None:
    """Periodically refresh Tidal token and re-push YouTube auth."""
    interval = 300  # every 5 minutes
    while True:
        await asyncio.sleep(interval)
        try:
            refreshed = await refresh_tidal_token()
            if refreshed:
                await push_tidal_token()
            await push_youtube_oauth()
        except Exception:
            log.exception("token-refresh: watchdog iteration error")


async def disconnect_lavalink():
    """Disconnect all Lavalink nodes."""
    if wavelink.Pool.nodes:
        for node in wavelink.Pool.nodes.values():
            await node.close()
    log.info("HelloDJ disconnected from Lavalink")


# ── setup hook ─────────────────────────────────────────────

async def setup_hook():
    log_debug_config()
    dbg.info("setup_hook: loading data stores...")
    storage.load()
    session.load()
    oauth_store.load_oauth()
    _blacklist.load()
    _blacklist.load_track_blacklist()
    _allowlist.load()
    _guild_settings.load()
    _sleep_settings.load()
    _metrics.load()
    _guild_policy.load()
    dbg.info("setup_hook: all data stores loaded")

    # Clean up old uploaded files (default 24h) on startup.
    file_handler.cleanup_old_files()

    # Clean up stale video temp files (>24h) on startup.
    try:
        from video.sources import _DOWNLOAD_DIR as _video_dir
        import time as _time

        if _video_dir.exists():
            _cutoff = _time.time() - 86400  # 24 hours
            for _f in _video_dir.iterdir():
                if _f.is_file() and _f.stat().st_mtime < _cutoff:
                    try:
                        _f.unlink()
                        dbg.debug("Cleaned up stale video file: %s", _f.name)
                    except OSError:
                        pass
    except ImportError:
        pass

    # Connect to Lavalink
    dbg.info("setup_hook: connecting to Lavalink at %s:%s", LAVALINK_HOST, LAVALINK_PORT)
    await connect_lavalink()
    dbg.info("setup_hook: Lavalink connected successfully")

    # Load custom cogs
    await bot.load_extension("cogs.music")
    await bot.load_extension("cogs.playlists")
    await bot.load_extension("cogs.filters")
    await bot.load_extension("cogs.autoplay")
    await bot.load_extension("cogs.admin")
    await bot.load_extension("cogs.lyrics")
    await bot.load_extension("cogs.info")
    await bot.load_extension("cogs.help")
    await bot.load_extension("cogs.radio")
    await bot.load_extension("cogs.voice")
    await bot.load_extension("cogs.stream")

    # Video streaming cog (optional — bot still works without the video module)
    try:
        await bot.load_extension("cogs.video")
        dbg.info("setup_hook: video cog loaded")
    except Exception as _video_exc:
        log.warning("setup_hook: could not load video cog (non-fatal): %s", _video_exc)

    # Register the unified remote control view as persistent (survives restarts)
    from views.unified_remote import UnifiedControlView
    bot.add_view(UnifiedControlView())
    dbg.info("setup_hook: UnifiedControlView registered as persistent view")

    # Wire video cog's activity_backend into the unified router
    if _UNIFIED_PLAYBACK_AVAILABLE and _playback_router is not None:
        video_cog = bot.get_cog("Video")
        if video_cog and hasattr(video_cog, "_backend"):
            _playback_router._activity_backend = video_cog._backend

    # ── Lyrics overlay: chain LyricsService into track-start callback ──
    # Must run AFTER the video cog loads (VisualizerManager may register its
    # callback first). LyricsService captures the existing callback and chains
    # itself so both run on track start. Failures never propagate (Req 9.5).
    try:
        from video.lyrics_service import init_lyrics_services, register_track_start_callback

        video_cog = bot.get_cog("Video")
        if video_cog and hasattr(video_cog, "_backend"):
            ws_hub = video_cog._backend.ws_hub
            init_lyrics_services(ws_hub)
            register_track_start_callback()
            dbg.info("setup_hook: lyrics service initialized and track-start callback chained")
        else:
            dbg.debug("setup_hook: video cog not available, lyrics service not initialized")
    except Exception as _lyrics_exc:
        log.warning("setup_hook: could not initialize lyrics service (non-fatal): %s", _lyrics_exc)

    # Unified playback cog (optional — requires unified playback modules)
    if _UNIFIED_PLAYBACK_AVAILABLE:
        try:
            await bot.load_extension("cogs.playback")
            dbg.info("setup_hook: unified playback cog loaded")
        except Exception as _playback_exc:
            log.warning("setup_hook: could not load playback cog (non-fatal): %s", _playback_exc)

        # Admin panel cog (optional — provides /hellodj command group)
        try:
            await bot.load_extension("cogs.admin_panel")
            dbg.info("setup_hook: admin panel cog loaded")
        except Exception as _admin_exc:
            log.warning("setup_hook: could not load admin panel cog (non-fatal): %s", _admin_exc)

    # ── switchable voice-connect debug layer ─────────────────
    # Installs a socket raw-listener that logs whether the bot's OWN
    # VOICE_STATE_UPDATE / VOICE_SERVER_UPDATE events arrive after op-4,
    # and whether a voice client was registered at arrival time. On when
    # HELLODJ_VOICE_DEBUG=1 (default); set to 0 to disable.
    voice_debug.install_raw_listeners(bot)

    # Sync commands globally. If Entry Point conflict (50240) happens,
    # fall back to per-guild sync in on_ready.
    try:
        await bot.tree.sync()
        log.info("Global slash command sync complete.")
        bot._global_sync_ok = True
    except discord.HTTPException as _sync_exc:
        if _sync_exc.code == 50240:
            log.warning("Global sync blocked by Entry Point (50240) — will sync per-guild in on_ready")
            bot._global_sync_ok = False
        else:
            log.error("Global sync failed: %s", _sync_exc)
            bot._global_sync_ok = False

    # Per-guild sync happens in on_ready (guild cache is empty here in setup_hook)


bot.setup_hook = setup_hook

# ── global permission check (blacklist + allowlist + mode) ──

async def permission_check(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return True

    gid = interaction.guild.id
    uid = interaction.user.id

    # Allow /activate command through without any gates
    if interaction.command and getattr(interaction.command, "name", "") == "activate":
        return True

    # Activation key gate: guilds must be activated before any commands work.
    from credentials import creds as _creds
    activated = _creds.get(f"guild.{gid}.activated", "")
    if activated != "true":
        await interaction.response.send_message(
            "🔒 This server has not been activated. "
            "An administrator must run `/activate <key>` to enable HelloDJ.",
            ephemeral=True,
        )
        return False

    # Guild approval gate: only approved guilds can use commands.
    if not await _guild_policy.is_authorized(gid):
        await interaction.response.send_message(
            "⏳ This server is pending approval by a HelloDJ administrator. "
            "Commands are disabled until approved.",
            ephemeral=True,
        )
        return False

    mode = get_guild_mode(gid)

    if mode == "allow_all":
        # In allow-all mode, only allow-listed users can interact.
        if not is_allowed(gid, uid):
            await interaction.response.send_message(
                "You are not allowed to use HelloDJ in this guild.", ephemeral=True
            )
            return False
    else:
        # In restrictive mode (default), block blacklisted users.
        if is_blacklisted(gid, uid):
            await interaction.response.send_message(
                "You have been revoked from using HelloDJ.", ephemeral=True
            )
            return False

    return True

bot.interaction_check = permission_check


# ── resume sessions ────────────────────────────────────────

_resumed = False


async def _resume_sessions():
    for gid_str, saved in session.all().items():
        # Skip composite keys ("guild_id:channel_id") — those are managed by
        # the unified playback persistence system (playback/persistence.py).
        if ":" in gid_str:
            continue
        if not saved.get("auto_resume"):
            continue
        try:
            gid = int(gid_str)
            guild = bot.get_guild(gid)
            if guild is None:
                continue

            # Skip guilds that aren't activated
            from credentials import creds as _resume_creds
            if _resume_creds.get(f"guild.{gid}.activated", "") != "true":
                log.info("resume: skipping guild %d — not activated", gid)
                continue

            voice_channel = guild.get_channel(saved.get("voice_channel_id"))
            text_channel = guild.get_channel(saved.get("text_channel_id"))
            if voice_channel is None or text_channel is None:
                continue
            if not any(not m.bot for m in voice_channel.members):
                continue

            state = player.get_state(gid)
            state["voice_channel"] = voice_channel
            state["text_channel"] = text_channel

            state["autoplay_enabled"] = saved.get("autoplay_enabled", False)
            state["autoplay_genres"] = saved.get("autoplay_genres", [])
            saved_sp = saved.get("source_provider", "youtube")
            state["source_provider"] = saved_sp
            state["repeat_mode"] = saved.get("repeat_mode", "off")
            state["crossfade_seconds"] = saved.get("crossfade_seconds", 0.0)
            state["history"] = saved.get("history", [])

            # DIAGNOSIS: log what source_provider was actually loaded from disk
            log.info(
                "resume guild=%d loaded source_provider=%r from disk (saved keys=%r)",
                gid, saved_sp, list(saved.keys()),
            )

            entries = []
            if saved.get("current"):
                entries.append(saved["current"])
            entries.extend(saved.get("queue", []))
            if entries:
                await player.enqueue_and_start(guild, text_channel, entries, replace=True)
                await text_channel.send("HelloDJ reconnected after a restart — resuming the queue.")
        except Exception as exc:
            if "50001" in str(exc) or "Missing Access" in str(exc):
                log.info("Guild %s no longer accessible — clearing stale session.", gid_str)
                await session.clear(gid)
                continue
            log.warning("Could not resume session for guild %s: %s", gid_str, exc)


def _build_guilds_data() -> dict:
    """Snapshot the bot's live gateway guilds into the bot_guilds.json schema."""
    data = {}
    for guild in bot.guilds:
        channels = []
        for ch in guild.channels:
            if ch.type in (discord.ChannelType.text, discord.ChannelType.voice):
                channels.append(
                    {"id": str(ch.id), "name": ch.name, "type": str(ch.type)}
                )
        # Persist only the icon id/key (guild.icon.key) — NOT the full CDN url.
        # The web UI splices this value into the icon-id path segment; storing the
        # .url produced malformed URLs like
        #   https://cdn.discordapp.com/icons/<gid>/https://cdn.discordapp.com/icons/...
        icon = guild.icon.key if guild.icon else None
        _, missing = permissions.check_permissions(guild.me) if guild.me else ({}, [])
        data[str(guild.id)] = {
            "name": guild.name,
            "icon": icon,
            "member_count": guild.member_count or 0,
            "channels": channels,
            "permissions_ok": not missing,
            "missing_permissions": missing,
        }
    return data


# ── gateway-health watchdog ────────────────────────────────
# Observed failure (2026-08-15): the gateway TCP socket connects and READY is
# received (a session id is logged from the READY payload), but the guild cache
# never populates — no GUILD_CREATE events arrive, so on_ready never fires,
# slash commands are not synced, and no /play / voice join can be processed.
# This watchdog detects the pre-on_ready stall, force-reconnects the gateway,
# and escalates to a process restart (k8s Recreate) if the stall persists.
_ready_at: float = 0.0          # monotonic() when on_ready last fired
_watchdog_started = False
# YouTube OAuth periodic re-push watchdog (started once per process)
_yt_oauth_task_started = False
# Guild-authorization periodic re-check watchdog (started once per process)
_guild_policy_task_started = False
# Unified playback orchestrator health check (started once per process)
_orchestrator_health_started = False


async def _gateway_health_watchdog() -> None:
    stall_secs = float(os.getenv("GATEWAY_READY_TIMEOUT", "120.0"))
    backoff = int(os.getenv("GATEWAY_RECONNECT_BACKOFF", "30"))
    max_reconnects = int(os.getenv("GATEWAY_RESTART_AFTER", "3"))
    reconnects = 0
    log.info("gateway watchdog: starting (stall=%.0fs backoff=%ds max_reconnects=%d)",
             stall_secs, backoff, max_reconnects)
    while True:
        await asyncio.sleep(30)
        try:
            if bot.is_ready():
                continue
            elapsed = time.monotonic() - _ready_at
            if elapsed < stall_secs:
                continue
            log.error(
                "gateway watchdog: READY stall (not ready for %.0fs; %d guilds "
                "cached; ws=%s; reconnects=%d)",
                elapsed, len(bot.guilds), bot.ws is not None, reconnects,
            )
            if reconnects >= max_reconnects:
                log.critical(
                    "gateway watchdog: still stalled after %d reconnects — "
                    "forcing process restart (k8s will relaunch)",
                    reconnects,
                )
                os._exit(1)
            reconnects += 1
            # Backoff before reconnecting so we don't hammer the gateway /
            # trip a global rate limit that withholds guild data.
            await asyncio.sleep(backoff)
            ws = bot.ws
            if ws is not None:
                try:
                    await ws.close()
                    log.info(
                        "gateway watchdog: closed gateway websocket to force "
                        "reconnect (attempt %d)",
                        reconnects,
                    )
                except Exception:
                    log.exception("gateway watchdog: could not close websocket")
            else:
                log.warning("gateway watchdog: no bot.ws to close; relying on restart")
        except Exception:
            log.exception("gateway watchdog: loop error")


@bot.event
async def on_connect():
    global _watchdog_started
    if not _watchdog_started:
        _watchdog_started = True
        bot.loop.create_task(_gateway_health_watchdog())
        log.info("gateway health watchdog started (on_connect)")


# ── Unified playback: orchestrator health loop ─────────────


async def _orchestrator_health_loop():
    """Run orchestrator health checks every 30 seconds."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await _orchestrator.health_check()
        except Exception as exc:
            log.warning("Orchestrator health check error: %s", exc)
        await asyncio.sleep(30)


@bot.event
async def on_ready():
    global _ready_at
    global _yt_oauth_task_started
    global _guild_policy_task_started
    _ready_at = time.monotonic()
    log.info("HelloDJ logged in as %s (%s)", bot.user, bot.user.id)
    log.info("on_ready fired with %d guilds", len(bot.guilds))
    # Re-check every guild the bot is already in against admin membership
    # (startup re-check; unauthorized guilds are left).
    await _recheck_guilds()
    await oauth_store.write_guilds(_build_guilds_data(), force=True)

    # Sync commands per-guild (guild cache is populated now)
    # Only needed if global sync failed (Entry Point conflict)
    if not getattr(bot, '_global_sync_ok', False):
        synced = 0
        for guild in bot.guilds:
            try:
                bot.tree.copy_global_to(guild=guild)
                await bot.tree.sync(guild=guild)
                synced += 1
            except Exception as _g_exc:
                log.debug("Guild sync failed for %s: %s", guild.id, _g_exc)
        log.info("Per-guild command sync complete (%d/%d guilds).", synced, len(bot.guilds))
    else:
        # Global sync succeeded — clear any stale per-guild overrides
        for guild in bot.guilds:
            try:
                bot.tree.clear_commands(guild=guild)
                await bot.tree.sync(guild=guild)
            except Exception:
                pass
        log.info("Cleared per-guild command overrides (%d guilds).", len(bot.guilds))

    global _resumed
    if not _resumed:
        _resumed = True
        await _resume_sessions()
    # Start the token refresh watchdog (Tidal refresh + YouTube push + Spotify)
    if not _yt_oauth_task_started:
        _yt_oauth_task_started = True
        bot.loop.create_task(_token_refresh_watchdog())
        log.info("token-refresh: watchdog started (tidal + youtube + spotify)")
    # Start the poToken auto-refresh from bgutil server (hourly)
    global _potoken_task_started
    if not _potoken_task_started:
        _potoken_task_started = True
        bot.loop.create_task(_potoken_refresh_task())
        log.info("potoken-refresh: background task started (interval=%ds)", POTOKEN_REFRESH_INTERVAL)
    # Start the periodic guild-authorization re-check so the policy stays
    # current as admins join/leave guilds while the bot runs.
    if not _guild_policy_task_started:
        _guild_policy_task_started = True
        bot.loop.create_task(_guild_policy_watchdog())
        log.info("guild_policy: periodic re-check watchdog started")

    # ── Unified playback: orchestrator init + persistence + health check ──
    global _orchestrator_health_started
    if _UNIFIED_PLAYBACK_AVAILABLE:
        # Load unified persistence (runs migration if legacy keys found)
        try:
            await unified_persistence.load_all()
            log.info("Unified persistence loaded (migration applied if needed)")
        except Exception as exc:
            log.warning("Unified persistence load failed (non-fatal): %s", exc)

        # Initialize multi-instance orchestrator (loads credentials, connects clients)
        try:
            await _orchestrator.initialize()
            log.info(
                "InstanceOrchestrator initialized (%d instances available)",
                _orchestrator.available_count,
            )
        except Exception as exc:
            log.warning("InstanceOrchestrator initialization failed (non-fatal): %s", exc)

        # Start periodic health check for bot instances
        if not _orchestrator_health_started:
            _orchestrator_health_started = True
            bot.loop.create_task(
                _orchestrator_health_loop(), name="orchestrator-health"
            )
            log.info("orchestrator: health check loop started (30s interval)")


# ── file upload playback ────────────────────────────────────
# Plays audio/video attachments in messages that mention the bot, or that are
# posted in the voice channel's bound text channel, or in a channel where
# auto-play is configured (guild setting "file_autoplay"). The permission gate
# mirrors interaction_check (allowlist in allow_all mode, blacklist otherwise).

def _file_autoplay_channel(guild_id: int) -> int | None:
    """Return the bound text-channel id that should auto-play uploads, or None."""
    # Guild setting "file_autoplay_channel": the text channel whose uploads
    # should always be played. Falls back to the player's bound text channel.
    return _guild_settings.get_setting(guild_id, "file_autoplay_channel", None)


def _should_process_upload(message: discord.Message) -> bool:
    """Decide whether an upload-bearing message should trigger playback."""
    if not message.guild:
        return False
    gid = message.guild.id
    state = player.get_state(gid)
    # 1. Bot was @mentioned.
    if message.mentions and bot.user in message.mentions:
        return True
    # 2. Message is in the voice channel's bound text channel.
    bound = state.get("text_channel")
    if bound is not None and message.channel.id == bound.id:
        return True
    # 3. Guild has an auto-play text channel configured for uploads.
    auto_ch = _file_autoplay_channel(gid)
    if auto_ch is not None and message.channel.id == auto_ch:
        return True
    return False


def _user_allowed(message: discord.Message) -> bool:
    """Apply the same allowlist/blacklist gate as interaction_check."""
    if not message.guild:
        return True
    gid = message.guild.id
    uid = message.author.id
    mode = get_guild_mode(gid)
    if mode == "allow_all":
        return is_allowed(gid, uid)
    return not is_blacklisted(gid, uid)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not message.attachments:
        return
    if message.guild is None:
        return

    # Guild authorization gate: never operate on servers where no bot
    # administrator is a member (see guild_policy.py).
    if not await _guild_policy.is_authorized(message.guild.id):
        log.info("file_handler: upload in unauthorized guild %s ignored",
                 message.guild.id)
        return

    if not _should_process_upload(message):
        return
    if not _user_allowed(message):
        log.info("file_handler: upload by disallowed user %s in guild %s ignored",
                 message.author.id, message.guild.id)
        return

    gid = message.guild.id
    state = player.get_state(gid)
    player_obj = state.get("player")

    # If no player is connected, join the uploader's voice channel if present.
    if not player_obj or not player_obj.connected:
        if not message.author.voice:
            await message.channel.send("HelloDJ needs you in a voice channel to play that file.")
            return
        try:
            channel = message.author.voice.channel
            state["voice_channel"] = channel
            state["text_channel"] = message.channel
            state["persist_enabled"] = True
            player_obj = await player.connect_player(channel)
            state["player"] = player_obj
        except Exception as exc:
            log.error("file_handler: could not connect voice for guild %s: %s", gid, exc)
            await message.channel.send("HelloDJ could not join the voice channel.")
            return

    for attachment in message.attachments:
        try:
            info = await file_handler.process_upload(attachment, player_obj, message.channel)
        except ValueError as exc:
            log.info("file_handler: ignored %s: %s", getattr(attachment, "filename", "?"), exc)
            await message.channel.send(
                f"HelloDJ couldn't play **{getattr(attachment, 'filename', '?')}** — {exc}"
            )
            # React ⚠️ to unsupported/unknown file uploads.
            try:
                await message.add_reaction("⚠️")
            except Exception as exc2:
                log.warning("on_message: could not add ⚠️ reaction: %s", exc2)
            continue
        except Exception as exc:
            log.error("file_handler: upload failed for %s: %s",
                      getattr(attachment, "filename", "?"), exc)
            await message.channel.send(
                f"HelloDJ couldn't play **{getattr(attachment, 'filename', '?')}** — {exc}"
            )
            continue

        if info is None:
            # Image attachments are silently ignored — no chat message, no error.
            # No reaction for images; only directly-uploaded audio/video gets 🎵.
            continue

        played = await file_handler.play_uploaded_file(gid, player_obj, info["playable_path"], info["title"])

        # React 🎵 to successfully-uploaded audio/video files (once per message).
        try:
            await message.add_reaction("🎵")
        except Exception as exc:
            log.warning("on_message: could not add 🎵 reaction: %s", exc)

        # Confirmation embed showing what was detected and added.
        embed = discord.Embed(
            title="🎵 HelloDJ — Upload Added",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(name="File", value=info["title"], inline=True)
        embed.add_field(name="Type", value=info["media_type"], inline=True)
        embed.add_field(name="Status", value="Playing" if played else "Queued (playback pending)", inline=True)
        embed.set_footer(text="HelloDJ file upload — press nothing, it's already playing")
        await message.channel.send(embed=embed)


# ── guild authorization policy ─────────────────────────────
# HelloDJ may only operate on a guild where at least one bot administrator
# (the owner, or an OAuth-bound admin) is a member. On join we check membership
# and leave unauthorized servers; we also re-check existing guilds at startup
# and periodically so the policy stays current as admins join/leave.

async def _leave_unauthorized_guild(guild: discord.Guild) -> None:
    """Politely notify and leave a guild that is denied."""
    gid = int(guild.id)
    try:
        system_channel = getattr(guild, "system_channel", None)
        if system_channel is not None and system_channel.permissions_for(guild.me).send_messages:
            await system_channel.send(
                "HelloDJ is not approved for this server. "
                "A HelloDJ administrator must approve it via the admin portal."
            )
    except Exception as exc:
        log.info("guild_policy: could not notify guild %s before leave: %s", gid, exc)

    try:
        await guild.leave()
        log.warning("HelloDJ left denied guild %s (%s)", guild.name, gid)
    except Exception as exc:
        log.error("guild_policy: could not leave guild %s (%s): %s", guild.name, gid, exc)


async def _notify_pending_guild(guild: discord.Guild) -> None:
    """Notify a guild that it's pending approval."""
    try:
        system_channel = getattr(guild, "system_channel", None)
        if system_channel is not None and system_channel.permissions_for(guild.me).send_messages:
            await system_channel.send(
                "👋 HelloDJ has joined but is **pending approval**. "
                "A HelloDJ administrator must approve this server via the admin portal. "
                "If not approved within 24 hours, HelloDJ will leave automatically.\n\n"
                "Commands are disabled until approved."
            )
    except Exception:
        pass


async def _recheck_guilds() -> None:
    """Re-check every guild the bot is in.

    - Approved guilds stay as-is.
    - Pending guilds are left alone (watchdog handles expiry).
    - Denied guilds are left.
    - Unknown guilds (no entry) get set to pending.
    """
    for guild in list(bot.guilds):
        try:
            status = await _guild_policy.check_guild(guild)
            if status == "denied":
                await _leave_unauthorized_guild(guild)
        except Exception as exc:
            log.error("guild_policy: recheck failed for guild %s (%s): %s",
                      getattr(guild, "name", "?"), getattr(guild, "id", "?"), exc)


async def _guild_policy_watchdog() -> None:
    """Periodically expire pending guilds and re-check denied ones."""
    interval = 3600  # every hour
    while True:
        await asyncio.sleep(interval)
        try:
            await _guild_policy.expire_pending_guilds(bot)
            await _recheck_guilds()
        except Exception:
            log.exception("guild_policy: watchdog iteration error")


@bot.event
async def on_guild_join(guild: discord.Guild):
    log.info("HelloDJ joined guild %s (%s)", guild.name, guild.id)
    try:
        status = await _guild_policy.check_guild(guild)
        if status == "pending":
            await _notify_pending_guild(guild)
        elif status == "denied":
            await _leave_unauthorized_guild(guild)
        # "approved" -> do nothing, bot is welcome
    except Exception as exc:
        log.error("guild_policy: join check failed for guild %s (%s): %s",
                  guild.name, guild.id, exc)
    await oauth_store.write_guilds(_build_guilds_data())


@bot.event
async def on_guild_remove(guild: discord.Guild):
    log.info("HelloDJ removed from guild %s (%s)", guild.name, guild.id)
    await _guild_policy.clear(guild.id)
    await oauth_store.write_guilds(_build_guilds_data(), force=True)


# ── global app-command error handler (safety net) ──────────
# Prevents any deferred command from leaving Discord stuck at "thinking..."
# when an unhandled exception escapes to the command tree.

@bot.tree.error
async def on_error(interaction: discord.Interaction, error: Exception) -> None:
    from discord.app_commands import CommandNotFound
    if isinstance(error, CommandNotFound):
        # Stale cached global command — tell user to refresh
        try:
            await interaction.response.send_message(
                "⚠️ Commands just updated — please close and re-open Discord, "
                "or press Ctrl+R to refresh. Then try again.",
                ephemeral=True,
            )
        except Exception:
            pass
        return

    log.exception("App-command error in %s: %s", interaction.command, error)
    try:
        if interaction.response.is_deferred():
            await interaction.followup.send(
                f"An error occurred: {error}", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"An error occurred: {error}", ephemeral=True
            )
    except Exception:
        # The interaction may already be expired or un-replyable; nothing more to do.
        pass


# ── wavelink event listeners ───────────────────────────────

@bot.event
async def on_wavelink_track_start(payload: wavelink.TrackStartEventPayload):
    wv_player = payload.player
    track = payload.track
    if wv_player is None:
        return
    guild_id = int(wv_player.guild.id)
    dbg.event("track_start", guild_id=guild_id,
              title=getattr(track, "title", None),
              uri=getattr(track, "uri", None),
              author=getattr(track, "author", None),
              length=getattr(track, "length", None),
              source=getattr(track, "source", None))
    await player.on_track_start(guild_id, wv_player, track)


@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    wv_player = payload.player
    track = payload.track
    reason = payload.reason
    if wv_player is None:
        return
    guild_id = int(wv_player.guild.id)
    dbg.event("track_end", guild_id=guild_id,
              title=getattr(track, "title", None),
              reason=reason)
    await player.on_track_end(guild_id, wv_player, track, reason)


@bot.event
async def on_wavelink_track_exception(payload: wavelink.TrackExceptionEventPayload):
    wv_player = payload.player
    track = payload.track
    exception = payload.exception
    if wv_player is None:
        return
    guild_id = int(wv_player.guild.id)
    dbg.event("track_exception", guild_id=guild_id,
              title=getattr(track, "title", None),
              exception=str(exception))
    await player.on_track_exception(guild_id, wv_player, track, exception)


# ── main ───────────────────────────────────────────────────

token = cfg("discord.token")
if not token:
    raise SystemExit(
        "discord.token is not set in the credential store. "
        "Run migrate_to_db.py or set it via the web UI."
    )

bot.run(token)
