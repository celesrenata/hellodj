"""Playlist cog: the /playlist command group (shared, per-guild playlists)."""

import discord
from discord import app_commands
from discord.ext import commands
import wavelink
from wavelink import Playable, TrackSource

import player
import storage
from cogs.music import SearchSelectView


def _fmt_duration(seconds) -> str:
    secs = int(seconds or 0)
    mins, secs = divmod(secs, 60)
    return f"{mins}:{secs:02d}"


def _track_from_info(info: dict) -> dict:
    """Build a lightweight stored-track dict from a search result."""
    return {
        "url": info.get("webpage_url") or info.get("url"),
        "title": info.get("title") or "Unknown",
        "duration": int(info.get("duration") or 0),
    }


def _to_queue_entry(track: dict) -> dict:
    """Map a stored track to a lightweight queue entry (resolved lazily on play)."""
    return {"webpage_url": track["url"], "title": track.get("title", "Unknown")}


class ConfirmView(discord.ui.View):
    def __init__(self, invoker_id: int):
        super().__init__(timeout=30)
        self.invoker_id = invoker_id
        self.confirmed: bool | None = None

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who ran the command can answer.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self._guard(interaction):
            return
        self.confirmed = False
        self.stop()
        await interaction.response.defer()


class Playlists(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    group = app_commands.Group(name="playlist", description="Create and manage playlists")

    # --- autocomplete helpers ---

    async def _name_autocomplete(self, interaction: discord.Interaction, current: str):
        current = current.casefold()
        return [
            app_commands.Choice(name=n, value=n)
            for n in storage.names(interaction.guild.id)
            if current in n.casefold()
        ][:25]

    async def _track_autocomplete(self, interaction: discord.Interaction, current: str):
        name = getattr(interaction.namespace, "name", None)
        pl = storage.get(interaction.guild.id, name) if name else None
        if not pl:
            return []
        current = current.casefold()
        choices = []
        for i, t in enumerate(pl["tracks"]):
            label = f"{i + 1}. {t.get('title', 'Unknown')}"[:100]
            if current in label.casefold():
                choices.append(app_commands.Choice(name=label, value=str(i)))
        return choices[:25]

    # --- commands ---

    @group.command(name="create", description="Create a new empty playlist")
    @app_commands.describe(name="Name for the new playlist")
    async def create(self, interaction: discord.Interaction, name: str):
        try:
            await storage.create(interaction.guild.id, name, interaction.user.id)
        except storage.PlaylistError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(f"Created playlist **{name}**.", ephemeral=True)

    @group.command(name="delete", description="Delete a playlist")
    @app_commands.describe(name="Playlist to delete")
    @app_commands.autocomplete(name=_name_autocomplete)
    async def delete(self, interaction: discord.Interaction, name: str):
        pl = storage.get(interaction.guild.id, name)
        if not pl:
            await interaction.response.send_message(f"No playlist named **{name}**.", ephemeral=True)
            return
        view = ConfirmView(interaction.user.id)
        await interaction.response.send_message(
            f"Delete **{name}** ({len(pl['tracks'])} track(s))? This can't be undone.",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if view.confirmed:
            try:
                await storage.delete(interaction.guild.id, name)
                msg = f"Deleted **{name}**."
            except storage.PlaylistError as exc:
                msg = str(exc)
        else:
            msg = "Cancelled."
        await interaction.edit_original_response(content=msg, view=None)

    @group.command(name="add", description="Add a track to a playlist (URL or search)")
    @app_commands.describe(name="Playlist to add to", query="YouTube URL or search terms")
    @app_commands.autocomplete(name=_name_autocomplete)
    async def add(self, interaction: discord.Interaction, name: str, query: str):
        await interaction.response.defer(ephemeral=True)
        gid = interaction.guild.id

        # Use wavelink 3.5 Playable.search
        try:
            is_url = query.startswith("http://") or query.startswith("https://")
            tracks = await Playable.search(query, source=TrackSource.YouTubeMusic)
            if not tracks:
                tracks = await Playable.search(query, source=TrackSource.YouTube)

            if not tracks:
                await interaction.followup.send("No results found.", ephemeral=True)
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
                    key = await storage.add_track(gid, name, _track_from_info(info))
                    await picker.response.edit_message(
                        content=f"Added **{info.get('title', 'Unknown')}** to **{key}**.", view=None
                    )

                view = player.SearchSelectView(results, interaction.user.id, on_pick)
                msg = await interaction.followup.send("Select a song to add:", view=view, ephemeral=True)
                view.message = msg
                return

            track = tracks[0]
            info = {
                "url": str(track.url),
                "title": track.name or "Unknown",
                "duration": track.duration or 0,
            }
            key = await storage.add_track(gid, name, info)
            await interaction.followup.send(
                f"Added **{info['title']}** to **{key}**.", ephemeral=True
            )
        except Exception as exc:
            await interaction.followup.send(f"Could not add: {exc}", ephemeral=True)

    @group.command(name="addcurrent", description="Add the currently playing track to a playlist")
    @app_commands.describe(name="Playlist to add to")
    @app_commands.autocomplete(name=_name_autocomplete)
    async def addcurrent(self, interaction: discord.Interaction, name: str):
        current = player.get_state(interaction.guild.id).get("current")
        if not current:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        key = await storage.add_track(interaction.guild.id, name, _track_from_info(current))
        await interaction.response.send_message(
            f"Added **{current.get('title', 'Unknown')}** to **{key}**.", ephemeral=True
        )

    @group.command(name="remove", description="Remove a track from a playlist")
    @app_commands.describe(name="Playlist", track="Track to remove")
    @app_commands.autocomplete(name=_name_autocomplete, track=_track_autocomplete)
    async def remove(self, interaction: discord.Interaction, name: str, track: str):
        try:
            index = int(track)
        except ValueError:
            await interaction.response.send_message("Pick a track from the list.", ephemeral=True)
            return
        try:
            removed = await storage.remove_track(interaction.guild.id, name, index)
        except storage.PlaylistError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Removed **{removed.get('title', 'Unknown')}** from **{name}**.", ephemeral=True
        )

    @group.command(name="edit", description="Edit playlist metadata (name, visibility, description)")
    @app_commands.describe(
        name="Playlist to edit",
        new_name="New name for the playlist",
        visibility="Visibility: public or private",
        description="New description",
    )
    @app_commands.autocomplete(name=_name_autocomplete)
    @app_commands.choices(visibility=[
        app_commands.Choice(name="Public", value="public"),
        app_commands.Choice(name="Private", value="private"),
    ])
    async def edit(
        self,
        interaction: discord.Interaction,
        name: str,
        new_name: str | None = None,
        visibility: str | None = None,
        description: str | None = None,
    ):
        try:
            result = await storage.edit(
                interaction.guild.id, name,
                new_name=new_name,
                visibility=visibility,
                description=description,
            )
            msg = f"Updated playlist **{result}**."
        except storage.PlaylistError as exc:
            msg = str(exc)
        await interaction.response.send_message(msg, ephemeral=True)

    @group.command(name="list", description="List all playlists")
    async def list_cmd(self, interaction: discord.Interaction):
        playlists = storage.list_playlists(interaction.guild.id)
        if not playlists:
            await interaction.response.send_message("No playlists yet. Use `/playlist create`.")
            return
        embed = discord.Embed(title="Playlists", colour=discord.Colour.blurple())
        for name in sorted(playlists, key=str.casefold):
            count = len(playlists[name]["tracks"])
            vis = playlists[name].get("visibility", "public")
            desc = playlists[name].get("description", "")
            label = f"{count} track(s) — {vis}"
            if desc:
                label += f"\n{desc[:60]}"
            embed.add_field(name=name, value=label, inline=True)
        await interaction.response.send_message(embed=embed)

    @group.command(name="show", description="Show the tracks in a playlist")
    @app_commands.describe(name="Playlist to show")
    @app_commands.autocomplete(name=_name_autocomplete)
    async def show(self, interaction: discord.Interaction, name: str):
        pl = storage.get(interaction.guild.id, name)
        if pl is None:
            await interaction.response.send_message(f"No playlist named **{name}**.", ephemeral=True)
            return
        tracks = pl["tracks"]
        if not tracks:
            await interaction.response.send_message(f"**{name}** is empty.")
            return

        limit = 25
        lines = [
            f"{i + 1}. **{t.get('title', 'Unknown')}** ({_fmt_duration(t.get('duration'))})"
            for i, t in enumerate(tracks[:limit])
        ]
        if len(tracks) > limit:
            lines.append(f"…and {len(tracks) - limit} more")
        embed = discord.Embed(
            title=f"{name} — {len(tracks)} track(s)",
            description="\n".join(lines),
            colour=discord.Colour.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    @group.command(name="play", description="Play a playlist")
    @app_commands.describe(
        name="Playlist to play",
        mode="Append to the queue or replace it",
        shuffle="Shuffle the tracks",
    )
    @app_commands.autocomplete(name=_name_autocomplete)
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="Append to queue", value="append"),
            app_commands.Choice(name="Replace queue", value="replace"),
        ]
    )
    async def play(
        self,
        interaction: discord.Interaction,
        name: str,
        mode: str = "append",
        shuffle: bool = False,
    ):
        if not interaction.user.voice:
            await interaction.response.send_message("You need to be in a voice channel first.")
            return

        pl = storage.get(interaction.guild.id, name)
        if pl is None:
            await interaction.response.send_message(f"No playlist named **{name}**.", ephemeral=True)
            return
        if not pl["tracks"]:
            await interaction.response.send_message(f"**{name}** is empty.", ephemeral=True)
            return

        await interaction.response.defer()

        voice_channel = interaction.user.voice.channel
        state = player.get_state(interaction.guild.id)
        state["voice_channel"] = voice_channel

        entries = [_to_queue_entry(t) for t in pl["tracks"]]
        count = await player.enqueue_and_start(
            interaction.guild,
            interaction.channel,
            entries,
            replace=(mode == "replace"),
            shuffle=shuffle,
        )
        verb = "Replaced queue with" if mode == "replace" else "Queued"
        extra = " (shuffled)" if shuffle else ""
        await interaction.followup.send(f"{verb} **{count}** track(s) from **{name}**{extra}.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Playlists(bot))
