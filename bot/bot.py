"""HelloDJ — Entry point: loads cogs, configures Lavalink/wavelink, syncs slash commands, runs the bot."""

import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv
import wavelink

import player
import session
import storage
import blacklist as _blacklist

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

intents = discord.Intents.default()
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
    node = wavelink.Node(
        uri=LAVALINK_URI,
        password=LAVALINK_PASSWORD,
    )
    await wavelink.Pool.connect(nodes=[node], client=bot)
    log.info("HelloDJ connected to Lavalink at %s", LAVALINK_URI)


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

    await bot.tree.sync()
    print("HelloDJ slash commands synced.")


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
            log.warning("Could not resume session for guild %s: %s", gid_str, exc)


@bot.event
async def on_ready():
    print(f"HelloDJ logged in as {bot.user} ({bot.user.id})")
    global _resumed
    if not _resumed:
        _resumed = True
        await _resume_sessions()


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
