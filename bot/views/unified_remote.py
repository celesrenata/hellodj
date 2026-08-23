"""Unified remote control view — works for both audio (wavelink) and video (Activity streamer).

Replaces the separate NowPlayingView (player.py) and VideoControlView (cogs/video.py)
with a single persistent view that detects the active playback type at interaction time.

Registered once in setup_hook with `bot.add_view(UnifiedControlView())`.
All custom_ids are fixed strings so the view survives bot restarts.
"""

from __future__ import annotations

import logging
import time as _time
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from discord.ext import commands

log = logging.getLogger(__name__)


def _get_bot(interaction: discord.Interaction) -> "commands.Bot":
    return interaction.client  # type: ignore[return-value]


def _get_video_cog(interaction: discord.Interaction):
    """Get the Video cog, or None if not loaded."""
    bot = _get_bot(interaction)
    return bot.get_cog("Video")


def _get_guild_id(interaction: discord.Interaction) -> int:
    assert interaction.guild_id is not None
    return interaction.guild_id


def _get_user_voice_channel_id(interaction: discord.Interaction) -> int | None:
    """Get the voice channel ID the interacting user is in, if any."""
    user = interaction.user
    if hasattr(user, "voice") and user.voice and user.voice.channel:
        return user.voice.channel.id
    return None


def _is_video_active_for(guild_id: int, interaction: discord.Interaction) -> bool:
    """Check if a video session is active for the guild (any channel)."""
    import player
    return player._is_video_active(guild_id)


def _get_active_streamer(guild_id: int, interaction: discord.Interaction):
    """Get the active video streamer for the user's voice channel, or None."""
    video_cog = _get_video_cog(interaction)
    if video_cog is None:
        return None
    channel_id = _get_user_voice_channel_id(interaction)
    if channel_id is None:
        return None
    streamer = video_cog._registry.get(guild_id, channel_id)
    if streamer is not None and streamer.is_active:
        return streamer
    return None


