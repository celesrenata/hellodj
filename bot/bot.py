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
import blacklist as _blacklist
import oauth_store
import permissions
import voice_debug

load_dotenv()

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

# ── Lavalink config ────────────────────────────────────────

LAVALINK_HOST = os.getenv("LAVALINK_HOST", "losingtime.dpaste.org")
LAVALINK_PORT = int(os.getenv("LAVALINK_PORT", "2124"))
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "SleepingOnTrains")
LAVALINK_URI = f"http://{LAVALINK_HOST}:{LAVALINK_PORT}"

# ── Spotify credentials (optional) ─────────────────────────

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

# ── Guild blacklist (shared with cogs/admin.py) ────────────

blacklist = _blacklist.blacklist
is_blacklisted = _blacklist.is_blacklisted


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
    await push_youtube_oauth()


async def push_youtube_oauth() -> bool:
    """Push the stored YouTube OAuth refresh token to Lavalink's youtube-source.

    The youtube-source plugin (dev.lavalink.youtube:youtube-plugin:1.18.2)
    authenticates YouTube requests with an OAuth refresh token supplied via its
    REST endpoint ``POST /youtube`` with JSON ``{"refreshToken": ..., "skipInitialization": false}``.
    The web-ui stores the token in ``data/oauth.json`` (providers.youtube.refresh_token)
    via its device flow; this shares the same NFS data mount.

    Returns True when a token was pushed successfully, False otherwise (no token
    stored, or a push failure). Never raises.
    """
    token = oauth_store.get_youtube_refresh_token()
    if not token:
        log.info("youtube-oauth: no stored refresh token — skipping push to Lavalink")
        return False

    url = f"{LAVALINK_URI}/youtube"
    payload = {"refreshToken": token, "skipInitialization": False}
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
                    log.info(
                        "youtube-oauth: pushed refresh token to Lavalink %s (status=%s) body=%s",
                        url, resp.status, body,
                    )
                    return True
                log.warning(
                    "youtube-oauth: Lavalink push failed (status=%s) body=%s",
                    resp.status, body,
                )
    except Exception as exc:
        log.warning("youtube-oauth: push to Lavalink failed: %s", exc)
    return False


async def _youtube_oauth_watchdog() -> None:
    """Periodically re-push the stored YouTube OAuth refresh token to Lavalink.

    The web-ui device flow writes providers.youtube.refresh_token into
    data/oauth.json at an arbitrary time. This loop re-reads it and pushes it to
    Lavalink's youtube-source REST endpoint so a token stored while the bot is
    running is applied without a restart.
    """
    interval = int(os.getenv("YOUTUBE_OAUTH_PUSH_INTERVAL", "60"))
    while True:
        await asyncio.sleep(interval)
        try:
            token = oauth_store.get_youtube_refresh_token()
            if not token:
                continue
            await push_youtube_oauth()
        except Exception:
            log.exception("youtube-oauth: watchdog iteration error")


async def disconnect_lavalink():
    """Disconnect all Lavalink nodes."""
    if wavelink.Pool.nodes:
        for node in wavelink.Pool.nodes.values():
            await node.close()
    log.info("HelloDJ disconnected from Lavalink")


# ── setup hook ─────────────────────────────────────────────

async def setup_hook():
    storage.load()
    session.load()
    oauth_store.load_oauth()
    _blacklist.load()

    # Connect to Lavalink
    await connect_lavalink()

    # Load custom cogs
    await bot.load_extension("cogs.music")
    await bot.load_extension("cogs.playlists")
    await bot.load_extension("cogs.filters")
    await bot.load_extension("cogs.autoplay")
    await bot.load_extension("cogs.admin")
    await bot.load_extension("cogs.lyrics")
    await bot.load_extension("cogs.info")
    await bot.load_extension("cogs.voice")

    # ── switchable voice-connect debug layer ─────────────────
    # Installs a socket raw-listener that logs whether the bot's OWN
    # VOICE_STATE_UPDATE / VOICE_SERVER_UPDATE events arrive after op-4,
    # and whether a voice client was registered at arrival time. On when
    # HELLODJ_VOICE_DEBUG=1 (default); set to 0 to disable.
    voice_debug.install_raw_listeners(bot)

    await bot.tree.sync()
    log.info("HelloDJ slash commands synced.")


bot.setup_hook = setup_hook

# ── global blacklist check ─────────────────────────────────

async def blacklist_check(interaction: discord.Interaction) -> bool:
    if interaction.guild and is_blacklisted(interaction.guild.id, interaction.user.id):
        await interaction.response.send_message(
            "You have been revoked from using HelloDJ.", ephemeral=True
        )
        return False
    return True

bot.interaction_check = blacklist_check


# ── resume sessions ────────────────────────────────────────

_resumed = False


async def _resume_sessions():
    for gid_str, saved in session.all().items():
        if not saved.get("auto_resume"):
            continue
        try:
            gid = int(gid_str)
            guild = bot.get_guild(gid)
            if guild is None:
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
            state["source_provider"] = saved.get("source_provider", "youtube")
            state["repeat_mode"] = saved.get("repeat_mode", "off")

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


@bot.event
async def on_ready():
    global _ready_at
    global _yt_oauth_task_started
    _ready_at = time.monotonic()
    log.info("HelloDJ logged in as %s (%s)", bot.user, bot.user.id)
    log.info("on_ready fired with %d guilds", len(bot.guilds))
    await oauth_store.write_guilds(_build_guilds_data(), force=True)
    global _resumed
    if not _resumed:
        _resumed = True
        await _resume_sessions()
    # Start the YouTube OAuth periodic re-push so a token stored via the web-ui
    # device flow is applied to Lavalink without a bot restart.
    if not _yt_oauth_task_started:
        _yt_oauth_task_started = True
        bot.loop.create_task(_youtube_oauth_watchdog())
        log.info("youtube-oauth: periodic re-push watchdog started")


@bot.event
async def on_guild_join(guild: discord.Guild):
    log.info("HelloDJ joined guild %s (%s)", guild.name, guild.id)
    await oauth_store.write_guilds(_build_guilds_data())


@bot.event
async def on_guild_remove(guild: discord.Guild):
    log.info("HelloDJ removed from guild %s (%s)", guild.name, guild.id)
    await oauth_store.write_guilds(_build_guilds_data(), force=True)


# ── global app-command error handler (safety net) ──────────
# Prevents any deferred command from leaving Discord stuck at "thinking..."
# when an unhandled exception escapes to the command tree.

@bot.tree.error
async def on_error(interaction: discord.Interaction, error: Exception) -> None:
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
    await player.on_track_start(guild_id, wv_player, track)


@bot.event
async def on_wavelink_track_end(payload: wavelink.TrackEndEventPayload):
    wv_player = payload.player
    track = payload.track
    reason = payload.reason
    if wv_player is None:
        return
    guild_id = int(wv_player.guild.id)
    await player.on_track_end(guild_id, wv_player, track, reason)


@bot.event
async def on_wavelink_track_exception(payload: wavelink.TrackExceptionEventPayload):
    wv_player = payload.player
    track = payload.track
    exception = payload.exception
    if wv_player is None:
        return
    guild_id = int(wv_player.guild.id)
    await player.on_track_exception(guild_id, wv_player, track, exception)


# ── main ───────────────────────────────────────────────────

token = os.getenv("DISCORD_TOKEN")
if not token:
    raise SystemExit(
        "DISCORD_TOKEN is not set. Copy .env.example to .env and add your bot token."
    )

bot.run(token)
