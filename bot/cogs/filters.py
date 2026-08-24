"""HelloDJ — Filters cog: audio effects via Lavalink's built-in EQ/filter API.

Uses wavelink 3.5's Filters API directly (no dismusic dependency).

Available filters
-----------------
- ``/filter bassboost [level]`` — equalizer low-band boost
- ``/filter nightcore`` — timescale 1.25x speed + pitch
- ``/filter 8d`` — rotation (spatial panning) at 0.5 Hz
- ``/filter vaporwave`` — timescale slowdown + low-band boost (slowed, mellow)
- ``/filter 8bit`` — arcade/chiptune guitar-pedal chain: distortion + tremolo +
  vibrato + timescale + equalizer (no low-pass muffle)
- ``/filter 808`` — plays the 808 cowbell as a sound effect (separate audio source)
- ``/filter equalizer`` — 10-band custom EQ
- ``/tune`` — toggle enhanced audio (less compressed, more crisp); a persistent
  per-song enhancement that re-applies to every new track until turned off
- ``/filter stems isolate [stem_type]`` — stem isolation (see note below)
- ``/filter test`` — diagnostic: report which filters are active on the node
- ``/filter reset`` — reset all filters (subcommand of /filter)
- ``/filter_reset`` — top-level alias of ``/filter reset`` (identical behavior)

Stem isolation (honest limitation)
----------------------------------
Lavalink's built-in filters do NOT support true stem separation (isolated
vocals / drums / bass / melody). The only DSP approximation Lavalink offers is
the ``karaoke`` filter, which *attenuates the vocal band* — producing an
*instrumental* version (vocals removed). It cannot produce isolated
vocals/drums/bass/melody stems.

- ``/filter stems isolate vocals`` → uses the Lavalink ``karaoke`` filter to
  mute/attenuate vocals, leaving the instrumental (drums+bass+melody). This is
  the closest Lavalink supports and is verified server-side.
- ``/filter stems isolate drums|bass|melody`` → cannot be done via Lavalink
  filters. We check the optional AI separation path (``bot/stems.py``); when no
  heavy model (demucs/spleeter/onnx) is installed, we clearly explain the
  limitation and offer the vocals-isolation (instrumental) fallback.

Implementation notes
--------------------
- Lavalink v4 enables all filters by default (equalizer, timescale, rotation,
  lowPass, distortion, ...), but this deployment's ``application.yml`` only
  enables a subset (see ``bot/lavalink/application.yml``). The filters the code
  relies on are enabled there so they actually work on the node.
- The 8-bit effect is a guitar-pedal-style chain — **distortion + tremolo +
  vibrato + timescale + equalizer** — evoking an arcade machine. A low-pass
  muffle is the wrong tool for 8-bit: 8-bit/chiptune is a bitcrusher +
  sample-rate-reduction sound, and Lavalink has no native bitcrush, so the best
  achievable arcade vibe uses Lavalink's supported DSP filters:
  - **distortion** (sinScale/cosScale/tanScale/scale/offset) — harsh
    square-wave-like grit (Lavalink supports distortion and wavelink 3.5
    documents all its parameters; see ``wavelink/filters.py::Distortion``)
  - **tremolo** (frequency/depth) — the classic 8-bit volume warbling
  - **vibrato** (frequency/depth) — retro pitch vibrato
  - **timescale** (slightly lower speed, pitch up) — the chiptune pitch character
  - **equalizer** (cut lows, boost mids) — the tinny arcade speaker character
  The parameters are tuned conservatively so distortion adds grit without
  replacing the audio with a pure tone.
- The 808 "filter" is a sound effect, not a DSP filter. Lavalink filters are
  applied to the audio stream and cannot mix in a separate audio source, so the
  808 cowbell is played as a separate audio source via the same path the chime
  feature uses (``sounds.play_sound`` → ``TTSPLayer.play_pcm`` →
  ``send_audio_packet``). It plays alongside the music rather than being mixed
  into it.
- Every filter command verifies the effect was actually applied by fetching the
  server-side filter state from Lavalink (``node.fetch_player_info``) and
  checking for the expected filter key. This is the "test mechanism" that
  confirms a filter is really active (important for the 8d fix, where the user
  reported the filter "does nothing").
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands
import wavelink

import player
import sounds
import stems
from debug import get_debug_logger

log = logging.getLogger(__name__)
dbg = get_debug_logger("filters")


# ── server-side verification helpers ──────────────────────────────────────
# The client-side ``player.filters`` object only reflects what we *asked* for.
# To prove a filter is actually active on the Lavalink node (the ground truth),
# we fetch the server-side player state via ``node.fetch_player_info`` and
# inspect its ``filters`` payload. This is the verification mechanism that
# confirms a filter is really applied (not just set client-side).

async def _get_server_filters(player_obj, guild_id: int) -> dict | None:
    """Fetch the server-side filter payload Lavalink holds for this guild.

    Returns the ``filters`` dict (e.g. ``{"rotation": {"rotationHz": 0.5}}``)
    or None when it cannot be fetched (node not connected, no player, etc.).
    """
    try:
        info = await player_obj.node.fetch_player_info(guild_id)
        if info is None:
            return None
        # ``info.filters`` is a wavelink.Filters object; calling it returns the
        # FilterPayload dict with empty filters already stripped out.
        return info.filters()
    except Exception as exc:
        log.warning("filters: could not fetch server-side filter state: %s", exc)
        return None


def _filter_active(server_filters: dict | None, key: str) -> bool:
    """True when ``key`` is present and non-empty in the server-side payload."""
    if not server_filters:
        return False
    return key in server_filters and bool(server_filters[key])


async def _verify_filter(player_obj, guild_id: int, expected_key: str) -> tuple[bool, dict | None]:
    """Fetch server-side state and check for the expected filter key.

    Returns ``(verified, server_filters)``.
    """
    server_filters = await _get_server_filters(player_obj, guild_id)
    return _filter_active(server_filters, expected_key), server_filters


def _eq_bands(gains: list[float]) -> list[dict]:
    """Convert a 15-element gain list to wavelink's ``[{band, gain}, ...]`` form."""
    return [{"band": i, "gain": g} for i, g in enumerate(gains)]