class UnifiedControlView(discord.ui.View):
    """Universal playback remote — works for audio and video.

    Row 0: ⏮ Previous • ⏯ Play/Pause • ⏭ Skip • ➕ Playlist • 🚫 Block
    Row 1: 🎛️ Filters dropdown (audio-only; responds gracefully during video)

    Persistent: timeout=None, fixed custom_ids, registered in setup_hook.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

        # Row 1: filter dropdown
        filter_select = discord.ui.Select(
            placeholder="🎛️ Filters & EQ…",
            options=[
                discord.SelectOption(label="Equalizer", value="equalizer", emoji="🎛️",
                                     description="Fine-tune frequency bands"),
                discord.SelectOption(label="Bass Boost", value="bassboost", emoji="🔊",
                                     description="Boost low-end frequencies"),
                discord.SelectOption(label="Nightcore", value="nightcore", emoji="⚡",
                                     description="Speed up + pitch shift"),
                discord.SelectOption(label="8D Audio", value="8d", emoji="🌀",
                                     description="Spatial panning effect"),
                discord.SelectOption(label="Vaporwave", value="vaporwave", emoji="🌊",
                                     description="Slowed, mellow vibe"),
                discord.SelectOption(label="8-Bit", value="8bit", emoji="🕹️",
                                     description="Arcade chiptune effect"),
                discord.SelectOption(label="808 Cowbell", value="808", emoji="🔔",
                                     description="Play the 808 cowbell"),
                discord.SelectOption(label="Tune (Enhanced)", value="tune", emoji="✨",
                                     description="Studio master polish"),
                discord.SelectOption(label="Reset Filters", value="reset", emoji="🔄",
                                     description="Remove all effects"),
            ],
            row=1,
            custom_id="unified_filter_select",
        )
        filter_select.callback = self._on_filter_select
        self.add_item(filter_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Any guild member can use it
        return True

    # ── Row 0 buttons ────────────────────────────────────────

    @discord.ui.button(label="⏮", style=discord.ButtonStyle.secondary,
                       custom_id="unified_prev", row=0)
    async def prev_track(self, interaction: discord.Interaction,
                         _button: discord.ui.Button) -> None:
        guild_id = _get_guild_id(interaction)

        if _is_video_active_for(guild_id, interaction):
            await self._video_previous(interaction, guild_id)
        else:
            await self._audio_previous(interaction, guild_id)

    @discord.ui.button(label="⏯", style=discord.ButtonStyle.primary,
                       custom_id="unified_toggle", row=0)
    async def pause_resume(self, interaction: discord.Interaction,
                           _button: discord.ui.Button) -> None:
        guild_id = _get_guild_id(interaction)

        if _is_video_active_for(guild_id, interaction):
            await self._video_pause_resume(interaction, guild_id)
        else:
            await self._audio_pause_resume(interaction, guild_id)

    @discord.ui.button(label="⏭", style=discord.ButtonStyle.secondary,
                       custom_id="unified_next", row=0)
    async def next_track(self, interaction: discord.Interaction,
                         _button: discord.ui.Button) -> None:
        guild_id = _get_guild_id(interaction)

        if _is_video_active_for(guild_id, interaction):
            await self._video_skip(interaction, guild_id)
        else:
            await self._audio_skip(interaction, guild_id)

    @discord.ui.button(label="➕", style=discord.ButtonStyle.success,
                       custom_id="unified_playlist", row=0)
    async def add_to_playlist(self, interaction: discord.Interaction,
                              _button: discord.ui.Button) -> None:
        guild_id = _get_guild_id(interaction)
        await self._show_playlist_picker(interaction, guild_id)

    @discord.ui.button(label="🚫", style=discord.ButtonStyle.secondary,
                       custom_id="unified_block", row=0)
    async def block_track(self, interaction: discord.Interaction,
                          _button: discord.ui.Button) -> None:
        guild_id = _get_guild_id(interaction)

        if _is_video_active_for(guild_id, interaction):
            await self._video_block(interaction, guild_id)
        else:
            await self._audio_block(interaction, guild_id)

    # ── Audio handlers ───────────────────────────────────────

    async def _audio_previous(self, interaction: discord.Interaction, guild_id: int) -> None:
        """Go to previous audio track from history, or seek to start if no history."""
        import player
        state = player.get_state(guild_id)
        history = state.get("history", [])
        if history:
            # Jump to most recent history track
            ok = await player.jump_to(guild_id, history_index=0)
            if ok:
                title = state.get("current", {}).get("title", "Unknown")
                await interaction.response.send_message(
                    f"⏮ Playing **{title}**", ephemeral=True
                )
                return
        # No history — seek to start of current track
        p = player.get_player(guild_id)
        if p and p.playing:
            try:
                await p.seek(0)
            except Exception:
                pass
        await interaction.response.defer()

    async def _audio_pause_resume(self, interaction: discord.Interaction, guild_id: int) -> None:
        """Toggle audio pause/resume."""
        import player
        p = player.get_player(guild_id)
        if not p:
            await interaction.response.defer()
            return
        if p.paused:
            await p.pause(False)
        elif p.playing:
            await p.pause(True)
        await interaction.response.defer()

    async def _audio_skip(self, interaction: discord.Interaction, guild_id: int) -> None:
        """Skip current audio track using the queue lock to prevent race conditions."""
        import player
        lock = player._get_queue_lock(guild_id)
        async with lock:
            p = player.get_player(guild_id)
            if p:
                await p.stop()
            await player._play_next_from_queue(guild_id)
        await interaction.response.defer()

    async def _audio_block(self, interaction: discord.Interaction, guild_id: int) -> None:
        """Block current audio track. Admin only."""
        import player
        import oauth_store
        import blacklist as _blacklist

        is_admin = False
        if interaction.guild:
            is_admin = interaction.user.guild_permissions.administrator
        if not is_admin:
            is_admin = oauth_store.is_bound_admin(interaction.user.id)
        if not is_admin:
            await interaction.response.send_message(
                "🚫 Only administrators can block tracks.", ephemeral=True
            )
            return

        state = player.get_state(guild_id)
        current = state.get("current")
        title = (current or {}).get("title", "Unknown")
        if current:
            _blacklist.add_blacklist_entry(guild_id, current)
        p = player.get_player(guild_id)
        if p:
            await p.stop()
        await interaction.response.send_message(
            f"🚫 Blocked **{title}**.", ephemeral=True
        )

    # ── Video handlers ───────────────────────────────────────

    async def _video_previous(self, interaction: discord.Interaction, guild_id: int) -> None:
        """Go to previous video in the video streamer's history."""
        streamer = _get_active_streamer(guild_id, interaction)
        if streamer is None:
            # No active streamer — fall back to unified queue history
            # This handles the case where the session just ended but
            # _is_video_active() hasn't caught up yet, or queue is empty
            await self._audio_previous(interaction, guild_id)
            return

        await interaction.response.defer()
        try:
            result = await streamer.previous()
        except Exception as exc:
            log.error("Unified remote: video previous failed guild=%d: %s", guild_id, exc)
            await interaction.followup.send("❌ Could not go to previous video.", ephemeral=True)
            return

        if not result:
            await interaction.followup.send("⏮ No previous video available.", ephemeral=True)
            return

        # Broadcast new state to Activity clients
        video_cog = _get_video_cog(interaction)
        if video_cog and streamer.is_active and streamer.source:
            from video.ws_hub import PlaybackState
            new_state = PlaybackState(playing=True, anchor_position=0.0, anchor_time=_time.time())
            video_cog._backend.ws_hub.set_state(guild_id, new_state)
            await video_cog._backend.ws_hub.broadcast_from_bot(guild_id, {
                "type": "state",
                "playing": True,
                "position": 0.0,
                "timestamp": _time.time(),
                "subtitle_lang": None,
                "audio_lang": None,
            })
            # Update the now-playing embed
            from cogs.video import _build_now_playing_embed
            channel_id = _get_user_voice_channel_id(interaction)
            embed = _build_now_playing_embed(streamer.source, len(streamer.queue), elapsed_seconds=0.0)
            msg = await interaction.followup.send(
                "⏮ Playing previous video!", embed=embed,
                view=UnifiedControlView(), wait=True,
            )
            if channel_id:
                key = (guild_id, channel_id)
                video_cog._now_playing_messages[key] = msg
                video_cog._start_seek_bar_update(key)
        else:
            await interaction.followup.send("⏮ Went back to previous video.")

    async def _video_pause_resume(self, interaction: discord.Interaction, guild_id: int) -> None:
        """Toggle video play/pause via WebSocket broadcast."""
        video_cog = _get_video_cog(interaction)
        if video_cog is None:
            await interaction.response.defer()
            return

        ws_hub = video_cog._backend.ws_hub
        state = ws_hub.get_state(guild_id)
        if state is None:
            await interaction.response.send_message("No sync state available.", ephemeral=True)
            return

        if state.playing:
            state.set_playing(False)
            msg = {
                "type": "pause",
                "position": state.anchor_position,
                "anchor_position": state.anchor_position,
                "anchor_time": state.anchor_time_wall,
                "anchor_time_mono": state.anchor_time,
                "timestamp": _time.time(),
            }
        else:
            state.set_playing(True)
            msg = {
                "type": "play",
                "position": state.anchor_position,
                "anchor_position": state.anchor_position,
                "anchor_time": state.anchor_time_wall,
                "anchor_time_mono": state.anchor_time,
                "timestamp": _time.time(),
            }

        ws_hub.set_state(guild_id, state)
        await ws_hub.broadcast_from_bot(guild_id, msg)

        emoji = "⏸" if not state.playing else "▶"
        label = "Paused" if not state.playing else "Resumed"
        await interaction.response.send_message(f"{emoji} {label}", ephemeral=True)

    async def _video_skip(self, interaction: discord.Interaction, guild_id: int) -> None:
        """Skip the current video — advance unified queue."""
        video_cog = _get_video_cog(interaction)
        channel_id = _get_user_voice_channel_id(interaction)

        if video_cog is None or channel_id is None:
            await interaction.response.send_message(
                "No video is currently streaming.", ephemeral=True
            )
            return

        streamer = video_cog._registry.get(guild_id, channel_id)
        if streamer is None or not streamer.is_active:
            await interaction.response.send_message(
                "No video is currently streaming.", ephemeral=True
            )
            return

        await interaction.response.defer()
        key = (guild_id, channel_id)
        had_internal_queue = len(streamer.queue) > 0

        try:
            await streamer.skip()
        except Exception as exc:
            log.error("Unified remote: video skip failed guild=%d: %s", guild_id, exc)
            await interaction.followup.send("❌ Skip failed.", ephemeral=True)
            return

        if had_internal_queue and streamer.is_active and streamer.source:
            # Skipped within the video streamer's own queue
            from video.ws_hub import PlaybackState
            new_state = PlaybackState(playing=True, anchor_position=0.0, anchor_time=_time.time())
            video_cog._backend.ws_hub.set_state(guild_id, new_state)
            await video_cog._backend.ws_hub.broadcast_from_bot(guild_id, {
                "type": "state",
                "playing": True,
                "position": 0.0,
                "timestamp": _time.time(),
                "subtitle_lang": None,
                "audio_lang": None,
            })
            from cogs.video import _build_now_playing_embed
            embed = _build_now_playing_embed(streamer.source, len(streamer.queue), elapsed_seconds=0.0)
            msg = await interaction.followup.send(
                "⏭ Skipped!", embed=embed, view=UnifiedControlView(), wait=True,
            )
            video_cog._now_playing_messages[key] = msg
            video_cog._start_seek_bar_update(key)
        else:
            # Video streamer's queue empty — check unified queue
            import player
            state = player.get_state(guild_id)
            if state["queue"]:
                # More items in unified queue — clean up video session, advance.
                # Acquire queue lock BEFORE stopping streamer to prevent
                # on_track_end from racing (it checks lock.locked()).
                lock = player._get_queue_lock(guild_id)
                async with lock:
                    video_cog._stop_seek_bar_update(key)
                    await video_cog._backend.ws_hub.broadcast_from_bot(guild_id, {
                        "type": "session_end",
                    })
                    await streamer.stop()
                    video_cog._backend.ws_hub.unregister_streamer(guild_id)
                    video_cog._registry.unregister(guild_id, channel_id)
                    state["current"] = None
                    await interaction.followup.send("⏭ Skipping to next in queue...")
                    await player._play_next_from_queue(guild_id)
            else:
                # Truly empty — stop everything
                video_cog._stop_seek_bar_update(key)
                await video_cog._backend.ws_hub.disconnect_all(guild_id)
                video_cog._backend.ws_hub.unregister_streamer(guild_id)
                if video_cog._launcher is not None:
                    try:
                        await video_cog._launcher.close(streamer.channel_id)
                    except Exception:
                        pass
                video_cog._registry.unregister(guild_id, channel_id)
                await interaction.followup.send("⏭ Queue empty — stopped.")

    async def _video_block(self, interaction: discord.Interaction, guild_id: int) -> None:
        """Block the current video. Admin only."""
        import oauth_store
        import blacklist as _blacklist

        is_admin = False
        if interaction.guild:
            is_admin = interaction.user.guild_permissions.administrator
        if not is_admin:
            is_admin = oauth_store.is_bound_admin(interaction.user.id)
        if not is_admin:
            await interaction.response.send_message(
                "🚫 Only administrators can block tracks.", ephemeral=True
            )
            return

        streamer = _get_active_streamer(guild_id, interaction)
        if streamer is None or streamer.source is None:
            await interaction.response.send_message(
                "No video is currently streaming.", ephemeral=True
            )
            return

        title = streamer.source.title
        source = streamer.source
        block_url = (source.metadata.get("tidal_url")
                     or source.metadata.get("url")
                     or source.file_path)

        # Build a track_info dict compatible with blacklist.add_blacklist_entry
        track_info = {"title": title, "url": block_url}
        _blacklist.add_blacklist_entry(guild_id, track_info)

        await interaction.response.send_message(f"🚫 Blocked **{title}**.", ephemeral=True)
        # Skip after blocking
        try:
            await streamer.skip()
        except Exception:
            pass

    # ── Playlist picker (works for both) ─────────────────────

    async def _show_playlist_picker(self, interaction: discord.Interaction, guild_id: int) -> None:
        """➕ Add current track to a playlist."""
        import player
        import storage

        state = player.get_state(guild_id)
        current = state.get("current")
        if not current:
            await interaction.response.send_message(
                "Nothing is playing right now.", ephemeral=True
            )
            return

        playlist_names = storage.names(guild_id)
        if not playlist_names:
            await interaction.response.send_message(
                "No playlists exist yet. Use `/playlist create <name>` to create one first.",
                ephemeral=True,
            )
            return

        view = _PlaylistSelectView(guild_id, current)
        await interaction.response.send_message(
            "Choose a playlist to add the current song to:",
            view=view,
            ephemeral=True,
        )

    # ── Filter dropdown ──────────────────────────────────────

    async def _on_filter_select(self, interaction: discord.Interaction) -> None:
        """Handle filter dropdown — only works during audio playback."""
        import player

        guild_id = _get_guild_id(interaction)

        if _is_video_active_for(guild_id, interaction):
            await interaction.response.send_message(
                "🎛️ Filters only apply to audio playback, not video.", ephemeral=True
            )
            return

        value = interaction.data["values"][0]
        player_obj = player.get_player(guild_id)
        if not player_obj:
            await interaction.response.send_message(
                "HelloDJ is not connected to voice.", ephemeral=True
            )
            return

        state = player.get_state(guild_id)

        if value == "reset":
            filters = player_obj.filters
            filters.equalizer.reset()
            filters.timescale.reset()
            filters.rotation.reset()
            filters.low_pass.reset()
            filters.distortion.reset()
            filters.vibrato.reset()
            filters.tremolo.reset()
            filters.karaoke.reset()
            filters.channel_mix.reset()
            await player_obj.set_filters(filters)
            state["filters"] = {}
            state["tune_enabled"] = False
            player.persist(guild_id)
            await interaction.response.send_message("🔄 All filters reset.", ephemeral=True)

        elif value == "bassboost":
            gains = [0.0, 0.1, 0.15, 0.1, 0.05, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
            bands = [{"band": i, "gain": g} for i, g in enumerate(gains)]
            filters = player_obj.filters
            filters.equalizer.set(bands=bands)
            await player_obj.set_filters(filters)
            state["filters"]["bassboost"] = {"level": "moderate", "gains": gains}
            player.persist(guild_id)
            await interaction.response.send_message("🔊 Bass Boost applied.", ephemeral=True)

        elif value == "nightcore":
            filters = player_obj.filters
            filters.timescale.set(speed=1.25, pitch=1.25, rate=1.0)
            await player_obj.set_filters(filters)
            state["filters"]["nightcore"] = {"speed": 1.25, "pitch": 1.25}
            player.persist(guild_id)
            await interaction.response.send_message("⚡ Nightcore applied.", ephemeral=True)

        elif value == "8d":
            filters = player_obj.filters
            filters.rotation.set(rotation_hz=0.5)
            await player_obj.set_filters(filters)
            state["filters"]["8d"] = {"rotation": 0.5}
            player.persist(guild_id)
            await interaction.response.send_message("🌀 8D Audio applied.", ephemeral=True)

        elif value == "vaporwave":
            gains = [0.15, 0.15, 0.15, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
            bands = [{"band": i, "gain": g} for i, g in enumerate(gains)]
            filters = player_obj.filters
            filters.timescale.set(speed=0.85, pitch=0.9, rate=0.85)
            filters.equalizer.set(bands=bands)
            await player_obj.set_filters(filters)
            state["filters"]["vaporwave"] = {"speed": 0.85, "pitch": 0.9}
            player.persist(guild_id)
            await interaction.response.send_message("🌊 Vaporwave applied.", ephemeral=True)

        elif value == "tune":
            tune_on = not state.get("tune_enabled", False)
            state["tune_enabled"] = tune_on
            if tune_on:
                from player import _apply_tune_to
                await _apply_tune_to(player_obj)
                player.persist(guild_id)
                await interaction.response.send_message("✨ Tune enabled.", ephemeral=True)
            else:
                filters = player_obj.filters
                filters.equalizer.reset()
                filters.timescale.reset()
                filters.distortion.reset()
                await player_obj.set_filters(filters)
                player.persist(guild_id)
                await interaction.response.send_message("✨ Tune disabled.", ephemeral=True)

        elif value == "equalizer":
            from cogs.equalizer_view import EqualizerView, _build_eq_embed
            eq_view = EqualizerView(guild_id)
            embed = _build_eq_embed(eq_view.gains, eq_view.selected_band)
            await interaction.response.send_message(embed=embed, view=eq_view, ephemeral=True)

        elif value == "8bit":
            filters = player_obj.filters
            gains = [0, 0.05, 0.1, 0.2, 0.25, 0.3, 0.25, 0.2, 0.15, 0.1, 0.05, 0, -0.1, -0.2, -0.3]
            bands = [{"band": i, "gain": g} for i, g in enumerate(gains)]
            filters.distortion.set(scale=2.0)
            filters.tremolo.set(frequency=16.0, depth=0.6)
            filters.vibrato.set(frequency=12.0, depth=0.4)
            filters.timescale.set(speed=1.0, pitch=1.1, rate=1.0)
            filters.equalizer.set(bands=bands)
            filters.low_pass.reset()
            filters.rotation.reset()
            filters.karaoke.reset()
            filters.channel_mix.reset()
            await player_obj.set_filters(filters)
            state["filters"]["8bit"] = {
                "gains": gains,
                "speed": 1.0, "pitch": 1.1, "rate": 1.0,
                "distortion_scale": 2.0,
                "tremolo": {"frequency": 16.0, "depth": 0.6},
                "vibrato": {"frequency": 12.0, "depth": 0.4},
            }
            player.persist(guild_id)
            await interaction.response.send_message("🕹️ 8-Bit arcade filter applied.", ephemeral=True)

        elif value == "808":
            import sounds
            await interaction.response.defer(ephemeral=True)
            path = await sounds.ensure_preset(sounds.DEFAULT_PRESET)
            if not path:
                await interaction.followup.send(
                    "Could not load the 808 cowbell sample.", ephemeral=True
                )
                return
            ok = await sounds.play_sound(player_obj, path, volume=100)
            if ok:
                await interaction.followup.send("🔔 808 cowbell played.", ephemeral=True)
            else:
                await interaction.followup.send(
                    "Could not play the 808 cowbell.", ephemeral=True
                )


class _PlaylistSelectView(discord.ui.View):
    """Ephemeral dropdown to pick a playlist for the ➕ button."""

    def __init__(self, guild_id: int, track_entry: dict):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.track_entry = track_entry

        import storage
        names = storage.names(guild_id)[:25]  # Discord max 25 options
        options = [
            discord.SelectOption(label=name, value=name)
            for name in names
        ]
        select = discord.ui.Select(
            placeholder="Select playlist…", options=options,
            custom_id="unified_playlist_pick",
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        import storage
        name = interaction.data["values"][0]
        storage.add(self.guild_id, name, self.track_entry)
        title = self.track_entry.get("title", "current track")
        await interaction.response.send_message(
            f"➕ Added **{title}** to playlist **{name}**.", ephemeral=True
        )
        self.stop()
