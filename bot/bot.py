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

# ── YouTube poToken (Proof of Origin) ───────────────────────
# Optional values supplied to the youtube-source plugin to defeat YouTube's
# bot-detection ("Sign in to confirm you're not a bot" / "The page needs to be
# reloaded" / "No supported audio streams available") for the non-OAuth
# WEB-family clients. Generate a fresh token via
# https://github.com/iv-org/youtube-trusted-session-generator and set these env
# vars (bot-configmap / docker-compose). Blank values disable the push.
POT_TOKEN = os.getenv("POT_TOKEN", "")
POT_VISITOR_DATA = os.getenv("POT_VISITOR_DATA", "")

# ── Spotify credentials (optional) ─────────────────────────

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")

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
    await push_youtube_oauth()
    # Push the optional poToken/visitorData (Proof of Origin) to the same
    # endpoint. poToken defeats YouTube bot-detection for the non-OAuth
    # WEB-family clients and complements OAuth (TV). Blank values are a no-op.
    await push_youtube_pot()


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


async def push_youtube_pot() -> bool:
    """Push the optional YouTube poToken/visitorData (Proof of Origin) to Lavalink.

    The youtube-source plugin's ``POST /youtube`` endpoint accepts a ``poToken``
    + ``visitorData`` pair to defeat YouTube's bot-detection for the non-OAuth
    WEB-family clients (WEB, WEBEMBEDDED, TVHTML5_SIMPLY) — the errors "Sign in
    to confirm you're not a bot" / "The page needs to be reloaded" /
    "No supported audio streams available" seen in the live log. It complements
    OAuth (which only benefits the TV client). Per the plugin docs, poToken and
    OAuth are mutually exclusive per-request, so this push sets ``refreshToken``
    to ``"x"`` to leave the OAuth token untouched.

    Values come from the POT_TOKEN / POT_VISITOR_DATA env vars. Blank values are
    a no-op (returns False). Never raises.
    """
    if not POT_TOKEN or not POT_VISITOR_DATA:
        log.info("youtube-pot: no poToken configured — skipping push to Lavalink")
        return False

    url = f"{LAVALINK_URI}/youtube"
    payload = {
        "refreshToken": "x",  # do not update OAuth; poToken only
        "skipInitialization": False,
        "poToken": POT_TOKEN,
        "visitorData": POT_VISITOR_DATA,
    }
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
                        "youtube-pot: pushed poToken to Lavalink %s (status=%s) body=%s",
                        url, resp.status, body,
                    )
                    return True
                log.warning(
                    "youtube-pot: Lavalink push failed (status=%s) body=%s",
                    resp.status, body,
                )
    except Exception as exc:
        log.warning("youtube-pot: push to Lavalink failed: %s", exc)
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
            if token:
                await push_youtube_oauth()
            # poToken/visitorData are static env values; re-push on each tick so
            # a value set while the bot runs is applied without a restart.
            await push_youtube_pot()
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
    _blacklist.load_track_blacklist()
    _allowlist.load()
    _guild_settings.load()
    _sleep_settings.load()
    _metrics.load()
    _guild_policy.load()

    # Clean up old uploaded files (default 24h) on startup.
    file_handler.cleanup_old_files()

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
    await bot.load_extension("cogs.help")
    await bot.load_extension("cogs.radio")
    await bot.load_extension("cogs.voice")
    await bot.load_extension("cogs.stream")

    # ── switchable voice-connect debug layer ─────────────────
    # Installs a socket raw-listener that logs whether the bot's OWN
    # VOICE_STATE_UPDATE / VOICE_SERVER_UPDATE events arrive after op-4,
    # and whether a voice client was registered at arrival time. On when
    # HELLODJ_VOICE_DEBUG=1 (default); set to 0 to disable.
    voice_debug.install_raw_listeners(bot)

    await bot.tree.sync()
    log.info("HelloDJ slash commands synced.")


bot.setup_hook = setup_hook

# ── global permission check (blacklist + allowlist + mode) ──

async def permission_check(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return True

    gid = interaction.guild.id
    uid = interaction.user.id

    # Guild authorization gate: refuse to operate on servers where no bot
    # administrator is a member (see guild_policy.py).
    if not await _guild_policy.is_authorized(gid):
        await interaction.response.send_message(
            "This server isn't authorized — no HelloDJ administrator is a member. "
            "Ask a HelloDJ admin to join before using it.",
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
            saved_sp = saved.get("source_provider", "youtube")
            state["source_provider"] = saved_sp
            state["repeat_mode"] = saved.get("repeat_mode", "off")
            state["crossfade_seconds"] = saved.get("crossfade_seconds", 0.0)

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
    global _guild_policy_task_started
    _ready_at = time.monotonic()
    log.info("HelloDJ logged in as %s (%s)", bot.user, bot.user.id)
    log.info("on_ready fired with %d guilds", len(bot.guilds))
    # Re-check every guild the bot is already in against admin membership
    # (startup re-check; unauthorized guilds are left).
    await _recheck_guilds()
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
    # Start the periodic guild-authorization re-check so the policy stays
    # current as admins join/leave guilds while the bot runs.
    if not _guild_policy_task_started:
        _guild_policy_task_started = True
        bot.loop.create_task(_guild_policy_watchdog())
        log.info("guild_policy: periodic re-check watchdog started")


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
    """Politely notify and leave a guild that has no bot-administrator member."""
    gid = int(guild.id)
    # Try to send a polite explanation first (best-effort; may fail if the bot
    # cannot message in any channel).
    try:
        system_channel = getattr(guild, "system_channel", None)
        if system_channel is not None and system_channel.permissions_for(guild.me).send_messages:
            await system_channel.send(
                "HelloDJ left this server because no HelloDJ administrator is a "
                "member. Ask a HelloDJ admin to join before re-inviting."
            )
    except Exception as exc:
        log.info("guild_policy: could not notify guild %s before leave: %s", gid, exc)

    try:
        await guild.leave()
        log.warning("HelloDJ left unauthorized guild %s (%s)", guild.name, gid)
    except Exception as exc:
        log.error("guild_policy: could not leave guild %s (%s): %s", guild.name, gid, exc)


async def _recheck_guilds() -> None:
    """Re-check every guild the bot is in against admin membership.

    Any guild whose membership check now fails is marked unauthorized and left.
    Runs at startup (on_ready) and periodically (watchdog).
    """
    for guild in list(bot.guilds):
        try:
            authorized = await _guild_policy.check_guild(guild)
            if not authorized:
                await _leave_unauthorized_guild(guild)
        except Exception as exc:
            log.error("guild_policy: recheck failed for guild %s (%s): %s",
                      getattr(guild, "name", "?"), getattr(guild, "id", "?"), exc)


async def _guild_policy_watchdog() -> None:
    """Periodically re-check guild authorization (admins join/leave over time)."""
    interval = int(os.getenv("GUILD_POLICY_RECHECK_INTERVAL", "3600"))
    while True:
        await asyncio.sleep(interval)
        try:
            await _recheck_guilds()
        except Exception:
            log.exception("guild_policy: watchdog iteration error")


@bot.event
async def on_guild_join(guild: discord.Guild):
    log.info("HelloDJ joined guild %s (%s)", guild.name, guild.id)
    try:
        authorized = await _guild_policy.check_guild(guild)
        if not authorized:
            await _leave_unauthorized_guild(guild)
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