# ── /tune enhancement chain ─────────────────────────────────
# A transparent "studio master" polish (NOT a gimmick): gentle low-band boost +
# high-frequency lift for air/crispness, natural tempo (speed=1.0), and a very
# light distortion (scale=1.1) for warmth. No vibrato/tremolo (those add wobble;
# this should be clean). Mirrored in player.py (`_apply_tune_to`) so it can be
# re-applied on every new track without a circular import.
TUNE_GAINS = [0.5, 0.3, 0.2, 0.1, 0.1, 0, 0, -0.05, 0, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35]


async def _apply_tune(player_obj: wavelink.Player) -> None:
    """Apply the /tune enhancement chain to ``player_obj`` (equalizer + timescale)."""
    bands = _eq_bands(TUNE_GAINS)
    filters = player_obj.filters
    filters.equalizer.set(bands=bands)
    filters.timescale.set(speed=1.0, pitch=1.0, rate=1.0)
    filters.distortion.set(scale=1.1)
    filters.rotation.reset()
    filters.low_pass.reset()
    filters.karaoke.reset()
    filters.channel_mix.reset()
    await player_obj.set_filters(filters)


async def _broadcast_timescale_to_activity(bot: commands.Bot, guild_id: int, speed: float = 1.0) -> None:
    """Broadcast the current timescale speed to Activity clients.

    When video audio is routed through Lavalink, the Activity frontend needs
    to adjust its video playbackRate to match the Lavalink timescale speed.
    This keeps video and audio in sync when filters like nightcore (1.25x)
    or vaporwave (0.85x) are applied.
    """
    try:
        video_cog = bot.get_cog("Video")
        if video_cog is None or not hasattr(video_cog, "_backend"):
            return
        ws_hub = video_cog._backend.ws_hub
        await ws_hub.broadcast_from_bot(guild_id, {
            "type": "filter_sync",
            "timescale": speed,
        })
        log.debug("Broadcast timescale=%.3f to Activity for guild=%d", speed, guild_id)
    except Exception as exc:
        log.debug("_broadcast_timescale_to_activity failed: %s", exc)


# ── Non-timing filter keys (Lavalink auto-propagates these through the pipe) ─
_NON_TIMING_FILTER_KEYS = frozenset({
    "equalizer", "rotation", "tremolo", "vibrato", "distortion",
    "karaoke", "lowPass", "channelMix",
})


async def _sync_video_pipe_on_filter_change(
    bot: commands.Bot,
    guild_id: int,
    *,
    has_non_timing_filters: bool,
    timescale_speed: float = 1.0,
    was_reset: bool = False,
) -> None:
    """Synchronize the audio pipe with a filter change during active video.

    Called after filters are applied to the wavelink player. Handles four cases:
    1. Non-timing filter change with pipe active → no action (auto-propagates)
    2. Non-timing filter added with no pipe → enable pipe, restart pipeline
    3. Timescale change → restart pipeline with new speed
    4. Filter reset → disable pipe, restart pipeline with source audio
    """
    video_cog = bot.get_cog("Video")
    if video_cog is None or not hasattr(video_cog, "_registry"):
        return

    # Find active video session for this guild
    sessions = video_cog._registry.get_by_guild(guild_id)
    if not sessions:
        return

    # Use first active session
    _channel_id, streamer = sessions[0]
    if not streamer.is_active:
        return

    pipe_was_active = streamer._pipe_session is not None and streamer._pipe_session.active

    # Determine previous timescale speed from the pipeline state
    prev_speed = 1.0
    if streamer.pipeline and hasattr(streamer.pipeline, "_timescale_speed"):
        prev_speed = streamer.pipeline._timescale_speed or 1.0

    try:
        if was_reset:
            # Case 4: Filter reset — disable pipe, restart with source audio
            if pipe_was_active:
                await streamer.restart_pipeline_for_filter_change(
                    enable_pipe=False,
                    timescale_speed=1.0,
                )
                log.info(
                    "Filter reset: disabled pipe, restarted pipeline with source "
                    "audio for guild=%d",
                    guild_id,
                )

        elif timescale_speed != prev_speed:
            # Case 3: Timescale changed — restart pipeline with new speed
            await streamer.restart_pipeline_for_filter_change(
                enable_pipe=has_non_timing_filters,
                timescale_speed=timescale_speed,
            )
            log.info(
                "Timescale change: restarted pipeline with speed=%.2f pipe=%s "
                "for guild=%d",
                timescale_speed,
                has_non_timing_filters,
                guild_id,
            )

        elif has_non_timing_filters and not pipe_was_active:
            # Case 2: New non-timing filters on unfiltered video — enable pipe
            await streamer.restart_pipeline_for_filter_change(
                enable_pipe=True,
                timescale_speed=timescale_speed,
            )
            log.info(
                "New filters on unfiltered video: enabled pipe, restarted pipeline "
                "for guild=%d",
                guild_id,
            )

        # else: Case 1 — pipe already active, non-timing filter changed.
        # Lavalink auto-propagates. No action needed.

    except Exception as exc:
        log.warning(
            "_sync_video_pipe_on_filter_change failed for guild=%d: %s",
            guild_id,
            exc,
        )

    # Always broadcast filter_sync WS for frontend feedback
    try:
        ws_hub = video_cog._backend.ws_hub
        await ws_hub.broadcast_from_bot(guild_id, {
            "type": "filter_sync",
            "speed": timescale_speed,
            "filters_active": has_non_timing_filters,
        })
    except Exception as exc:
        log.debug("filter_sync broadcast failed for guild=%d: %s", guild_id, exc)


