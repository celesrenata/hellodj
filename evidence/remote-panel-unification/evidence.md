# Remote Panel Unification — Implementation Evidence

Task: Unify the remote panel into the now-playing embed, add `/link`, add paginated `/help`, add status messages, and audit command naming.

## 1. Remote Panel Unification (`bot/player.py`)

Rewrote `NowPlayingView` to a unified 4-glyph control panel attached to the now-playing embed:

| Button | Label | `custom_id` | Behavior |
|--------|-------|-------------|----------|
| Previous | `⏮` | `np_prev` | `player.seek(0)` — restart current track |
| Play/Pause toggle | `⏯` / `▶️` | `np_toggle` | toggle `player.pause(True/False)`; icon updates on click via `edit_message(view=self)` |
| Next | `⏭` | `np_next` | `player.stop()` — advance to next track |
| Block | `🚫` | `np_block` | `_blacklist.add_blacklist_entry(guild_id, current)` then `player.stop()`, ephemeral reply `🚫 Blocked **{title}**.` |

- Removed the old 🔀 shuffle and ⏹️ stop buttons.
- Kept `interaction_check` (any member) and `timeout=300`.

Added module-level helper `build_now_playing_embed_from_entry(entry)` that mirrors `_build_now_playing_embed` but takes the `state["current"]` dict entry (keys `webpage_url`, `title`, `author`, `duration`) so `/remote` can render the same per-song embed.

## 2. `/remote` now edits the now-playing message (`bot/cogs/music.py`)

`/remote` now:
- Edits `state["now_playing_msg"]` with `player.NowPlayingView(guild_id)` and `player.build_now_playing_embed_from_entry(current)`.
- Falls back to posting a new message if `now_playing_msg` is missing (NotFound caught).
- Does **not** post `RemoteControlView` anymore. The `RemoteControlView` class is kept intact (still used for the filter dropdown).

## 3. `/link` command (`bot/cogs/music.py`)

Added after `/nowplaying`:

```python
@app_commands.command(name="link", description="Copy the direct link to the currently playing song")
async def link(self, interaction):
    state = player.get_state(interaction.guild.id)
    current = state.get("current")
    if not current:
        ... "Nothing is playing right now." (ephemeral)
    url = current.get("uri") or current.get("url") or current.get("webpage_url")
    if not url:
        ... "No direct link available..." (ephemeral)
    await interaction.response.send_message(f"🔗 {url}", ephemeral=True)
```

## 4. Track-blacklist API (`bot/blacklist.py`)

The task required `bot.blacklist.add_blacklist_entry(guild_id, track_info)`, but the existing module only managed a user-ID blacklist (`{guild_id: [user_id,...]}` shared with the web UI at `data/blacklist.json`). Added a **track** blacklist persisted to a separate `data/track_blacklist.json` to avoid colliding with the web UI shape:

- `TRACK_BLACKLIST_FILE = "data/track_blacklist.json"`
- `track_blacklist: dict[int, list[str]]`
- `load_track_blacklist()` / `save_track_blacklist()` (atomic write)
- `add_blacklist_entry(guild_id, track_info) -> str | None` (reads `webpage_url`/`url`/`uri`)
- `is_track_blacklisted(guild_id, url)`

## 5. Paginated `/help` (`bot/cogs/help.py` — new cog)

- `HelpPageView`: ⬅️ / ➡️ buttons (`help_prev` / `help_next`), `interaction_check` returns True, `timeout=300`.
- `/help` fetches commands dynamically via `self.bot.tree.get_commands(guild=interaction.guild)`.
- Flattens `app_commands.Group` subcommands to `"{group} {sub}"` (e.g. `play song`), dedups, groups logically (🎵 Music / 🎚️ Filters / 🛠️ Utility).
- Chunks to `MAX_PER_PAGE = 25` lines per page; embed title shows `🎵 HelloDJ Commands (X/Y)`.
- Section headers rendered as `\n**Section**`, commands as `**/name** — desc`.
- `setup(bot)` registers the cog.

## 6. Registration (`bot/bot.py`)

- `setup_hook`: added `_blacklist.load_track_blacklist()` and `await bot.load_extension("cogs.help")`.

## 7. Status messages (`bot/cogs/music.py`)

Added ephemeral followup status before slow operations (all commands already `defer(ephemeral=True)`):
- `_play_search_flow`: `"🔄 Searching…"`
- `_play_url_flow`: `"🔄 Resolving…"`
- `_play_album_flow`: `"🔄 Loading album…"`
- `/add`: `"🔄 Searching…"`

## 8. Naming audit (verification only)

- `/filter reset` (group) AND `/filter_reset` (top-level) both exist in `bot/cogs/filters.py` — no changes needed.
- `/np` AND `/nowplaying` both exist in `bot/cogs/music.py` — no changes needed.

## Validation

```
python -m py_compile bot/blacklist.py bot/player.py bot/cogs/music.py bot/cogs/help.py bot/bot.py
# Exit 0 → PY_COMPILE_OK
```

All five modified files compile cleanly.
