"""HelloDJ — Music cog: voice playback and queue commands — builds on wavelink 3.5."""

import asyncio

import discord
from discord import app_commands
from discord.ext import commands
import wavelink
from wavelink import Playable, TrackSource

import player
import session


class SaveQueueView(discord.ui.View):
    def __init__(self, invoker_id: int):
        super().__init__(timeout=30)
        self.invoker_id = invoker_id
        self.save = True

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who ran the command can answer.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Save queue", style=discord.ButtonStyle.success)
    async def save_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        self.save = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Discard", style=discord.ButtonStyle.secondary)
    async def discard_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        self.save = False
        self.stop()
        await interaction.response.defer()


class QueuePaginatedView(discord.ui.View):
    def __init__(self, guild_id: int, page_size: int = 10):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.page_size = page_size
        self.page = 0

    def _embed(self) -> discord.Embed:
        state = player.get_state(self.guild_id)
        current = state.get("current")
        items = player.get_queue_page(state, self.page, self.page_size)
        total_pages = max(1, (len(state["queue"]) + self.page_size - 1) // self.page_size)

        embed = discord.Embed(title="🎶 HelloDJ Queue", colour=discord.Colour.blurple())
        if current:
            embed.add_field(name="Now Playing", value=f"**{current.get('title', 'Unknown')}**", inline=False)

        if items:
            start = self.page * self.page_size
            lines = [f"{start + i + 1}. **{item.get('title', 'Unknown')}**" for i, item in enumerate(items)]
            embed.add_field(name=f"Up Next  (Page {self.page + 1}/{total_pages})", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Up Next", value="Empty", inline=False)

        embed.set_footer(text=f"{len(state['queue'])} track(s) total — HelloDJ")
        return embed

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, custom_id="q_prev")
    async def prev_page(self, interaction: discord.Interaction, _button: discord.ui.Button):
        state = player.get_state(self.guild_id)
        total_pages = max(1, (len(state["queue"]) + self.page_size - 1) // self.page_size)
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, custom_id="q_next")
    async def next_page(self, interaction: discord.Interaction, _button: discord.ui.Button):
        state = player.get_state(self.guild_id)
        total_pages = max(1, (len(state["queue"]) + self.page_size - 1) // self.page_size)
        if self.page < total_pages - 1:
            self.page += 1
        await interaction.response.edit_message(embed=self._embed(), view=self)


class SearchSelectView(discord.ui.View):
    """Dropdown of search results."""

    def __init__(self, results: list[dict], invoker_id: int, on_pick):
        super().__init__(timeout=60)
        self.results = results
        self.invoker_id = invoker_id
        self.on_pick = on_pick
        self.message: discord.Message | None = None

        options = []
        for i, info in enumerate(results):
            title = (info.get("title") or "Unknown")[:100]
            duration = info.get("duration") or 0
            mins, secs = divmod(int(duration), 60)
            uploader = (info.get("uploader") or "")[:50]
            desc = f"{uploader} • {mins}:{secs:02d}" if uploader else f"{mins}:{secs:02d}"
            options.append(discord.SelectOption(label=title, value=str(i), description=desc[:100]))

        select = discord.ui.Select(placeholder="Choose a song…", options=options)
        select.callback = self._on_select
        self.add_item(select)

        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who searched can cancel.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Search cancelled.", view=None)
        self.stop()

    async def _on_select(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who searched can pick a song.", ephemeral=True)
            return
        idx = int(interaction.data["values"][0])
        info = self.results[idx]
        await self.on_pick(info, interaction)
        self.stop()

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(content="Search timed out.", view=None)
            except discord.HTTPException:
                pass


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Connection ──────────────────────────────────────────

    @app_commands.command(name="join", description="Join your current voice channel")
    async def join(self, interaction: discord.Interaction):
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.")
            return
        await interaction.response.defer()
        channel = interaction.user.voice.channel
        state = player.get_state(interaction.guild.id)
        state["voice_channel"] = channel
        state["text_channel"] = interaction.channel

        # Create wavelink Player for this guild (HybridPlayer when voice_recv is present)
        player_obj = await player.connect_player(channel)
        state["player"] = player_obj

        await interaction.followup.send(f"HelloDJ joined **{channel.name}**.")

    # ── Play ────────────────────────────────────────────────

    async def _resolve_tracks(self, query: str, provider: str) -> list:
        source_map = {
            "youtube": TrackSource.YouTube,
            "youtube_music": TrackSource.YouTubeMusic,
            "soundcloud": TrackSource.SoundCloud,
            "spotify": TrackSource.Spotify,
            "tidal": "tidal",
        }
        source = source_map.get(provider, TrackSource.YouTube)
        if provider == "tidal":
            tidal_query = f"tdsearch:{query}" if not query.startswith("http") else query
            tracks = await Playable.search(tidal_query, source=TrackSource.YouTube)  # source ignored; prefix drives routing
            if not tracks:
                tracks = await Playable.search(query, source=TrackSource.YouTube)  # fallback
            return tracks
        tracks = await Playable.search(query, source=source)
        if not tracks:
            # Fallback
            tracks = await Playable.search(query, source=TrackSource.YouTube)
        return tracks

    @app_commands.command(name="play", description="Play a song (URL or search)")
    @app_commands.describe(query="URL or search terms")
    async def play(self, interaction: discord.Interaction, query: str):
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.")
            return
        await interaction.response.defer()

        voice_channel = interaction.user.voice.channel
        state = player.get_state(interaction.guild.id)
        state["voice_channel"] = voice_channel
        state["text_channel"] = interaction.channel
        state["persist_enabled"] = True

        # Connect wavelink player if not present (HybridPlayer when voice_recv is present)
        player_obj = state.get("player")
        if not player_obj or not player_obj.connected:
            player_obj = await player.connect_player(voice_channel)
            state["player"] = player_obj

        provider = state.get("source_provider", "youtube")
        is_url = query.startswith("http://") or query.startswith("https://")

        try:
            tracks = await self._resolve_tracks(query, provider)
            if not tracks:
                await interaction.followup.send("No results found.")
                return

            # For search queries, show selection dropdown
            if not is_url and len(tracks) > 1:
                results = []
                for t in tracks[:5]:
                    results.append({
                        "webpage_url": str(t.url),
                        "title": t.name,
                        "duration": t.duration,
                        "uploader": t.author,
                    })

                async def on_pick(info: dict, picker: discord.Interaction):
                    title = info.get("title") or "Unknown"
                    await player.add_track(state, interaction.guild.id, info)
                    p = player.get_player(interaction.guild.id)
                    if p and p.connected and not p.playing and not p.paused:
                        await player._play_next_from_queue(interaction.guild.id)
                        content = f"HelloDJ loading: **{title}**"
                    else:
                        content = f"HelloDJ added to queue (#{len(state['queue'])}): **{title}**"
                    await picker.response.edit_message(content=content, view=None)

                view = SearchSelectView(results, interaction.user.id, on_pick)
                msg = await interaction.followup.send("Select a song:", view=view)
                view.message = msg
                return

            # Direct URL or single result
            track = tracks[0]
            info = {
                "webpage_url": str(track.url),
                "title": track.name or "Unknown",
                "author": track.author or "",
                "duration": track.duration or 0,
            }
            await player.add_track(state, interaction.guild.id, info)
            await interaction.followup.send(f"HelloDJ added to queue: **{info['title']}**")

        except Exception as exc:
            await interaction.followup.send(f"Could not play: {exc}")

    # ── Pause / Resume ──────────────────────────────────────

    @app_commands.command(name="pause", description="Pause playback")
    async def pause(self, interaction: discord.Interaction):
        player_obj = player.get_player(interaction.guild.id)
        if player_obj and player_obj.playing:
            await player_obj.pause()
            await interaction.response.send_message("HelloDJ paused.")
        else:
            await interaction.response.send_message("HelloDJ: Nothing is playing.")

    @app_commands.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction):
        player_obj = player.get_player(interaction.guild.id)
        if player_obj and player_obj.paused:
            await player_obj.resume()
            await interaction.response.send_message("HelloDJ resumed.")
        else:
            await interaction.response.send_message("HelloDJ: Nothing is paused.")

    @app_commands.command(name="start", description="Alias for /resume")
    async def start(self, interaction: discord.Interaction):
        await self.resume(interaction)

    # ── Skip ────────────────────────────────────────────────

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        player_obj = player.get_player(interaction.guild.id)
        if player_obj and (player_obj.playing or player_obj.paused):
            await player_obj.stop()
            await interaction.response.send_message("HelloDJ skipped.")
        else:
            await interaction.response.send_message("HelloDJ: Nothing to skip.")

    @app_commands.command(name="next", description="Alias for /skip")
    async def next_cmd(self, interaction: discord.Interaction):
        await self.skip(interaction)

    # ── Stop / Clear ────────────────────────────────────────

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        state = player.get_state(gid)
        had_content = bool(state.get("current")) or bool(state["queue"])
        snap = player._snapshot(state)
        state["persist_enabled"] = False
        player.clear_queue(state)
        player_obj = player.get_player(gid)
        if player_obj and (player_obj.playing or player_obj.paused):
            await player_obj.stop()
        state["current"] = None

        if not had_content:
            await session.clear(gid)
            await interaction.response.send_message("HelloDJ stopped and cleared the queue.")
            return

        view = SaveQueueView(interaction.user.id)
        await interaction.response.send_message(
            "HelloDJ stopped. Save this queue so you can `/continue` later?", view=view
        )
        await view.wait()
        if view.save:
            await session.save_guild(gid, auto_resume=False, **snap)
            msg = "HelloDJ saved — use `/continue` to resume this queue."
        else:
            await session.clear(gid)
            msg = "HelloDJ stopped and cleared the queue."
        await interaction.edit_original_response(content=msg, view=None)

    @app_commands.command(name="clear", description="Stop and discard the queue (no save prompt)")
    async def clear_cmd(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        state = player.get_state(gid)
        state["persist_enabled"] = False
        player.clear_queue(state)
        player_obj = player.get_player(gid)
        if player_obj and (player_obj.playing or player_obj.paused):
            await player_obj.stop()
        state["current"] = None
        await session.clear(gid)
        await interaction.response.send_message("HelloDJ cleared the queue.")

    # ── Queue display ───────────────────────────────────────

    @app_commands.command(name="queue", description="Show the current queue (paginated)")
    @app_commands.choices(
        view_type=[
            app_commands.Choice(name="Simple list", value="simple"),
            app_commands.Choice(name="Paginated embed", value="paginated"),
        ]
    )
    async def queue_cmd(self, interaction: discord.Interaction, view_type: str = "simple"):
        state = player.get_state(interaction.guild.id)
        current = state.get("current")
        items = state["queue"]

        if view_type == "paginated":
            view = QueuePaginatedView(interaction.guild.id)
            await interaction.response.send_message(embed=view._embed(), view=view)
            return

        if not current and not items:
            await interaction.response.send_message("HelloDJ queue is empty.")
            return
        lines = []
        if current:
            lines.append(f"HelloDJ now playing: **{current.get('title', 'Unknown')}**")
        lines += [f"{i + 1}. **{item.get('title', 'Unknown')}**" for i, item in enumerate(items)]
        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="list", description="Alias for /queue (paginated)")
    async def list_cmd(self, interaction: discord.Interaction):
        await self.queue_cmd(interaction, "paginated")

    @app_commands.command(name="q", description="Alias for /queue (simple)")
    async def q_cmd(self, interaction: discord.Interaction):
        await self.queue_cmd(interaction, "simple")

    # ── Add (append without interrupting) ───────────────────

    @app_commands.command(name="add", description="Add a song to the queue without interrupting playback")
    @app_commands.describe(query="URL or search terms")
    async def add(self, interaction: discord.Interaction, query: str):
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.")
            return
        await interaction.response.defer()

        state = player.get_state(interaction.guild.id)
        state["voice_channel"] = interaction.user.voice.channel
        state["text_channel"] = interaction.channel

        provider = state.get("source_provider", "youtube")
        is_url = query.startswith("http://") or query.startswith("https://")

        try:
            tracks = await self._resolve_tracks(query, provider)
            if not tracks:
                await interaction.followup.send("No results found.")
                return

            if not is_url and len(tracks) > 1:
                results = []
                for t in tracks[:5]:
                    results.append({
                        "webpage_url": str(t.url),
                        "title": t.name,
                        "duration": t.duration,
                        "uploader": t.author,
                    })

                async def on_pick(info: dict, picker: discord.Interaction):
                    title = info.get("title") or "Unknown"
                    await player.add_track(state, interaction.guild.id, info)
                    await picker.response.edit_message(
                        content=f"HelloDJ added to queue (#{len(state['queue'])}): **{title}**", view=None
                    )

                view = SearchSelectView(results, interaction.user.id, on_pick)
                msg = await interaction.followup.send("Select a song to add:", view=view)
                view.message = msg
                return

            track = tracks[0]
            info = {
                "webpage_url": str(track.url),
                "title": track.name or "Unknown",
                "author": track.author or "",
                "duration": track.duration or 0,
            }
            await player.add_track(state, interaction.guild.id, info)
            await interaction.followup.send(f"HelloDJ added to queue (#{len(state['queue'])}): **{info['title']}**")

        except Exception as exc:
            await interaction.followup.send(f"Could not add: {exc}")

    # ── Remove ──────────────────────────────────────────────

    @app_commands.command(name="remove", description="Remove a track from the queue by index")
    @app_commands.describe(index="Track number to remove (1-based)")
    async def remove(self, interaction: discord.Interaction, index: int):
        state = player.get_state(interaction.guild.id)
        if not state["queue"]:
            await interaction.response.send_message("HelloDJ queue is empty.")
            return
        removed = player.remove_from_queue(state, index - 1)
        if removed is None:
            await interaction.response.send_message(f"Invalid index. Queue has {len(state['queue'])} track(s).")
            return
        player.persist(interaction.guild.id)
        await interaction.response.send_message(f"HelloDJ removed **{removed.get('title', 'Unknown')}** from the queue.")

    @app_commands.command(name="delete", description="Alias for /remove")
    @app_commands.describe(index="Track number to delete (1-based)")
    async def delete(self, interaction: discord.Interaction, index: int):
        await self.remove(interaction, index)

    # ── Move ────────────────────────────────────────────────

    @app_commands.command(name="move", description="Move a track to a new position in the queue")
    @app_commands.describe(from_index="Current position (1-based)", to_index="Target position (1-based)")
    async def move(self, interaction: discord.Interaction, from_index: int, to_index: int):
        state = player.get_state(interaction.guild.id)
        if not state["queue"]:
            await interaction.response.send_message("HelloDJ queue is empty.")
            return
        ok = player.move_in_queue(state, from_index - 1, to_index - 1)
        if not ok:
            await interaction.response.send_message(f"Invalid indices. Queue has {len(state['queue'])} track(s).")
            return
        player.persist(interaction.guild.id)
        await interaction.response.send_message("HelloDJ track moved.")

    # ── Shuffle ─────────────────────────────────────────────

    @app_commands.command(name="shuffle", description="Randomize the order of tracks in the queue")
    async def shuffle(self, interaction: discord.Interaction):
        state = player.get_state(interaction.guild.id)
        if not state["queue"]:
            await interaction.response.send_message("HelloDJ queue is empty.")
            return
        player.shuffle_queue(state)
        player.persist(interaction.guild.id)
        await interaction.response.send_message("HelloDJ queue shuffled.")

    # ── Repeat ──────────────────────────────────────────────

    @app_commands.command(name="repeat", description="Toggle repeat mode")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Off", value="off"),
        app_commands.Choice(name="Single Track", value="single"),
        app_commands.Choice(name="Full Queue", value="queue"),
    ])
    async def repeat(self, interaction: discord.Interaction, mode: str = ""):
        state = player.get_state(interaction.guild.id)
        if mode:
            player.set_repeat(state, mode)
        else:
            modes = ["off", "single", "queue"]
            current = state["repeat_mode"]
            next_mode = modes[(modes.index(current) + 1) % len(modes)] if current in modes else "off"
            player.set_repeat(state, next_mode)
            mode = next_mode
        player.persist(interaction.guild.id)
        await interaction.response.send_message(f"HelloDJ repeat: **{mode}**")

    # ── Source ──────────────────────────────────────────────

    @app_commands.command(name="source", description="Set the preferred streaming source/provider")
    @app_commands.choices(provider=[
        app_commands.Choice(name="YouTube", value="youtube"),
        app_commands.Choice(name="YouTube Music", value="youtube_music"),
        app_commands.Choice(name="SoundCloud", value="soundcloud"),
        app_commands.Choice(name="Spotify", value="spotify"),
        app_commands.Choice(name="Tidal", value="tidal"),
    ])
    async def source(self, interaction: discord.Interaction, provider: str):
        state = player.get_state(interaction.guild.id)
        state["source_provider"] = provider
        player.persist(interaction.guild.id)
        await interaction.response.send_message(f"HelloDJ source set to **{provider}**.")

    # ── Leave ───────────────────────────────────────────────

    @app_commands.command(name="leave", description="Disconnect HelloDJ from voice")
    async def leave(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        state = player.get_state(gid)
        player_obj = player.get_player(gid)
        if not player_obj or not player_obj.connected:
            await interaction.response.send_message("HelloDJ is not in a voice channel.")
            return

        had_content = bool(state.get("current")) or bool(state["queue"])
        snap = player._snapshot(state)
        state["persist_enabled"] = False
        player.clear_queue(state)
        state["current"] = None
        await player_obj.disconnect()

        if not had_content:
            await session.clear(gid)
            await interaction.response.send_message("HelloDJ disconnected.")
            return

        view = SaveQueueView(interaction.user.id)
        message = await interaction.response.send_message(
            "HelloDJ disconnected. Save this queue so you can `/continue` later?", view=view
        )
        await view.wait()
        if view.save:
            await session.save_guild(gid, auto_resume=False, **snap)
            text = "HelloDJ saved — use `/continue` to resume this queue."
        else:
            await session.clear(gid)
            text = "HelloDJ disconnected."
        await message.edit(content=text, view=None)

    # ── Continue ────────────────────────────────────────────

    @app_commands.command(name="continue", description="Resume a previously saved queue")
    async def continue_cmd(self, interaction: discord.Interaction):
        gid = interaction.guild.id
        saved = session.get(gid)
        if not saved or not (saved.get("current") or saved.get("queue")):
            await interaction.response.send_message("There's no saved queue to resume.", ephemeral=True)
            return
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.", ephemeral=True)
            return

        await interaction.response.defer()
        voice_channel = interaction.user.voice.channel
        state = player.get_state(gid)
        state["voice_channel"] = voice_channel
        state["text_channel"] = interaction.channel

        entries = []
        if saved.get("current"):
            entries.append(saved["current"])
        entries.extend(saved.get("queue", []))
        count = await player.enqueue_and_start(interaction.guild, interaction.channel, entries, replace=True)
        await interaction.followup.send(f"HelloDJ resuming **{count}** track(s) from your saved queue.")

    # ── Voice state listener ────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, _before: discord.VoiceState, _after: discord.VoiceState):
        guild = member.guild
        player_obj = player.get_player(guild.id)
        if not player_obj or not player_obj.connected:
            return
        vc_channel = player_obj.channel
        if not vc_channel:
            return

        human_members = [m for m in vc_channel.members if not m.bot]
        state = player.get_state(guild.id)

        if human_members:
            alone_task = state.get("alone_task")
            if alone_task and not alone_task.done():
                alone_task.cancel()
            state["alone_task"] = None
            return

        if state.get("alone_task") and not state["alone_task"].done():
            return

        async def _leave_if_alone() -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                return
            p2 = player.get_player(guild.id)
            if p2 and p2.connected:
                if not any(not m.bot for m in p2.channel.members):
                    had_content = bool(state.get("current")) or bool(state["queue"])
                    if had_content:
                        await player.park(guild.id)
                    else:
                        await player.discard(guild.id)
                    player.clear_queue(state)
                    state["current"] = None
                    await p2.disconnect()
                    text_ch = state.get("text_channel")
                    if text_ch:
                        if had_content:
                            await text_ch.send("HelloDJ: Everyone left — I saved the queue. Use `/continue` to resume.")
                        else:
                            await text_ch.send("HelloDJ: Everyone left — disconnected from voice.")
            state["alone_task"] = None

        state["alone_task"] = asyncio.ensure_future(_leave_if_alone())


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