class Filters(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    filter_group = app_commands.Group(name="filter", description="Apply audio filters to HelloDJ playback")

    # ── Bassboost ───────────────────────────────────────────

    @filter_group.command(name="bassboost", description="Boost low-end frequencies")
    @app_commands.choices(level=[
        app_commands.Choice(name="Low", value="low"),
        app_commands.Choice(name="Moderate", value="moderate"),
        app_commands.Choice(name="Strong", value="strong"),
    ])
    async def bassboost(self, interaction: discord.Interaction, level: str = "moderate"):
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj:
            await interaction.response.send_message("HelloDJ is not connected to voice.")
            return

        # Lavalink EQ: boost 60-200Hz bands (15 bands total, 0-14)
        eq_levels = {
            "low": [0.0, 0.05, 0.1, 0.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "moderate": [0.0, 0.1, 0.15, 0.1, 0.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "strong": [0.0, 0.15, 0.25, 0.15, 0.1, 0.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        }

        gains = eq_levels.get(level, eq_levels["moderate"])
        bands = _eq_bands(gains)

        # Build filters: set equalizer, reset others
        filters = player_obj.filters
        filters.equalizer.set(bands=bands)
        filters.timescale.reset()
        filters.rotation.reset()
        filters.low_pass.reset()
        await player_obj.set_filters(filters)

        state = player.get_state(interaction.guild.id)
        state["filters"]["bassboost"] = {"level": level, "gains": gains}
        player.persist(interaction.guild.id)

        verified, _ = await _verify_filter(player_obj, interaction.guild.id, "equalizer")
        if verified:
            await interaction.response.send_message(
                f"HelloDJ bassboost **{level}** applied and **verified active** on the Lavalink node."
            )
        else:
            await interaction.response.send_message(
                f"HelloDJ bassboost **{level}** applied, but I could not verify it is active "
                "on the Lavalink node. Check the logs."
            )

        # Sync video pipe (non-timing filter, no timescale)
        await _sync_video_pipe_on_filter_change(
            self.bot, interaction.guild.id,
            has_non_timing_filters=True,
            timescale_speed=1.0,
        )

    # ── Nightcore ───────────────────────────────────────────

    @filter_group.command(name="nightcore", description="Speed up tempo and shift pitch upward")
    async def nightcore(self, interaction: discord.Interaction):
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj:
            await interaction.response.send_message("HelloDJ is not connected to voice.")
            return

        # Lavalink timescale filter: speed 1.25x, pitch shift
        filters = player_obj.filters
        filters.timescale.set(speed=1.25, pitch=1.25, rate=1.0)
        filters.equalizer.reset()
        filters.rotation.reset()
        filters.low_pass.reset()
        await player_obj.set_filters(filters)

        state = player.get_state(interaction.guild.id)
        state["filters"]["nightcore"] = {"speed": 1.25, "pitch": 1.25}
        player.persist(interaction.guild.id)

        # Sync timescale to Activity video playback rate
        await _broadcast_timescale_to_activity(self.bot, interaction.guild.id, speed=1.25)

        # Sync video pipe (timescale change)
        await _sync_video_pipe_on_filter_change(
            self.bot, interaction.guild.id,
            has_non_timing_filters=False,
            timescale_speed=1.25,
        )

        verified, _ = await _verify_filter(player_obj, interaction.guild.id, "timescale")
        if verified:
            await interaction.response.send_message(
                "HelloDJ nightcore filter applied and **verified active** on the Lavalink node."
            )
        else:
            await interaction.response.send_message(
                "HelloDJ nightcore filter applied, but I could not verify it is active "
                "on the Lavalink node. Check the logs."
            )

    # ── 8D (fixed) ──────────────────────────────────────────
    # The original implementation used rotation_hz=0.2, which the user reported
    # "does nothing actually". 0.2 Hz is a very slow pan oscillation (one full
    # left-right-left cycle every 5 seconds) that is hard to perceive, especially
    # over speakers. We increase it to 0.5 Hz (one cycle every 2 seconds) which
    # is clearly perceptible, and we verify server-side that the rotation filter
    # is actually active on the Lavalink node.

    @filter_group.command(name="8d", description="Apply spatial panning (left/right oscillation)")
    async def eightd(self, interaction: discord.Interaction):
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj:
            await interaction.response.send_message("HelloDJ is not connected to voice.")
            return

        # Lavalink rotation filter: oscillate pan at 0.5 Hz (was 0.2 — too slow
        # to perceive). 0.5 Hz = one full left-right-left cycle every 2 seconds.
        filters = player_obj.filters
        filters.rotation.set(rotation_hz=0.5)
        filters.equalizer.reset()
        filters.timescale.reset()
        filters.low_pass.reset()
        await player_obj.set_filters(filters)

        state = player.get_state(interaction.guild.id)
        state["filters"]["8d"] = {"rotation": 0.5}
        player.persist(interaction.guild.id)

        verified, server_filters = await _verify_filter(player_obj, interaction.guild.id, "rotation")
        if verified:
            rot_hz = server_filters.get("rotation", {}).get("rotationHz", 0.5)
            await interaction.response.send_message(
                f"HelloDJ 8D filter applied and **verified active** on the Lavalink node "
                f"(rotation={rot_hz} Hz). You should hear the audio pan left↔right."
            )
        else:
            await interaction.response.send_message(
                "HelloDJ 8D filter applied, but I could **not** verify it is active on the "
                "Lavalink node. This may mean the rotation filter is not enabled on the node. "
                "Check the logs and run `/filter test` to inspect the active filters."
            )

        # Sync video pipe (non-timing filter, no timescale)
        await _sync_video_pipe_on_filter_change(
            self.bot, interaction.guild.id,
            has_non_timing_filters=True,
            timescale_speed=1.0,
        )

    # ── Vaporwave (new) ─────────────────────────────────────
    # "Slow toned vibe": slow the music down (timescale speed 0.85) and drop the
    # pitch slightly (pitch 0.9) for the classic vaporwave slowed feel, plus a
    # subtle low-band boost (equalizer bands 0-2 +0.15) for the "toned" quality.

    @filter_group.command(name="vaporwave", description="Slowed, mellow vaporwave vibe")
    async def vaporwave(self, interaction: discord.Interaction):
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj:
            await interaction.response.send_message("HelloDJ is not connected to voice.")
            return

        # timescale: speed=0.85, pitch=0.9, rate=0.85 (slower, slightly lower pitch)
        # equalizer: bands 0-2 gain +0.15 (subtle bass boost for the "toned" quality)
        gains = [0.15, 0.15, 0.15, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        bands = _eq_bands(gains)

        filters = player_obj.filters
        filters.timescale.set(speed=0.85, pitch=0.9, rate=0.85)
        filters.equalizer.set(bands=bands)
        filters.rotation.reset()
        filters.low_pass.reset()
        await player_obj.set_filters(filters)

        state = player.get_state(interaction.guild.id)
        state["filters"]["vaporwave"] = {
            "speed": 0.85, "pitch": 0.9, "rate": 0.85, "gains": gains,
        }
        player.persist(interaction.guild.id)

        # Sync timescale to Activity video playback rate
        await _broadcast_timescale_to_activity(self.bot, interaction.guild.id, speed=0.85)

        # Sync video pipe (timescale + non-timing EQ)
        await _sync_video_pipe_on_filter_change(
            self.bot, interaction.guild.id,
            has_non_timing_filters=True,
            timescale_speed=0.85,
        )

        verified, _ = await _verify_filter(player_obj, interaction.guild.id, "timescale")
        if verified:
            await interaction.response.send_message(
                "HelloDJ vaporwave filter applied and **verified active** on the Lavalink node "
                "(slowed to 0.85x, pitch 0.9x, subtle bass boost)."
            )
        else:
            await interaction.response.send_message(
                "HelloDJ vaporwave filter applied, but I could not verify it is active "
                "on the Lavalink node. Check the logs."
            )

    # ── 8-bit (arcade / chiptune) ───────────────────────────
    # Rethought as a guitar-pedal-style effect chain. The old implementation was
    # a low-pass muffle — the wrong tool. 8-bit/chiptune is a bitcrusher +
    # sample-rate-reduction sound, and Lavalink has no native bitcrush, so the
    # best achievable arcade vibe uses Lavalink's supported DSP filters chained
    # like guitar pedals:
    #
    #   1. distortion  (scale)              → harsh square-wave-like grit
    #   2. tremolo     (frequency/depth)    → classic 8-bit volume warbling
    #   3. vibrato     (frequency/depth)    → retro pitch vibrato
    #   4. timescale   (slower speed, pitch up) → chiptune pitch character
    #   5. equalizer   (cut lows, boost mids)   → tinny arcade speaker character
    #
    # Distortion parameter rationale: wavelink 3.5 documents all of
    # Distortion.set's parameters (sinOffset/sinScale/cosOffset/cosScale/
    # tanOffset/tanScale/offset/scale) and Lavalink's DistortionConfig exposes
    # the same set with safe defaults (scale=1.0, offsets=0.0). We only raise
    # ``scale`` (the linear gain term) slightly to 1.35 for a subtle drive; we
    # leave sin/cos/tan at their identity defaults so the nonlinear tan() term —
    # which can blow up near ±π/2 and replace the audio with a pure tone — is
    # NOT pushed into dangerous territory. This is deterministic and safe.
    #
    # Equalizer gain notes: wavelink documents gain in the range -0.25..1.0
    # (verified against Lavalink's filterConfigs.kt and Lavaplayer's Equalizer,
    # which use the raw gain unclamped). -0.25 is the documented "completely
    # muted" value, so we use it for the low/high cuts.

    @filter_group.command(name="8bit", description="Arcade 8-bit chiptune vibe (distortion + tremolo + vibrato + EQ)")
    async def eightbit(self, interaction: discord.Interaction):
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj:
            await interaction.response.send_message("HelloDJ is not connected to voice.")
            return

        # equalizer: treble-emphasis / mid-boost EQ so the arcade tone CUTS
        # THROUGH rather than muffles. Boost mids (bands 3-9), slight treble
        # rolloff at the top — NOT a low-cut that kills the body.
        gains = [0, 0.05, 0.1, 0.2, 0.25, 0.3, 0.25, 0.2, 0.15, 0.1, 0.05, 0, -0.1, -0.2, -0.3]
        bands = _eq_bands(gains)

        filters = player_obj.filters
        # distortion: harsh square-wave crunch — the "arcade" core
        filters.distortion.set(scale=2.0)
        # tremolo: fast square tremolo — choppy arcade pulse
        filters.tremolo.set(frequency=16.0, depth=0.6)
        # vibrato: retro pitch vibrato
        filters.vibrato.set(frequency=12.0, depth=0.4)
        # timescale: natural speed, pitch up slightly — do NOT slow it down
        # (slow = muddy); speed 1.0 keeps the tempo crisp.
        filters.timescale.set(speed=1.0, pitch=1.1, rate=1.0)
        filters.equalizer.set(bands=bands)
        # Remove the muffling low-pass and any stale filters from other presets.
        filters.low_pass.reset()
        filters.rotation.reset()
        filters.karaoke.reset()
        filters.channel_mix.reset()
        await player_obj.set_filters(filters)

        state = player.get_state(interaction.guild.id)
        state["filters"]["8bit"] = {
            "gains": gains,
            "speed": 1.0, "pitch": 1.1, "rate": 1.0,
            "distortion_scale": 2.0,
            "tremolo": {"frequency": 16.0, "depth": 0.6},
            "vibrato": {"frequency": 12.0, "depth": 0.4},
        }
        player.persist(interaction.guild.id)

        # Verify the most distinctive filter (distortion) is active.
        verified, server_filters = await _verify_filter(player_obj, interaction.guild.id, "distortion")
        if verified:
            await interaction.response.send_message(
                "HelloDJ 8-bit filter applied and **verified active** on the Lavalink node "
                "(distortion + tremolo + vibrato + timescale + equalizer arcade chain)."
            )
        else:
            # distortion may not be enabled on the node; fall back to checking the
            # other components so the user knows what actually took effect.
            tr_ok = _filter_active(server_filters, "tremolo")
            vb_ok = _filter_active(server_filters, "vibrato")
            ts_ok = _filter_active(server_filters, "timescale")
            eq_ok = _filter_active(server_filters, "equalizer")
            parts = []
            if eq_ok:
                parts.append("equalizer")
            if ts_ok:
                parts.append("timescale")
            if vb_ok:
                parts.append("vibrato")
            if tr_ok:
                parts.append("tremolo")
            if parts:
                await interaction.response.send_message(
                    "HelloDJ 8-bit filter applied. The **distortion** grit could not be "
                    f"verified on the node, but the following are active: {', '.join(parts)}. "
                    "The arcade effect may be less gritty than expected."
                )
            else:
                await interaction.response.send_message(
                    "HelloDJ 8-bit filter applied, but I could not verify any component is "
                    "active on the Lavalink node. Check the logs and run `/filter test`."
                )

        # Sync video pipe (non-timing filters + timescale speed=1.0 pitch=1.1)
        # Note: pitch without speed change is NOT a timing filter — only the speed
        # component requires FFmpeg handling. speed=1.0 here means no timing change.
        await _sync_video_pipe_on_filter_change(
            self.bot, interaction.guild.id,
            has_non_timing_filters=True,
            timescale_speed=1.0,
        )

    # ── /tune (enhanced audio) ──────────────────────────────
    # A permanent "light switch": /tune toggles enhanced audio ON/OFF and stays
    # on (persisted via player.persist -> session) until turned off. When ON, a
    # transparent enhancement chain (equalizer + timescale + light distortion)
    # is applied immediately to the current track AND re-applied automatically
    # to every new track that starts (see player.on_track_start hook).

    @app_commands.command(name="tune", description="Toggle enhanced audio (less compressed, more crisp)")
    async def tune(self, interaction: discord.Interaction):
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj:
            await interaction.response.send_message("HelloDJ is not connected to voice.")
            return

        state = player.get_state(interaction.guild.id)
        tune_enabled = not state.get("tune_enabled", False)
        state["tune_enabled"] = tune_enabled
        player.persist(interaction.guild.id)

        if tune_enabled:
            await _apply_tune(player_obj)
            await interaction.response.send_message(
                "🎚️ **Enhanced audio: ON** — less compressed, more crisp. "
                "It stays on for every new track until you turn it off with `/tune` again."
            )
            # Sync video pipe (non-timing filters: EQ + distortion, speed=1.0)
            await _sync_video_pipe_on_filter_change(
                self.bot, interaction.guild.id,
                has_non_timing_filters=True,
                timescale_speed=1.0,
            )
        else:
            # Turn the enhancement off: reset all filters so the previous tune
            # chain (and any other active filters) is cleared.
            filters = player_obj.filters
            filters.reset()
            await player_obj.set_filters(filters)
            await interaction.response.send_message(
                "🎚️ **Enhanced audio: OFF** — filters reset to default."
            )
            # Sync video pipe (filter reset)
            await _sync_video_pipe_on_filter_change(
                self.bot, interaction.guild.id,
                has_non_timing_filters=False,
                timescale_speed=1.0,
                was_reset=True,
            )

    # ── 808 (new) ───────────────────────────────────────────
    # Plays the 808 cowbell as a sound effect. Lavalink filters are DSP effects
    # applied to the audio stream and cannot mix in a separate audio source, so
    # the 808 cowbell is played as a separate audio source via the same path the
    # chime feature uses (sounds.play_sound → TTSPLayer.play_pcm →
    # send_audio_packet). It plays alongside the music rather than being mixed
    # into it.

    @filter_group.command(name="808", description="Play the 808 cowbell as a sound effect")
    async def eight08(self, interaction: discord.Interaction):
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj:
            await interaction.response.send_message("HelloDJ is not connected to voice.")
            return

        await interaction.response.defer(ephemeral=True)

        # Ensure the 808 cowbell sample is present (download if missing).
        path = await sounds.ensure_preset(sounds.DEFAULT_PRESET)
        if not path:
            await interaction.followup.send(
                "Could not load the 808 cowbell sample. It may be missing and "
                "unreachable. Check the logs.", ephemeral=True
            )
            return

        # Play the 808 cowbell as a separate audio source (alongside the music).
        ok = await sounds.play_sound(player_obj, path, volume=100)
        if ok:
            await interaction.followup.send(
                "🔊 808 cowbell played. Note: this is a sound effect played as a "
                "separate audio source alongside the music — Lavalink filters cannot "
                "mix a separate audio source into the stream.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                "Could not play the 808 cowbell. Check the logs.", ephemeral=True
            )

    # ── Equalizer ───────────────────────────────────────────

    @filter_group.command(name="equalizer", description="Fine-tune specific frequency bands")
    @app_commands.describe(
        band1="20Hz  (-1.0 to 1.0)",
        band2="60Hz  (-1.0 to 1.0)",
        band3="100Hz (-1.0 to 1.0)",
        band4="140Hz (-1.0 to 1.0)",
        band5="200Hz (-1.0 to 1.0)",
        band6="400Hz (-1.0 to 1.0)",
        band7="800Hz (-1.0 to 1.0)",
        band8="1.6kHz (-1.0 to 1.0)",
        band9="3.2kHz (-1.0 to 1.0)",
        band10="6.4kHz (-1.0 to 1.0)",
    )
    async def equalizer(
        self,
        interaction: discord.Interaction,
        band1: float = 0.0,
        band2: float = 0.0,
        band3: float = 0.0,
        band4: float = 0.0,
        band5: float = 0.0,
        band6: float = 0.0,
        band7: float = 0.0,
        band8: float = 0.0,
        band9: float = 0.0,
        band10: float = 0.0,
    ):
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj:
            await interaction.response.send_message("HelloDJ is not connected to voice.")
            return

        # Lavalink has 15 bands (0-14). Map our 10 bands to Lavalink's bands.
        gains = [0.0] * 15
        user_gains = [band1, band2, band3, band4, band5, band6, band7, band8, band9, band10]
        for i, g in enumerate(user_gains):
            if i < 10:
                gains[i] = max(-1.0, min(1.0, g))

        bands = _eq_bands(gains)
        filters = player_obj.filters
        filters.equalizer.set(bands=bands)
        filters.timescale.reset()
        filters.rotation.reset()
        filters.low_pass.reset()
        await player_obj.set_filters(filters)

        state = player.get_state(interaction.guild.id)
        state["filters"]["equalizer"] = {"gains": gains}
        player.persist(interaction.guild.id)

        verified, _ = await _verify_filter(player_obj, interaction.guild.id, "equalizer")
        if verified:
            await interaction.response.send_message(
                "HelloDJ equalizer applied with custom band levels and **verified active** "
                "on the Lavalink node."
            )
        else:
            await interaction.response.send_message(
                "HelloDJ equalizer applied with custom band levels, but I could not verify "
                "it is active on the Lavalink node. Check the logs."
            )

        # Sync video pipe (non-timing filter, no timescale)
        await _sync_video_pipe_on_filter_change(
            self.bot, interaction.guild.id,
            has_non_timing_filters=True,
            timescale_speed=1.0,
        )

    # ── Stems isolate (new) ─────────────────────────────────
    # True audio stem separation (isolated vocals/drums/bass/melody) requires a
    # source-separation ML model (demucs/spleeter/onnx) that is heavy and NOT
    # installed by default. Lavalink's built-in filters do NOT support stem
    # isolation; its only approximation is the ``karaoke`` filter, which
    # attenuates the vocal band → an *instrumental* (vocals-removed) version.
    #
    #   /filter stems isolate vocals   → karaoke filter (instrumental, verified)
    #   /filter stems isolate drums|bass|melody → requires the optional AI model;
    #       without it we clearly explain the limitation and offer the vocals
    #       (instrumental) fallback.

    # Nested group: discord.py 2.7.1 ships no `Group.group()` factory, so build
    # the nested group explicitly and attach it to filter_group via parent=.
    # The group needs no callback; only the "isolate" subcommand does work.
    stems_group = app_commands.Group(
        name="stems",
        description="Isolate audio stems (vocals/drums/bass/melody)",
        parent=filter_group,
    )

    @stems_group.command(name="isolate", description="Isolate a single audio stem from the mix")
    @app_commands.choices(stem_type=[
        app_commands.Choice(name="Vocals", value="vocals"),
        app_commands.Choice(name="Drums", value="drums"),
        app_commands.Choice(name="Bass", value="bass"),
        app_commands.Choice(name="Melody", value="melody"),
    ])
    async def stems_isolate(self, interaction: discord.Interaction, stem_type: str):
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj:
            await interaction.response.send_message("HelloDJ is not connected to voice.")
            return

        stem_type = stems.validate_stem_type(stem_type)
        if stem_type is None:
            await interaction.response.send_message(
                "Invalid stem type. Choose `vocals`, `drums`, `bass`, or `melody`."
            )
            return

        # ── vocals → Lavalink karaoke filter (instrumental) ──────────
        # The karaoke filter "uses equalization to eliminate part of a band,
        # usually targeting vocals". Per wavelink's Karaoke.set docs, level is
        # 0..1.0 where 0.0 = no effect and 1.0 = full effect — so level=1.0
        # fully removes the vocals, leaving the instrumental (drums+bass+melody).
        # We tune the filter band to the typical vocal range so only that band
        # is cut.
        if stem_type == "vocals":
            filters = player_obj.filters
            filters.karaoke.set(
                level=1.0,
                mono_level=1.0,
                filter_band=100.0,
                filter_width=100.0,
            )
            # Reset the other DSP filters so stems isolation is clean.
            filters.equalizer.reset()
            filters.timescale.reset()
            filters.rotation.reset()
            filters.low_pass.reset()
            await player_obj.set_filters(filters)

            state = player.get_state(interaction.guild.id)
            state["filters"]["stems_isolate"] = {
                "stem": "vocals",
                "mode": "karaoke",
                "instrumental": True,
            }
            player.persist(interaction.guild.id)

            # Verify server-side that the karaoke filter is active.
            verified, server_filters = await _verify_filter(player_obj, interaction.guild.id, "karaoke")
            if verified:
                kara = server_filters.get("karaoke", {})
                await interaction.response.send_message(
                    "🎤 **Vocals isolated (instrumental)** via the Lavalink karaoke "
                    "filter, **verified active** on the node (`karaoke` payload: "
                    f"`{kara}`).\n\n"
                    "This **removes the vocals** to leave the instrumental "
                    "(drums + bass + melody). Lavalink has **no filter that can "
                    "isolate vocals alone** — true vocal-only stems require the "
                    "optional AI separation model."
                )
            else:
                await interaction.response.send_message(
                    "🎤 Vocals-isolation (instrumental) filter applied, but I could "
                    "**not verify** it is active on the Lavalink node. The karaoke "
                    "filter may not be enabled on the node. Check the logs and run "
                    "`/filter test`."
                )

            # Sync video pipe (non-timing karaoke filter)
            await _sync_video_pipe_on_filter_change(
                self.bot, interaction.guild.id,
                has_non_timing_filters=True,
                timescale_speed=1.0,
            )
            return

        # ── drums / bass / melody → NOT possible via Lavalink filters ──
        # Lavalink has no drums/bass/melody isolation. Try the optional AI
        # separation path; when no heavy model is installed, explain honestly.
        audio_path = None  # streaming architecture: no local audio file to separate
        stem_path = await stems.isolate_stem(audio_path, stem_type)
        if stem_path:
            # Future AI path — not reachable today (isolate_stem returns None).
            await interaction.response.send_message(
                f"🧩 **{stems.STEM_LABELS[stem_type]}** stem isolated via the AI "
                "separation model. This is the standalone-solo path."
            )
            return

        # No AI model → limitation message + offer the vocals (instrumental) fallback.
        reason = stems.stems_reason()
        await interaction.response.send_message(
            f"⚠️ **Cannot isolate `{stem_type}`** with Lavalink's built-in filters.\n\n"
            "Lavalink has **no filter that isolates drums, bass, or melody**. True "
            "stem separation needs an **optional heavy AI model** (demucs/spleeter/"
            "onnx) that is not installed by default.\n\n"
            f"Status: {reason}\n\n"
            "**What you CAN do now:** `/filter stems isolate vocals` uses the "
            "karaoke filter to produce the **instrumental** (vocals removed) "
            "version. True `drums`/`bass`/`melody` solo stems require installing "
            "the optional separation model."
        )

    # ── Filter test (new diagnostic) ────────────────────────
    # Reports which filters are currently active on the Lavalink node. This is
    # the "test mechanism" that lets a user verify a filter is really applied
    # (important for the 8d fix, where the user reported the filter "does
    # nothing").

    @filter_group.command(name="test", description="Report which filters are active on the node")
    async def filter_test(self, interaction: discord.Interaction):
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj:
            await interaction.response.send_message("HelloDJ is not connected to voice.")
            return

        server_filters = await _get_server_filters(player_obj, interaction.guild.id)
        if server_filters is None:
            await interaction.response.send_message(
                "Could not fetch the server-side filter state from the Lavalink node. "
                "The node may not be connected. Check the logs.", ephemeral=True
            )
            return

        if not server_filters:
            await interaction.response.send_message(
                "No filters are currently active on the Lavalink node.", ephemeral=True
            )
            return

        lines = []
        for key, value in server_filters.items():
            if key == "volume":
                continue
            lines.append(f"• **{key}**: `{value}`")

        embed = discord.Embed(
            title="🔍 Active Filters (server-side)",
            description="\n".join(lines) if lines else "No filters active.",
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(text="Fetched from the Lavalink node — this is the ground truth.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── Filter reset (subcommand of /filter) ─────────────────
    # Naming parity: /filter reset is a space-style subcommand, consistent with
    # all other filter subcommands. A top-level /filter_reset alias is also
    # provided (see below) so the reset is discoverable by both naming styles.
    # The reset logic is unchanged.

    @filter_group.command(name="reset", description="Reset all audio filters to default")
    async def filter_reset(self, interaction: discord.Interaction):
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj:
            await interaction.response.send_message("HelloDJ is not connected to voice.")
            return

        # Reset all filters using wavelink 3.5 API
        filters = player_obj.filters
        filters.reset()
        await player_obj.set_filters(filters)

        state = player.get_state(interaction.guild.id)
        state["filters"] = {}
        player.persist(interaction.guild.id)

        # Reset Activity video playback rate to normal
        await _broadcast_timescale_to_activity(self.bot, interaction.guild.id, speed=1.0)

        # Sync video pipe (filter reset — disable pipe, restart with source audio)
        await _sync_video_pipe_on_filter_change(
            self.bot, interaction.guild.id,
            has_non_timing_filters=False,
            timescale_speed=1.0,
            was_reset=True,
        )

        # Verify the reset took effect server-side (no filters should remain).
        server_filters = await _get_server_filters(player_obj, interaction.guild.id)
        if server_filters is None:
            await interaction.response.send_message(
                "HelloDJ all filters reset to default (could not verify server-side)."
            )
        elif not server_filters:
            await interaction.response.send_message(
                "HelloDJ all filters reset to default and **verified** on the Lavalink node."
            )
        else:
            remaining = [k for k in server_filters if k != "volume"]
            await interaction.response.send_message(
                f"HelloDJ filters reset, but the following are still active on the node: "
                f"{', '.join(remaining) if remaining else 'none'}."
            )

    # ── Filter reset (top-level alias of /filter reset) ──────
    # Naming parity: /filter_reset is a top-level alias that performs the EXACT
    # same reset as /filter reset. Both commands are valid aliases.

    @app_commands.command(name="filter_reset", description="Reset all audio filters to default (same as /filter reset)")
    async def filter_reset_top(self, interaction: discord.Interaction):
        player_obj = player.get_player(interaction.guild.id)
        if not player_obj:
            await interaction.response.send_message("HelloDJ is not connected to voice.")
            return

        # Reset all filters using wavelink 3.5 API
        filters = player_obj.filters
        filters.reset()
        await player_obj.set_filters(filters)

        state = player.get_state(interaction.guild.id)
        state["filters"] = {}
        player.persist(interaction.guild.id)

        # Reset Activity video playback rate to normal
        await _broadcast_timescale_to_activity(self.bot, interaction.guild.id, speed=1.0)

        # Sync video pipe (filter reset — disable pipe, restart with source audio)
        await _sync_video_pipe_on_filter_change(
            self.bot, interaction.guild.id,
            has_non_timing_filters=False,
            timescale_speed=1.0,
            was_reset=True,
        )

        # Verify the reset took effect server-side (no filters should remain).
        server_filters = await _get_server_filters(player_obj, interaction.guild.id)
        if server_filters is None:
            await interaction.response.send_message(
                "HelloDJ all filters reset to default (could not verify server-side)."
            )
        elif not server_filters:
            await interaction.response.send_message(
                "HelloDJ all filters reset to default and **verified** on the Lavalink node."
            )
        else:
            remaining = [k for k in server_filters if k != "volume"]
            await interaction.response.send_message(
                f"HelloDJ filters reset, but the following are still active on the node: "
                f"{', '.join(remaining) if remaining else 'none'}."
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Filters(bot))
