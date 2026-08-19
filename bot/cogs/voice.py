"""HelloDJ — Voice Activation Cog.

Provides the `/voice` toggle command and wires the voice activation pipeline
to Discord voice events. Incoming audio is received via a hybrid voice player
(wavelink + discord.ext.voice_recv) through a custom AudioSink.
"""

import asyncio
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

import player
from voice.voice_commands import VoiceCommandOrchestrator
from voice.wakeword import WakeWordModel

log = logging.getLogger(__name__)

try:
    from discord.ext import voice_recv
    _VOICE_RECV_AVAILABLE = True
except ImportError:
    voice_recv = None  # type: ignore[assignment]
    _VOICE_RECV_AVAILABLE = False


def _build_sink_class():
    """Build the PipelineSink class (requires the voice_recv extension)."""
    from discord.ext import voice_recv

    class PipelineSink(voice_recv.AudioSink):
        """An AudioSink that pushes each incoming audio packet into the pipeline.

        ``wants_opus()`` returns True so we receive the raw Opus frames (the
        pipeline decodes them itself, preserving the exact 20 ms framing).
        """

        def __init__(self, orchestrator, guild_id: int):
            super().__init__()
            self._orchestrator = orchestrator
            self._guild_id = guild_id
            # voice_recv dispatches write() from a worker thread, so we must
            # schedule coroutines onto the bot's event loop thread-safely.
            self._loop = orchestrator.bot.loop if orchestrator is not None else None

        def wants_opus(self) -> bool:
            return True

        def write(self, user, data) -> None:
            """Called for each received Opus packet (raw, not decoded)."""
            if self._orchestrator is None or user is None:
                return
            # data.opus is the raw Opus payload; data.packet.ssrc is the source.
            opus = data.opus
            ssrc = data.packet.ssrc
            # Schedule the async handling on the bot loop.
            self._schedule(opus, ssrc, user.id)

        def cleanup(self) -> None:
            self._orchestrator = None

        def _schedule(self, opus: bytes, ssrc: int, user_id: int) -> None:
            import asyncio
            if self._loop is None or self._orchestrator is None:
                return
            # run_coroutine_threadsafe is safe to call from the voice_recv
            # worker thread; it schedules onto the bot's asyncio loop.
            fut = asyncio.run_coroutine_threadsafe(
                self._orchestrator.on_voice_receive(
                    self._guild_id, ssrc, opus, user_id
                ),
                self._loop,
            )
            # Retrieve the result so coroutine exceptions are surfaced (and not
            # silently dropped as "Task exception was never retrieved").
            fut.add_done_callback(lambda f: f.exception())

    return PipelineSink


PipelineSink = _build_sink_class() if _VOICE_RECV_AVAILABLE else None

# Type alias for annotation purposes (the class is built at runtime).
PipelineSinkType = type(PipelineSink) if PipelineSink is not None else object


# ── Voice Cog ────────────────────────────────────────────────────────────

