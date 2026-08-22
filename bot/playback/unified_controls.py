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
        await p.stop()  # triggers on_track_end → _play_next_from_queue
        return "skipped_audio"

    # Nothing active — try advancing queue directly
    state = player.get_state(guild_id)
    if state["queue"]:
        await player._play_next_from_queue(guild_id)
        return "skipped_to_next"

    return "nothing_playing"


async def unified_previous(guild_id: int) -> str:
    """Go to the previous track (audio or video) from unified history.

    Returns a status string:
    - "prev_video" — went back within video streamer history
    - "prev_audio" — jumped to previous audio track from history
    - "restarted" — restarted the current track from the beginning
    - "no_history" — no previous track available
    - "nothing_playing" — nothing active and no history
    """
    import player

    # Check if video is active
    if player._is_video_active(guild_id):
        return await _previous_video(guild_id)

    # Check if audio is playing — try audio history
    state = player.get_state(guild_id)
    history = state.get("history", [])

    p = player.get_player(guild_id)
    if p and (p.playing or p.paused):
        if history:
            # Jump to previous track
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
        # Push history[0] to front of queue and play
        prev_entry = history.pop(0)
        state["queue"].insert(0, prev_entry)
        state["current"] = None
        await player._play_next_from_queue(guild_id)
        return "prev_audio"

    return "nothing_playing"


async def _skip_video(guild_id: int) -> str:
    """Handle skip when video is active."""
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
        return "nothing_playing"

    # Try to skip within the video streamer
    had_queue = len(streamer.queue) > 0
    try:
        await streamer.skip()
    except Exception as exc:
        log.debug("unified_skip: video skip failed guild=%d: %s", guild_id, exc)
        # Streamer not in a skippable state — try unified queue
        state = player.get_state(guild_id)
        if state["queue"]:
            state["current"] = None
            await player._play_next_from_queue(guild_id)
            return "skipped_to_next"
        return "queue_empty"

    # Skip succeeded — check what happened
    if streamer.is_active and streamer.source:
        # Still playing (advanced within video queue)
        return "skipped_video"

    # Video streamer went idle — advance unified queue
    state = player.get_state(guild_id)
    if state["queue"]:
        # Clean up the video session
        _cleanup_idle_streamer(video_cog, guild_id, streamer)
        state["current"] = None
        await player._play_next_from_queue(guild_id)
        return "skipped_to_next"

    # Truly empty
    _cleanup_idle_streamer(video_cog, guild_id, streamer)
    return "queue_empty"


async def _previous_video(guild_id: int) -> str:
    """Handle previous when video is active."""
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
        # Video was active per flag but no streamer — fall back to audio history
        state = player.get_state(guild_id)
        history = state.get("history", [])
        if history:
            prev_entry = history.pop(0)
            state["queue"].insert(0, prev_entry)
            state["current"] = None
            await player._play_next_from_queue(guild_id)
            return "prev_audio"
        return "no_history"

    # Try video streamer's internal previous
    try:
        result = await streamer.previous()
        if result:
            return "prev_video"
    except Exception as exc:
        log.debug("unified_previous: video previous failed guild=%d: %s", guild_id, exc)

    # Video previous failed or returned False (no video history)
    # Fall back to unified audio history
    state = player.get_state(guild_id)
    history = state.get("history", [])
    if history:
        # Stop video, go back to audio
        _cleanup_idle_streamer(video_cog, guild_id, streamer)
        prev_entry = history.pop(0)
        state["queue"].insert(0, prev_entry)
        state["current"] = None
        await player._play_next_from_queue(guild_id)
        return "prev_audio"

    return "no_history"


def _cleanup_idle_streamer(video_cog, guild_id: int, streamer) -> None:
    """Clean up an idle video streamer — unregister from hub and registry."""
    try:
        video_cog._backend.ws_hub.unregister_streamer(guild_id)
    except Exception:
        pass
    try:
        video_cog._registry.unregister(guild_id, streamer.channel_id)
    except Exception:
        pass
