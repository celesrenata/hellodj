"""Unified skip/previous controls for HelloDJ.

A single entry point for skip and previous that works regardless of whether
audio (wavelink) or video (Activity streamer) is currently active. All callers
(WS hub, Discord buttons, slash commands) should use these functions.

Handles:
- Video active: skip/prev within video streamer queue + fallback to unified queue
- Audio active: skip/prev within audio player + history
- Nothing active: previous from history (restarts last played)
- Cross-media transitions (video → audio, audio → video)
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


async def unified_skip(guild_id: int) -> str:
    """Skip the current track (audio or video) and advance the unified queue.

    Returns a status string describing what happened:
    - "skipped_video" — skipped within video streamer's own queue
    - "skipped_to_next" — video ended, advanced unified queue (next could be audio or video)
    - "skipped_audio" — stopped audio track (on_track_end will advance queue)
    - "queue_empty" — nothing to skip to, everything stopped
    - "nothing_playing" — nothing was active
    """
    import player

    # Check if video is active
    if player._is_video_active(guild_id):
        return await _skip_video(guild_id)

    # Check if audio is playing
    p = player.get_player(guild_id)
    if p and (p.playing or p.paused):
        # Set transition flag to suppress on_track_end from also advancing
        state = player.get_state(guild_id)
        state["_skip_transition"] = True
        await p.stop()
        state.pop("_skip_transition", None)
        # Directly advance the queue (don't rely on on_track_end)
        await player._play_next_from_queue(guild_id)
        return "skipped_audio"

    # Nothing active — try advancing queue directly
    state = player.get_state(guild_id)
    if state["queue"]:
        await player._play_next_from_queue(guild_id)
        return "skipped_to_next"

    return "nothing_playing"


async def unified_previous(guild_id: int) -> str:
    """Go to the previous track from unified history.

    Behavior:
    - If video is active: STOP the video, play previous track from history
    - If audio is active: jump to previous audio track from history (or restart)
    - If nothing is active: pop from history and play

    Returns a status string:
    - "prev_audio" — jumped to previous audio track from history
    - "prev_video" — went back within video streamer history (plays previous video)
    - "restarted" — restarted the current track from the beginning
    - "no_history" — no previous track available
    - "nothing_playing" — nothing active and no history
    """
    import player

    state = player.get_state(guild_id)
    history = state.get("history", [])

    # If video is active: stop it and go to previous from unified history
    if player._is_video_active(guild_id):
        return await _previous_from_video(guild_id)

    # Audio active — try audio history
    p = player.get_player(guild_id)
    if p and (p.playing or p.paused):
        if history:
            ok = await player.jump_to(guild_id, history_index=0)
            if ok:
                return "prev_audio"
        # No history — restart current track
        try:
            await p.seek(0)
            return "restarted"
        except Exception:
            pass
        return "no_history"

    # Nothing is playing — check history and restart something
    if history:
        prev_entry = history.pop(0)
        state["queue"].insert(0, prev_entry)
        state["current"] = None
        await player._play_next_from_queue(guild_id)
        return "prev_audio"

    return "nothing_playing"


async def _skip_video(guild_id: int) -> str:
    """Handle skip when video is active — stops video, advances unified queue."""
    import player

    bot_ref = player._bot_ref
    if bot_ref is None:
        return "nothing_playing"

    video_cog = bot_ref.get_cog("Video")
    if video_cog is None:
        return "nothing_playing"

    # Find the active streamer
    streamer = None
    for channel_id_s, session in video_cog._registry.get_by_guild(guild_id):
        if session.is_active:
            streamer = session
            break

    if streamer is None:
        # Video flag set but no active streamer — advance unified queue
        state = player.get_state(guild_id)
        if state["queue"]:
            await player._play_next_from_queue(guild_id)
            return "skipped_to_next"
        return "queue_empty"
            return "skipped_to_next"
        return "queue_empty"

    # Stop the video session
    log.info("unified_skip: stopping video for guild=%d", guild_id)
    try:
        # Push current video to history before stopping
        if streamer.source is not None:
            streamer._push_history(streamer.source)
        await streamer.stop()
    except Exception as exc:
        log.warning("unified_skip: error stopping streamer: %s", exc)

    await _cleanup_idle_streamer(video_cog, guild_id, streamer)

    # Advance unified queue — let _play_next_from_queue handle history push
    state = player.get_state(guild_id)
    if state["queue"]:
        await player._play_next_from_queue(guild_id)
        return "skipped_to_next"

    # Queue empty — manually push current to history since _play_next won't run
    if state.get("current"):
        history = state.setdefault("history", [])
        history.insert(0, state["current"])
        del history[50:]
        state["current"] = None
    return "queue_empty"


async def _previous_from_video(guild_id: int) -> str:
    """Handle previous when video is active — stops video, plays previous from history."""
    import player

    bot_ref = player._bot_ref
    if bot_ref is None:
        return "nothing_playing"

    video_cog = bot_ref.get_cog("Video")
    if video_cog is None:
        return "nothing_playing"

    # Find the active streamer
    streamer = None
    for channel_id_s, session in video_cog._registry.get_by_guild(guild_id):
        if session.is_active:
            streamer = session
            break

    state = player.get_state(guild_id)
    history = state.get("history", [])

    if not history:
        return "no_history"

    # Stop the video session
    if streamer is not None:
        log.info("unified_previous: stopping video for guild=%d to play previous audio", guild_id)
        try:
            await streamer.stop()
        except Exception as exc:
            log.warning("unified_previous: error stopping streamer: %s", exc)
        await _cleanup_idle_streamer(video_cog, guild_id, streamer)

    # Push the current (video) entry back to front of queue so "next" replays it
    current = state.get("current")
    if current:
        state["queue"].insert(0, current)

    # Play previous from unified history
    prev_entry = history.pop(0)
    state["queue"].insert(0, prev_entry)
    state["current"] = None
    await player._play_next_from_queue(guild_id)
    return "prev_audio"


async def _cleanup_idle_streamer(video_cog, guild_id: int, streamer) -> None:
    """Notify Activity clients that the current session ended.

    Does NOT unregister the streamer — keeps it registered so the next video
    can reuse it (same Activity iframe, same WS connections).
    """
    try:
        await video_cog._backend.ws_hub.broadcast_from_bot(guild_id, {"type": "session_end"})
    except Exception:
        pass
