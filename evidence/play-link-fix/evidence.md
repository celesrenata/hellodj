# /play link — resolves name but does not actually play

Date: 2026-08-18

## Symptom

`/play link https://www.youtube.com/watch?v=PTlVtPV-Qtw` resolves and returns the track name, but playback never starts. The queue advances but no audio plays.

## Root Cause

The `/play link` subcommand delegates to `_play_url_flow` in `bot/cogs/music.py`. That flow:

1. Resolves the URL via `Playable.search(url, source=TrackSource.YouTube)` — this is why the track **name** resolves correctly.
2. Calls `player.add_track(state, guild_id, info)` for each resolved track.
3. Sends a "added to queue: **title**" reply.

**`player.add_track` only appends the entry to `state["queue"]` and calls `persist()` — it never starts playback.** It does **not** call `_play_next_from_queue()` or `player.play()`. So with a freshly-connected, idle player, the track sits in the queue and no audio ever starts.

This was a regression introduced by the `/play` refactor commit `2615032` ("bug fixes and updates"), which split the old single `play` command into `/play song|link|album|playlist|video` subcommands. The **dropdown path** of `/play song` (`on_pick`, `bot/cogs/music.py:851-853`) correctly calls `await player._play_next_from_queue(...)` after `add_track`, but:

- `_play_url_flow` (used by `/play link`, `/play video`, `/play playlist`) — **never** calls `_play_next_from_queue` / `player.play()`.
- the **direct-URL / single-result branch** of `_play_search_flow` (`bot/cogs/music.py:904-908`) — **never** calls `_play_next_from_queue`.

### Evidence (line numbers in HEAD `2615032`)

| Location | Behavior |
|----------|----------|
| `bot/cogs/music.py:931` `_play_url_flow` | Resolves URL, queues via `add_track`, **no playback trigger** |
| `bot/cogs/music.py:950-970` | All three branches call `player.add_track` + `followup.send("added to queue")` only |
| `bot/cogs/music.py:904-908` | `/play song` direct-URL branch: `add_track` + reply only, **no playback trigger** |
| `bot/cogs/music.py:851-853` `on_pick` | Working dropdown path: `add_track` **then** `await player._play_next_from_queue(...)` |
| `bot/player.py:661` `add_track` | `state["queue"].append(entry); persist(guild_id)` — queue-only, unchanged since initial migration (`git log -S "_play_next_from_queue"`) |
| `bot/player.py:690-692` `enqueue_and_start` | Working queue+start path: `add_track` then `_play_next_from_queue` |

### Ruled-out candidates

- **YouTube source / yt-cipher resolution failing** — the URL resolves and returns the track name, so `Playable.search(url, TrackSource.YouTube)` succeeded. The bug is *downstream* of resolution (no playback start). Not the cause.
- **`on_track_exception` retry loop** — only fires after a track *has started*; `/play link` never starts a track. Not the cause.
- **wavelink event wiring** (`bot.py:788-817` `on_wavelink_track_start/end/exception`) — verified correctly delegates to `player.on_track_*`. No regression.
- **`add_track` regression** — body identical in HEAD vs initial migration (`6496aef`). Not the cause.
- **Recent batch regressions** — `tune_enabled` in `_snapshot` (`bot/player.py:130`), `save_guild` accepting it (`bot/session.py:77`), and the `/tune` re-apply hook in `on_track_start` (`bot/player.py:965-977`, wrapped in try/except) only affect display/persistence, not whether playback starts. Not the cause.

### Verification against live pod

- Deployed pod was `registry.celestium.life/hellodj/bot:batch-fixes-2026-08-18-fix1`.
- `kubectl exec` confirmed the deployed `_play_url_flow` and `add_track` are byte-identical to HEAD — the live system had the bug.
- Pod logs (`kubectl logs -n hellodj-service -l app.kubernetes.io/component=bot`) showed no `/play link` invocation, track exception, or now-playing entries — consistent with the track being queued but never started (no `on_track_start` ever fires).

## Fix

Added a private helper `_start_if_idle` in `bot/cogs/music.py`:

```python
async def _start_if_idle(self, guild_id: int) -> None:
    """Start playback from the queue if the player is connected and idle.

    ``player.add_track`` only appends the resolved entry to the queue and
    persists it — it never starts playback. /play link (and the direct-URL
    branch of /play song) must mirror the dropdown path (``on_pick`` →
    ``player._play_next_from_queue``) so a freshly-connected, idle player
    actually starts playing instead of leaving the track queued forever.
    """
    p = player.get_player(guild_id)
    if p and p.connected and not p.playing and not p.paused:
        await player._play_next_from_queue(guild_id)
```

Called it after `add_track` in **all four** queueing branches that lacked a playback trigger:

- `_play_url_flow` playlist branch (`bot/cogs/music.py:967`)
- `_play_url_flow` list/`allow_playlist` branch (`bot/cogs/music.py:978`)
- `_play_url_flow` single-result branch (`bot/cogs/music.py:986`)
- `_play_search_flow` direct-URL branch (`bot/cogs/music.py:908`)

The guard (`connected and not playing and not paused`) mirrors the existing dropdown path, so it only auto-starts when the player is idle — tracks added while something is already playing are still queued normally.

**Scope:** `bot/cogs/music.py` only. No unrelated features touched.

## Verification

- `python -m py_compile bot/cogs/music.py` → **OK** (no syntax errors).
- `git diff -- bot/cogs/music.py` → diff is exactly the helper + 4 call sites; no unrelated changes.
- Redeployed to Kubernetes with tag **`play-link-fix-2026-08-18-fix1`**:
  - `docker build -t registry.celestium.life/hellodj/bot:play-link-fix-2026-08-18-fix1 bot/` → OK
  - `docker push registry.celestium.life/hellodj/bot:play-link-fix-2026-08-18-fix1` → OK
  - `kubectl set image deployment/hellodj bot=registry.celestium.life/hellodj/bot:play-link-fix-2026-08-18-fix1 -n hellodj-service` → OK
  - `kubectl rollout status deployment/hellodj -n hellodj-service` → successfully rolled out
- New pod `hellodj-747b78497-rwjnn` running on the new image; `kubectl exec` confirms `_start_if_idle` and all 4 call sites are present in `/app/cogs/music.py`.
- New pod startup clean: `HelloDJ connected to Lavalink`, `on_ready fired with 2 guilds`, no errors/exceptions.

## Follow-up (user-verifiable)

A live `/play link <youtube url>` in a voice channel should now:
1. Resolve and show the track name.
2. Trigger `_play_next_from_queue` → `_resolve_and_play` → `player.play(track)`.
3. Fire `on_track_start` and actually play audio (verify via the now-playing embed and audible output).