class VoiceCog(commands.Cog):
    """Cog that enables/disables voice activation per guild."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._orchestrator: VoiceCommandOrchestrator | None = None
        self._tick_task: asyncio.Task | None = None
        self._enabled_guilds: set[int] = set()
        self._disabled_guilds: set[int] = set()
        self._sinks: dict[int, object] = {}
        # Master switch: when VOICE_ENABLED=true the bot listens by default in
        from config import cfg
        self._voice_enabled = cfg.bool("voice.enabled")
        if self._voice_enabled:
            log.info("VOICE_ENABLED=true — voice activation auto-enabled for all guilds")

    def _should_listen(self, guild_id: int) -> bool:
        """Effective listening decision for a guild.

        Order of precedence:
          1. A guild explicitly toggled off via /voice disable never listens.
          2. VOICE_ENABLED=true enables listening by default everywhere.
          3. Otherwise fall back to the per-guild /voice toggle (_enabled_guilds).
        """
        if guild_id in self._disabled_guilds:
            return False
        if self._voice_enabled:
            return True
        return guild_id in self._enabled_guilds

    async def setup_orchestrator(self) -> None:
        """Initialize the voice activation pipeline."""
        wakeword = WakeWordModel()
        self._orchestrator = VoiceCommandOrchestrator(wakeword, self.bot)

        if not wakeword.available:
            log.warning(
                "Wake word model not found — voice activation disabled. "
                "Set WAKE_WORD_MODEL_PATH or place Hello_DJ.onnx in /app/models/"
            )

        log.info(
            "Voice orchestrator initialized (wakeword=%s, tts=%s, query=%s)",
            wakeword.available,
            self._orchestrator.tts.available,
            self._orchestrator.query.available,
        )

    # ── slash commands ───────────────────────────────────────────────────

    @app_commands.command(
        name="voice",
        description="Toggle voice activation (wake word + voice commands)",
    )
    @app_commands.choices(
        state=[
            app_commands.Choice(name="Enable", value="enable"),
            app_commands.Choice(name="Disable", value="disable"),
        ]
    )
    async def voice_toggle(
        self, interaction: discord.Interaction,
        state: str,
    ) -> None:
        """Enable or disable voice activation for this guild."""
        guild_id = interaction.guild.id

        if state == "enable":
            self._enabled_guilds.add(guild_id)
            self._disabled_guilds.discard(guild_id)
            self._start_receive(guild_id)
            await interaction.response.send_message(
                "HelloDJ voice activation **enabled**. "
                "Say \"Hello DJ\" followed by a command.",
                ephemeral=True,
            )
        else:
            self._enabled_guilds.discard(guild_id)
            self._disabled_guilds.add(guild_id)
            self._stop_receive(guild_id)
            await interaction.response.send_message(
                "HelloDJ voice activation **disabled**.",
                ephemeral=True,
            )

    # Intentional naming exception: /voice_status keeps its underscore name.
    # Converting to a space-group (/voice status) would break the existing
    # /voice enable|disable toggle command (voice.py:142), so this low-risk
    # underscore name is retained for parity-without-collision.

    @app_commands.command(
        name="voice_status",
        description="Show voice activation status",
    )
    async def voice_status(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild.id
        enabled = self._should_listen(guild_id)
        wakeword = (self._orchestrator.pipeline.wakeword.available
                     if self._orchestrator and self._orchestrator.pipeline.wakeword
                     else False)

        embed = discord.Embed(
            title="🎙️ HelloDJ Voice Status",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(name="Voice Activation", value="✅ Enabled" if enabled else "❌ Disabled", inline=True)
        embed.add_field(name="Wake Word Model", value="✅ Loaded" if wakeword else "❌ Missing", inline=True)
        embed.add_field(name="TTS Engine", value="✅ Ready" if (self._orchestrator and self._orchestrator.tts.available) else "❌ Unavailable", inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── voice state listener (wire receive when bot joins voice) ─────────

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member,
        _before: discord.VoiceState,
        _after: discord.VoiceState,
    ) -> None:
        """When the bot joins a voice channel, start receiving Opus frames."""
        if member.id != self.bot.user.id:
            return
        # Diagnosis: prove the receive-sink registration race. At the instant the
        # bot's OWN voice-state event fires, is state["player"] already populated?
        # connect_player returns the player to callers who store it into
        # state["player"] AFTER connect() returns, so this event usually races
        # ahead of that assignment.
        try:
            gid = member.guild.id
            st = player.get_state(gid)
            pobj = st.get("player")
            log.info(
                "on_voice_state_update bot self (guild_id=%s) before=%s after=%s "
                "state_player=%s player_connected=%s sinks=%s listen=%s",
                gid,
                _before.channel.id if _before.channel else None,
                _after.channel.id if _after.channel else None,
                pobj is not None,
                getattr(pobj, "connected", False) if pobj else False,
                list(self._sinks.keys()),
                getattr(pobj, "is_listening", lambda: False)() if pobj else False,
            )
        except Exception:
            log.exception("on_voice_state_update diag failed")
        if _after.channel is None:
            # Bot left voice — stop receiving
            self._stop_receive(member.guild.id)
            return
        if self._should_listen(member.guild.id):
            self._start_receive(member.guild.id)

    # ── sink wiring (uses the hybrid player's listen()) ─────────────────

    def _start_receive(self, guild_id: int) -> None:
        """Register the pipeline sink on the guild's hybrid player.

        Retries up to a short window: the bot's own voice-state event can fire
        while connect_player is still mid-handshake, so the player may exist but
        its real voice socket may not be connected yet (voice_recv's listen()
        requires a connected socket). We keep trying until listen() succeeds or
        the window elapses.
        """
        if not _VOICE_RECV_AVAILABLE:
            log.warning("voice_recv not installed — cannot receive voice audio")
            return
        if guild_id in self._sinks:
            return

        state = player.get_state(guild_id)
        player_obj = state.get("player")
        if player_obj is None:
            log.info("No player yet for guild %s — receive will start on connect", guild_id)
            return
        if self._orchestrator is None:
            return

        # Retry until the player's real voice connection is ready (listen() needs
        # is_connected() True). connect_player now stores the player into state
        # immediately, so player_obj may exist while the handshake is still
        # completing. Because this is a sync listener, schedule the retry as a
        # background task rather than blocking the event loop.
        connected = getattr(player_obj, "connected", False)
        conn = getattr(player_obj, "_connection", None)
        conn_ok = False
        if conn is not None and not isinstance(conn, str):
            try:
                conn_ok = conn.is_connected()
            except Exception:
                conn_ok = False
        if not (connected or conn_ok):
            log.info(
                "start_receive (guild_id=%s) player not connected yet "
                "(connected=%s conn_ok=%s) — scheduling retry task",
                guild_id, connected, conn_ok,
            )
            import asyncio
            asyncio.ensure_future(self._retry_start_receive(guild_id))
            return

        # Diagnosis: log the real voice-connection state before attempting
        # listen(). voice_recv.VoiceRecvClient.listen() calls is_connected() ->
        # self._connection.is_connected(). A wavelink-only forward (Lavalink
        # PATCH, no real socket) leaves _connection unconnected, so listen()
        # raises ClientException('Not connected to voice.') — proving the sink
        # can never attach.
        try:
            ptype = type(player_obj).__name__
            conn = getattr(player_obj, "_connection", None)
            conn_ok = False
            # conn may be MISSING sentinel or a VoiceConnectionState
            if conn is not None and not isinstance(conn, str):
                try:
                    conn_ok = conn.is_connected()
                except Exception:
                    conn_ok = False
            log.info(
                "start_receive diag (guild_id=%s) player_type=%s connected_prop=%s "
                "_connection=%s conn.is_connected=%s",
                guild_id, ptype,
                getattr(player_obj, "connected", False),
                conn if conn is not None else "MISSING",
                conn_ok,
            )
        except Exception:
            log.exception("start_receive diag failed")

        try:
            if PipelineSink is None:  # voice_recv unavailable
                return
            sink = PipelineSink(self._orchestrator, guild_id)
            player_obj.listen(sink)
            self._sinks[guild_id] = sink
            log.info("Voice receiver started for guild %s", guild_id)
        except Exception as exc:
            log.warning("Could not start voice receiver: %s", exc)

    async def _retry_start_receive(self, guild_id: int) -> None:
        """Background retry: wait for the player's voice socket, then listen()."""
        import asyncio
        for _ in range(8):
            if guild_id in self._sinks:
                return
            state = player.get_state(guild_id)
            player_obj = state.get("player")
            if player_obj is None:
                return
            connected = getattr(player_obj, "connected", False)
            conn = getattr(player_obj, "_connection", None)
            conn_ok = False
            if conn is not None and not isinstance(conn, str):
                try:
                    conn_ok = conn.is_connected()
                except Exception:
                    conn_ok = False
            if connected or conn_ok:
                break
            await asyncio.sleep(1.0)
        if guild_id in self._sinks:
            return
        player_obj = player.get_state(guild_id).get("player")
        if player_obj is None or self._orchestrator is None:
            return
        try:
            if PipelineSink is None:
                return
            sink = PipelineSink(self._orchestrator, guild_id)
            player_obj.listen(sink)
            self._sinks[guild_id] = sink
            log.info("Voice receiver started for guild %s (via retry)", guild_id)
        except Exception as exc:
            log.warning("Could not start voice receiver (retry): %s", exc)

    def _stop_receive(self, guild_id: int) -> None:
        """Unregister the pipeline sink for a guild."""
        sink = self._sinks.pop(guild_id, None)
        state = player.get_state(guild_id)
        player_obj = state.get("player")
        if player_obj is not None:
            try:
                player_obj.stop_listening()
            except Exception:
                pass

    # ── tick loop ────────────────────────────────────────────────────────

    async def _tick_loop(self):
        """Background task: run wake word detection every 80ms."""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            await asyncio.sleep(0.08)  # 80ms
            if self._orchestrator is None:
                continue
            # Only run wake-word inference when we're actually receiving voice
            # audio somewhere — don't burn CPU when there's no voice connection.
            if not self._sinks:
                continue
            await self._orchestrator.tick()

    def start_tick_loop(self) -> None:
        """Start the background tick loop."""
        if self._tick_task is None or self._tick_task.done():
            self._tick_task = asyncio.ensure_future(self._tick_loop())
            log.info("Voice tick loop started (every 80ms)")

    # ── cog unload cleanup ───────────────────────────────────────────────

    async def cog_unload(self) -> None:
        if self._tick_task:
            self._tick_task.cancel()
        for guild_id in list(self._sinks):
            self._stop_receive(guild_id)


async def setup(bot: commands.Bot):
    """Load the Voice cog and initialize the orchestrator."""
    cog = VoiceCog(bot)

    # Initialize the orchestrator (loads wake word model, STT, TTS)
    await cog.setup_orchestrator()

    # Add the cog
    await bot.add_cog(cog)

    # Start the tick loop
    cog.start_tick_loop()

    log.info("Voice activation cog loaded")
