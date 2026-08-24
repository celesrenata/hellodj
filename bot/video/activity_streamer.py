"""HelloDJ — Activity session manager: per-guild video streaming lifecycle.

Orchestrates the full lifecycle of a Discord Activity video session for a
single guild. Manages state transitions, HLS pipeline control, queue
management, elapsed time tracking, and background tasks for auto-advance
and max session duration enforcement.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from video import StreamState, VideoSource
from video.hls_transcode import HLSTranscodePipeline, Resolution, _HLS_BASE_DIR

if TYPE_CHECKING:
    from video.ws_hub import WebSocketHub

log = logging.getLogger(__name__)

# Maximum session duration: 8 hours in seconds
_MAX_SESSION_DURATION_SECONDS: float = 28800.0

# Maximum queue capacity
_MAX_QUEUE_SIZE: int = 50

# Maximum history stack size (LIFO)
_MAX_HISTORY_SIZE: int = 20

# Timeout for awaiting background task cancellation
_TASK_CANCEL_TIMEOUT: float = 5.0


class QueueFullError(Exception):
    """Raised when attempting to enqueue beyond the maximum capacity."""


class TransitionDeniedError(Exception):
    """Raised when a state transition is not allowed in the current state."""


class CountdownPhase(enum.Enum):
    """Three-phase countdown state for video session startup."""

    WAITING = "waiting"
    COUNTDOWN = "countdown"
    PLAYING = "playing"


class CountdownStateMachine:
    """Finite state machine governing the countdown lifecycle.

    Transitions:
        WAITING → COUNTDOWN (start_countdown)
        COUNTDOWN → PLAYING (complete_countdown)
        COUNTDOWN → WAITING (reset, e.g. all viewers disconnect)
        PLAYING → WAITING (reset, e.g. new track)
    """

    def __init__(self, countdown_seconds: int = 3) -> None:
        self.phase: CountdownPhase = CountdownPhase.WAITING
        self.countdown_seconds: int = countdown_seconds
        self.countdown_start_mono: float = 0.0
        self._disconnect_timer: asyncio.Task | None = None

    def can_start_countdown(self) -> bool:
        """Return True if the machine is in WAITING and can begin a countdown."""
        return self.phase == CountdownPhase.WAITING

    def start_countdown(self) -> bool:
        """Transition WAITING → COUNTDOWN. Returns False if not in WAITING."""
        if self.phase != CountdownPhase.WAITING:
            return False
        self.phase = CountdownPhase.COUNTDOWN
        self.countdown_start_mono = time.monotonic()
        return True

    def complete_countdown(self) -> bool:
        """Transition COUNTDOWN → PLAYING. Returns False if not in COUNTDOWN."""
        if self.phase != CountdownPhase.COUNTDOWN:
            return False
        self.phase = CountdownPhase.PLAYING
        return True

    def reset(self) -> None:
        """Reset for a new video session (any phase → WAITING)."""
        self.phase = CountdownPhase.WAITING
        self.countdown_start_mono = 0.0

    @property
    def remaining_seconds(self) -> float:
        """Seconds remaining in the countdown, or 0.0 if not counting down."""
        if self.phase != CountdownPhase.COUNTDOWN:
            return 0.0
        elapsed = time.monotonic() - self.countdown_start_mono
        return max(0.0, self.countdown_seconds - elapsed)


class ActivityStreamer:
    """Per-guild Activity session manager.

    Replaces VideoStreamer. Manages the state machine for a single guild's
    video Activity session, including playback, queue, and cleanup.

    State machine:
        IDLE → BUFFERING → STREAMING → STOPPING → IDLE
        Any state → ERROR (on unrecoverable failure)
    """

    def __init__(
        self, guild_id: int, channel_id: int, *, ws_hub: WebSocketHub | None = None,
        on_session_end=None,
        on_session_start=None,
        guild_bitrate_limit: int = 384_000,
    ) -> None:
        self.guild_id: int = guild_id
        self.channel_id: int = channel_id
        self.state: StreamState = StreamState.IDLE
        self.session_id: str = ""
        self.source: VideoSource | None = None
        self.pipeline: HLSTranscodePipeline | None = None
        self.queue: list[VideoSource] = []
        self.history: list[VideoSource] = []
        self.start_time: float = 0.0
        self.max_queue_size: int = _MAX_QUEUE_SIZE
        self._ws_hub: WebSocketHub | None = ws_hub
        self._on_session_end = on_session_end
        self._on_session_start = on_session_start
        # Guild's max audio bitrate in kbps (derived from Discord boost tier)
        self._audio_bitrate_kbps: int = guild_bitrate_limit // 1000

        # Background tasks
        self._advance_task: asyncio.Task[None] | None = None
        self._duration_task: asyncio.Task[None] | None = None

        # Lock guarding all source-change operations
        self._transition_lock: asyncio.Lock = asyncio.Lock()

        # Countdown state machine (replaces boolean flags)
        self._csm = CountdownStateMachine(countdown_seconds=3)

    # ------------------------------------------------------------------
    # Backward-compatible property accessors (bridge old API to CSM)
    # ------------------------------------------------------------------

    @property
    def waiting_for_viewer(self) -> bool:
        """True when in WAITING phase (waiting for first viewer)."""
        return self._csm.phase == CountdownPhase.WAITING

    @waiting_for_viewer.setter
    def waiting_for_viewer(self, value: bool) -> None:
        """Legacy setter — ignored; state managed by CSM."""
        pass

    @property
    def countdown_active(self) -> bool:
        """True when in COUNTDOWN phase."""
        return self._csm.phase == CountdownPhase.COUNTDOWN

    @countdown_active.setter
    def countdown_active(self, value: bool) -> None:
        """Legacy setter — ignored; state managed by CSM."""
        pass

    @property
    def playback_started(self) -> bool:
        """True when in PLAYING phase."""
        return self._csm.phase == CountdownPhase.PLAYING

    @playback_started.setter
    def playback_started(self, value: bool) -> None:
        """Legacy setter — ignored; state managed by CSM."""
        pass

    @property
    def countdown_seconds(self) -> int:
        """Return the countdown duration in seconds."""
        return self._csm.countdown_seconds

    @countdown_seconds.setter
    def countdown_seconds(self, value: int) -> None:
        """Set the countdown duration."""
        self._csm.countdown_seconds = value

    @property
    def is_active(self) -> bool:
        """Return True if the session is in an active (non-idle/error) state."""
        return self.state not in (StreamState.IDLE, StreamState.ERROR)

    async def play(self, source: VideoSource) -> None:
        """Start playback or enqueue if a session is already active.

        If the streamer is IDLE or in ERROR state, starts a fresh session
        with the given source. Otherwise, enqueues the source for later
        playback.

        Args:
            source: Resolved video source to play.

        Raises:
            QueueFullError: If the session is active and the queue is full.
        """
        if self.state in (StreamState.IDLE, StreamState.ERROR):
            # New session from IDLE — initialize empty whiteboard stroke registry
            if self.state == StreamState.IDLE and self._ws_hub is not None:
                self._ws_hub.init_stroke_registry(self.guild_id)
            await self._play_source(source)
        else:
            self.enqueue(source)

    async def stop(self) -> None:
        """Stop the current session and clean up all resources.

        Transitions through STOPPING to IDLE, kills the pipeline, clears
        the queue, cancels background tasks, and removes HLS files.
        """
        async with self._transition_lock:
            if self.state == StreamState.IDLE:
                return

            self.state = StreamState.STOPPING
            log.info(
                "Stopping Activity session for guild=%d session=%s",
                self.guild_id,
                self.session_id,
            )

            # Cancel background tasks (awaited)
            await self._cancel_background_tasks()

            # Kill pipeline
            if self.pipeline is not None:
                try:
                    await self.pipeline.stop()
                except Exception:
                    log.exception(
                        "Error stopping HLS pipeline for guild=%d", self.guild_id
                    )
                self.pipeline = None

            # Clean up HLS files
            await self.cleanup()

            # Clear whiteboard state and notify remaining clients
            if self._ws_hub is not None:
                self._ws_hub.clear_stroke_registry(self.guild_id)
                await self._ws_hub.broadcast_from_bot(self.guild_id, {
                    "type": "whiteboard_clear",
                    "timestamp": time.time(),
                })

            # Clear state
            self.queue.clear()
            self.source = None
            self.start_time = 0.0
            self.state = StreamState.IDLE

            # Reset countdown state machine
            self._csm.reset()

            log.info("Activity session stopped for guild=%d", self.guild_id)

    async def skip(self) -> None:
        """Skip the current video.

        Acquires the transition lock, validates state, pushes current source
        to history, then plays next in queue or stops.

        Raises:
            TransitionDeniedError: If state is not STREAMING or BUFFERING.
        """
        async with self._transition_lock:
            if self.state not in (StreamState.STREAMING, StreamState.BUFFERING):
                raise TransitionDeniedError(
                    f"Cannot skip in current state: {self.state.value}"
                )

            # Push current source to history (LIFO, max 20)
            if self.source is not None:
                self._push_history(self.source)

            if self.queue:
                next_source = self.queue.pop(0)
                log.info(
                    "Skipping to next in queue for guild=%d: %s",
                    self.guild_id,
                    next_source.title,
                )

                # Cancel current background tasks before starting new source
                await self._cancel_background_tasks()

                # Stop current pipeline
                if self.pipeline is not None:
                    try:
                        await self.pipeline.stop()
                    except Exception:
                        log.exception(
                            "Error stopping pipeline during skip for guild=%d",
                            self.guild_id,
                        )
                    self.pipeline = None

                # Clean up current session's HLS files
                await self.cleanup()

                # Reset state for new playback
                self.state = StreamState.IDLE

                # Play next source — propagate errors to caller
                try:
                    await self._play_source(next_source)
                except Exception:
                    log.exception(
                        "Failed to play next source after skip for guild=%d",
                        self.guild_id,
                    )
                    # If play failed and queue has more items, we leave in ERROR
                    # state — the caller (cog) can decide to retry or stop
                    raise
            else:
                # Queue empty — stop the session (inline, lock already held)
                await self._stop_internal()

    async def previous(self) -> bool:
        """Go back to the last played video from history.

        Acquires the transition lock, validates state, pops from history,
        pushes current to front of queue, then plays the history item.

        Returns:
            True if successfully went back, False if history is empty.

        Raises:
            TransitionDeniedError: If state is not STREAMING or BUFFERING.
        """
        async with self._transition_lock:
            if self.state not in (StreamState.STREAMING, StreamState.BUFFERING):
                raise TransitionDeniedError(
                    f"Cannot go back in current state: {self.state.value}"
                )

            if not self.history:
                return False

            # Find a replayable history entry (skip cleaned-up temp sources)
            prev_source: VideoSource | None = None
            while self.history:
                candidate = self.history.pop()
                if candidate.cleanup_on_finish and not Path(candidate.file_path).exists():
                    log.info(
                        "Skipping unavailable history entry for guild=%d: %s "
                        "(temp file cleaned up)",
                        self.guild_id,
                        candidate.title,
                    )
                    continue
                prev_source = candidate
                break

            if prev_source is None:
                # All history entries were unavailable
                return False

            # Push current source to front of queue
            if self.source is not None:
                self.queue.insert(0, self.source)

            log.info(
                "Going back to previous for guild=%d: %s",
                self.guild_id,
                prev_source.title,
            )

            # Cancel current background tasks
            await self._cancel_background_tasks()

            # Stop current pipeline
            if self.pipeline is not None:
                try:
                    await self.pipeline.stop()
                except Exception:
                    log.exception(
                        "Error stopping pipeline during previous for guild=%d",
                        self.guild_id,
                    )
                self.pipeline = None

            # Clean up current session's HLS files
            await self.cleanup()

            # Reset state for new playback
            self.state = StreamState.IDLE

            # Play the history item — propagate errors
            try:
                await self._play_source(prev_source)
            except Exception:
                log.exception(
                    "Failed to play previous source for guild=%d",
                    self.guild_id,
                )
                raise

            return True

    def enqueue(self, source: VideoSource) -> int:
        """Add a video source to the queue.

        Args:
            source: Video source to enqueue.

        Returns:
            New queue length after insertion.

        Raises:
            QueueFullError: If the queue is at maximum capacity (50).
        """
        if len(self.queue) >= self.max_queue_size:
            raise QueueFullError(
                f"Queue is full ({self.max_queue_size} items). "
                "Remove a video or wait for one to finish."
            )
        self.queue.append(source)
        log.debug(
            "Enqueued '%s' for guild=%d (queue length: %d)",
            source.title,
            self.guild_id,
            len(self.queue),
        )
        return len(self.queue)

    def get_elapsed_seconds(self) -> float:
        """Return elapsed playback time clamped to [0, duration].

        Returns 0.0 if not currently streaming, or if playback hasn't
        started yet (waiting for first viewer / countdown in progress).
        """
        if self.state != StreamState.STREAMING or self.start_time == 0.0:
            return 0.0

        # If we're waiting for the first viewer or countdown hasn't completed,
        # the video hasn't actually started playing — report 0.
        if self._csm.phase != CountdownPhase.PLAYING:
            return 0.0

        elapsed = time.monotonic() - self.start_time

        # Clamp to [0, duration] if duration is known
        if elapsed < 0.0:
            return 0.0

        if (
            self.source is not None
            and self.source.duration_seconds > 0
            and elapsed > self.source.duration_seconds
        ):
            return self.source.duration_seconds

        return elapsed

    def on_seek(self, position: float) -> None:
        """Adjust the server timer to match a client seek.

        Resets start_time so that get_elapsed_seconds() returns the new
        position. Called by ws_hub when a client sends a seek message.
        """
        if self._csm.phase != CountdownPhase.PLAYING or self.start_time == 0.0:
            return

        # Clamp position to [0, duration]
        if position < 0.0:
            position = 0.0
        if (
            self.source is not None
            and self.source.duration_seconds > 0
            and position > self.source.duration_seconds
        ):
            position = self.source.duration_seconds

        self.start_time = time.monotonic() - position
        log.debug(
            "Seek received for guild=%d — timer adjusted to %.1fs",
            self.guild_id,
            position,
        )

    # ------------------------------------------------------------------
    # Countdown protocol
    # ------------------------------------------------------------------

    def start_countdown(self) -> None:
        """Begin the countdown sequence.

        Delegates to the CountdownStateMachine. The WebSocketHub
        is responsible for broadcasting the countdown message to clients.
        """
        if not self._csm.can_start_countdown():
            return
        self._csm.start_countdown()
        log.info(
            "Countdown started for guild=%d (seconds=%d)",
            self.guild_id,
            self._csm.countdown_seconds,
        )

    def on_ready_received(self) -> bool:
        """Handle a client's `ready` message after countdown completes.

        Returns True if this is the first ready (triggers start broadcast),
        False if playback already started or countdown not active.
        """
        if self._csm.phase != CountdownPhase.COUNTDOWN:
            # Not in countdown phase — ignore stale/spurious ready
            return False

        # Transition COUNTDOWN → PLAYING
        if not self._csm.complete_countdown():
            return False

        # Mark position 0: reset start_time to now
        self.start_time = time.monotonic()
        log.info(
            "Playback started (ready received) for guild=%d — position reset to 0",
            self.guild_id,
        )
        return True

    def cancel_countdown(self) -> None:
        """Cancel an active countdown (e.g., all viewers disconnected).

        Resets countdown state machine back to WAITING without starting playback.
        """
        if self._csm.phase != CountdownPhase.COUNTDOWN:
            return
        self._csm.reset()
        log.info(
            "Countdown cancelled for guild=%d (all viewers disconnected)",
            self.guild_id,
        )

    def get_countdown_remaining(self) -> float:
        """Return remaining countdown seconds, or 0 if not active."""
        return self._csm.remaining_seconds

    def should_countdown(self) -> bool:
        """Return True if a countdown should be triggered for a new viewer.

        Conditions: in BUFFERING/STREAMING, CSM is in WAITING phase (can
        start countdown), and playback not yet started.
        """
        if self.state not in (StreamState.BUFFERING, StreamState.STREAMING):
            return False
        return self._csm.can_start_countdown()

    async def cleanup(self) -> None:
        """Delete all HLS files for the current session.

        Removes the session output directory and all its contents.
        """
        if not self.session_id:
            return

        session_dir = _HLS_BASE_DIR / str(self.guild_id) / self.session_id
        if session_dir.exists():
            try:
                shutil.rmtree(session_dir)
                log.info(
                    "Cleaned up HLS directory: %s",
                    session_dir,
                )
            except OSError as exc:
                log.warning(
                    "Failed to clean up HLS directory %s: %s",
                    session_dir,
                    exc,
                )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _push_history(self, source: VideoSource) -> None:
        """Push a source onto the history stack, enforcing max size."""
        self.history.append(source)
        # Trim oldest entries if over max
        while len(self.history) > _MAX_HISTORY_SIZE:
            self.history.pop(0)

    async def _await_segment_zero(self, hls_dir: Path, timeout: float = 10.0) -> float:
        """Poll for segment 0 in the HLS output directory.

        Checks every 200ms for ``stream0.ts`` with size > 0.  Returns 0.0
        when found, or a fallback anchor offset computed from the lowest
        available segment if the timeout expires.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            playlist = hls_dir / "stream.m3u8"
            if playlist.exists():
                seg0 = hls_dir / "stream0.ts"
                if seg0.exists() and seg0.stat().st_size > 0:
                    return 0.0
            await asyncio.sleep(0.2)
        # Fallback: find lowest segment, compute offset
        return self._find_lowest_segment_offset(hls_dir)

    def _find_lowest_segment_offset(self, hls_dir: Path) -> float:
        """Parse .m3u8 and compute offset from lowest available segment.

        Returns ``min_index * segment_duration``, or 0.0 if the playlist
        is missing or unparseable.
        """
        playlist = hls_dir / "stream.m3u8"
        if not playlist.exists():
            log.error("Segment-zero timeout: no playlist found in %s", hls_dir)
            return 0.0

        segment_duration = 0.0
        min_index = float("inf")

        try:
            content = playlist.read_text()
            for line in content.splitlines():
                if line.startswith("#EXTINF:"):
                    # Extract duration from EXTINF tag (all segments same duration)
                    try:
                        segment_duration = float(line.split(":")[1].rstrip(","))
                    except (ValueError, IndexError):
                        pass
                elif line.startswith("stream") and line.endswith(".ts"):
                    # Extract segment index from filename like "stream0.ts"
                    try:
                        idx = int(line.replace("stream", "").replace(".ts", ""))
                        min_index = min(min_index, idx)
                    except ValueError:
                        pass
        except OSError as exc:
            log.error("Failed to parse playlist %s: %s", playlist, exc)
            return 0.0

        if min_index == float("inf") or segment_duration <= 0:
            log.error(
                "Segment-zero timeout: could not determine offset from %s",
                playlist,
            )
            return 0.0

        offset = min_index * segment_duration
        log.warning(
            "Segment-zero timeout: using fallback offset %.1fs "
            "(segment %d × %.1fs) from %s",
            offset,
            min_index,
            segment_duration,
            hls_dir,
        )
        return offset

    async def _stop_internal(self) -> None:
        """Stop session internals without acquiring the lock (caller holds it)."""
        self.state = StreamState.STOPPING
        log.info(
            "Stopping Activity session for guild=%d session=%s",
            self.guild_id,
            self.session_id,
        )

        await self._cancel_background_tasks()

        # Stop Lavalink audio playback for this video
        await self._stop_lavalink_audio()

        if self.pipeline is not None:
            try:
                await self.pipeline.stop()
            except Exception:
                log.exception(
                    "Error stopping HLS pipeline for guild=%d", self.guild_id
                )
            self.pipeline = None

        await self.cleanup()

        # Notify clients that session has ended
        if self._ws_hub is not None:
            await self._ws_hub.broadcast_from_bot(self.guild_id, {
                "type": "session_end",
            })

        # Clear whiteboard state and notify remaining clients
        if self._ws_hub is not None:
            self._ws_hub.clear_stroke_registry(self.guild_id)
            await self._ws_hub.broadcast_from_bot(self.guild_id, {
                "type": "whiteboard_clear",
                "timestamp": time.time(),
            })

        self.queue.clear()
        self.source = None
        self.start_time = 0.0
        self.state = StreamState.IDLE

        # Reset countdown state machine
        self._csm.reset()

        log.info("Activity session stopped for guild=%d", self.guild_id)

    async def _play_source(self, source: VideoSource) -> None:
        """Start a new playback session for the given source.

        Generates a new session ID, creates the HLS pipeline, starts
        transcoding, waits for readiness, then transitions to STREAMING.

        Args:
            source: Resolved video source to begin playing.

        Raises:
            TransitionDeniedError: If state is not IDLE, ERROR, or BUFFERING
                (catches overlapping play attempts).
            RuntimeError: If the pipeline fails to start or become ready.
        """
        # State assertion: refuse to start if already streaming/stopping
        if self.state not in (StreamState.IDLE, StreamState.ERROR, StreamState.BUFFERING):
            raise TransitionDeniedError(
                f"Cannot start playback in state: {self.state.value}. "
                "Another source may already be playing."
            )

        # Generate new session
        self.session_id = str(uuid.uuid4())
        self.source = source
        self.state = StreamState.BUFFERING

        log.info(
            "Starting Activity session for guild=%d session=%s title='%s'",
            self.guild_id,
            self.session_id,
            source.title,
        )

        # Determine source codec from metadata (default to h264)
        source_codec = source.metadata.get("codec", "h264")
        source_fps = source.metadata.get("fps", 30.0)

        # Create pipeline
        self.pipeline = HLSTranscodePipeline(
            guild_id=self.guild_id,
            session_id=self.session_id,
            source_codec=source_codec,
            source_fps=source_fps,
            audio_bitrate_kbps=self._audio_bitrate_kbps,
        )

        try:
            if source.stream_url:
                # Stream directly from URL — throttled, no pre-download
                await self.pipeline.start_streaming(
                    source.stream_url, Resolution.RES_720P,
                    audio_url=source.audio_url,
                )
            else:
                await self.pipeline.start(source.file_path, Resolution.RES_720P)
        except Exception as exc:
            log.error(
                "Failed to start HLS pipeline for guild=%d: %s",
                self.guild_id,
                exc,
            )
            self.state = StreamState.ERROR
            self.pipeline = None
            raise RuntimeError(f"Pipeline start failed: {exc}") from exc

        # Wait for first segment (readiness)
        ready = await self.pipeline.wait_ready(timeout=30.0)
        if not ready:
            log.error(
                "HLS pipeline did not become ready within 30s for guild=%d session=%s",
                self.guild_id,
                self.session_id,
            )
            self.state = StreamState.ERROR
            await self.pipeline.stop()
            self.pipeline = None
            raise RuntimeError(
                "Pipeline did not become ready within 30s"
            )

        # Transition to STREAMING
        self.state = StreamState.STREAMING
        self.start_time = time.monotonic()

        # Enter WAITING phase: countdown hasn't fired yet (reset CSM for new track)
        self._csm.reset()

        # ── Lavalink audio routing ────────────────────────────────────────
        # Play the audio through Lavalink so filters (nightcore, vaporwave, etc.)
        # apply. The Activity frontend mutes its HLS audio and listens via
        # Discord voice channel instead.
        await self._start_lavalink_audio(source)

        log.info(
            "Activity now streaming for guild=%d session=%s title='%s'",
            self.guild_id,
            self.session_id,
            source.title,
        )

        # Notify on_session_start callback (e.g., VisualizerRegistry)
        if self._on_session_start:
            try:
                await self._on_session_start(self.guild_id)
            except Exception as exc:
                log.warning("on_session_start callback failed: %s", exc)

        # Start background tasks
        self._advance_task = asyncio.create_task(
            self._auto_advance(),
            name=f"activity-advance-{self.guild_id}",
        )
        self._duration_task = asyncio.create_task(
            self._max_duration_timer(),
            name=f"activity-duration-{self.guild_id}",
        )

    async def _start_lavalink_audio(self, source: VideoSource) -> None:
        """Play the video's audio through Lavalink for filter support.

        Routes the audio from the video source URL through the guild's
        wavelink player so Lavalink filters (nightcore, vaporwave, 8D, etc.)
        apply. The Activity frontend mutes its own HLS audio; users hear
        filtered audio through the Discord voice channel.

        Falls back gracefully — if Lavalink can't play the audio URL, the
        HLS audio in the Activity remains available as an unmuted fallback.
        """
        try:
            import player
            from wavelink import Playable, TrackSource

            player_obj = player.get_player(self.guild_id)
            if not player_obj or not player_obj.connected:
                log.info(
                    "Lavalink audio routing skipped — no connected player "
                    "for guild=%d",
                    self.guild_id,
                )
                return

            # Determine audio source URL:
            # 1. Separate audio_url (DASH/YouTube sources)
            # 2. stream_url (combined video+audio — Lavalink extracts audio)
            # 3. file_path (local file — less common)
            audio_source = source.audio_url or source.stream_url or source.file_path
            if not audio_source:
                log.info(
                    "Lavalink audio routing skipped — no audio URL available "
                    "for guild=%d title=%r",
                    self.guild_id,
                    source.title,
                )
                return

            # Search/load the audio through Lavalink
            tracks = await Playable.search(audio_source)
            if not tracks:
                log.warning(
                    "Lavalink could not load audio URL for video: guild=%d url=%s",
                    self.guild_id,
                    audio_source[:100],
                )
                return

            track = tracks[0] if isinstance(tracks, list) else tracks

            # Inject video metadata into the track for display purposes
            try:
                object.__setattr__(track, "_title", source.title)
            except Exception:
                pass

            # Play through Lavalink — this sends audio to Discord voice channel
            await player_obj.play(track)

            # Mark the player state so the unified queue knows video audio is active
            state = player.get_state(self.guild_id)
            state["current"] = player._track_entry(track, "video")
            state["current"]["type"] = "music_video"
            state["current"]["title"] = source.title
            if source.duration_seconds:
                state["current"]["duration"] = int(source.duration_seconds * 1000)

            log.info(
                "Lavalink audio routed for video: guild=%d title=%r",
                self.guild_id,
                source.title,
            )

            # Broadcast lavalink_audio_active to Activity clients so they mute HLS audio
            if self._ws_hub:
                await self._ws_hub.broadcast_from_bot(self.guild_id, {
                    "type": "lavalink_audio",
                    "active": True,
                    "timescale": 1.0,
                })

        except Exception as exc:
            # Non-fatal: video still works with HLS audio if Lavalink fails
            log.warning(
                "Lavalink audio routing failed for guild=%d: %s — "
                "HLS audio will remain active in Activity",
                self.guild_id,
                exc,
            )

    async def _stop_lavalink_audio(self) -> None:
        """Stop Lavalink audio playback when video session ends."""
        try:
            import player
            player_obj = player.get_player(self.guild_id)
            if player_obj and player_obj.connected and player_obj.playing:
                await player_obj.stop()
                log.info("Stopped Lavalink audio for video: guild=%d", self.guild_id)
        except Exception as exc:
            log.debug("_stop_lavalink_audio failed: %s", exc)

    async def _auto_advance(self) -> None:
        """Wait for pipeline completion and advance to the next queue item.

        Runs as a background task. When the current transcode finishes,
        either plays the next queued source or stops the session.
        """
        if self.pipeline is None:
            return

        # Capture the pipeline reference to detect stale firing after skip
        completed_pipeline = self.pipeline

        try:
            await self.pipeline.wait_complete()
        except asyncio.CancelledError:
            return

        log.info(
            "Playback complete for guild=%d session=%s title='%s'",
            self.guild_id,
            self.session_id,
            self.source.title if self.source else "unknown",
        )

        async with self._transition_lock:
            # Guard against stale task: if pipeline was replaced by a skip,
            # this advance task is stale — bail out
            if self.pipeline is not completed_pipeline:
                log.debug(
                    "Auto-advance detected stale pipeline for guild=%d — "
                    "another source already started, bailing out",
                    self.guild_id,
                )
                return

            # Re-check state: must still be STREAMING
            if self.state != StreamState.STREAMING:
                log.debug(
                    "Auto-advance skipped for guild=%d — state is %s, not STREAMING",
                    self.guild_id,
                    self.state.value,
                )
                return

            # Push current to history
            if self.source is not None:
                self._push_history(self.source)

            # Auto-advance: play next in queue or stop
            if self.queue:
                next_source = self.queue.pop(0)
                log.info(
                    "Auto-advancing to next in queue for guild=%d: %s",
                    self.guild_id,
                    next_source.title,
                )

                # Stop current pipeline and clean up before next
                self.pipeline = None
                await self.cleanup()

                # Cancel duration timer (will be re-created for new source)
                if self._duration_task is not None and not self._duration_task.done():
                    self._duration_task.cancel()
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(self._duration_task),
                            timeout=_TASK_CANCEL_TIMEOUT,
                        )
                    except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                        pass
                    self._duration_task = None

                # Reset state for new playback
                self.state = StreamState.IDLE

                try:
                    await self._play_source(next_source)
                except Exception:
                    log.exception(
                        "Failed to auto-advance to next source for guild=%d",
                        self.guild_id,
                    )
                    # Try next item in queue if available
                    while self.queue:
                        fallback = self.queue.pop(0)
                        try:
                            self.state = StreamState.IDLE
                            await self._play_source(fallback)
                            return
                        except Exception:
                            log.exception(
                                "Fallback source also failed for guild=%d: %s",
                                self.guild_id,
                                fallback.title,
                            )
                    # All failed — stop
                    await self._stop_internal()
            else:
                # Transcode finished but viewer may still be watching.
                # Wait for the remaining playback duration before stopping,
                # so HLS files stay available for the viewer to finish.
                # NOTE: Don't calculate remaining from wall time — clients can
                # seek backward, making wall_elapsed > actual viewer position.
                # Instead, keep the session alive for the full video duration
                # from when the transcode finishes. The client sends a status
                # check when video ends (DOM 'ended' event) which triggers
                # session transition naturally.
                remaining = 0.0
                if self.source and self.source.duration_seconds > 0:
                    # Give the viewer the full video duration from transcode
                    # completion — they may have seeked back at any point.
                    remaining = self.source.duration_seconds

                # Release lock during sleep so other operations can proceed
                # (we re-acquire after sleep to check state)

        # Sleep outside the lock if we need to wait for viewer
        if not self.queue and self.state == StreamState.STREAMING:
            remaining = 0.0
            if self.source and self.source.duration_seconds > 0:
                # Keep session alive for the full video duration after transcode
                # completes. Clients can seek freely; the session only ends when
                # the client reports video ended OR this timer expires.
                remaining = self.source.duration_seconds
            if remaining > 0:
                log.info(
                    "Transcode done for guild=%d — keeping session alive %.0fs "
                    "for viewer to finish",
                    self.guild_id,
                    remaining,
                )
                try:
                    await asyncio.sleep(remaining + 10.0)  # +10s buffer
                except asyncio.CancelledError:
                    return

            log.info(
                "Queue empty after playback for guild=%d — stopping session",
                self.guild_id,
            )
            await self.stop()

            # Notify the unified queue that the video session ended
            if self._on_session_end:
                try:
                    await self._on_session_end(self.guild_id)
                except Exception as exc:
                    log.warning("on_session_end callback failed: %s", exc)

    async def _max_duration_timer(self) -> None:
        """Enforce the maximum session duration of 8 hours.

        Runs as a background task. Automatically stops the session once
        the time limit is reached.
        """
        try:
            await asyncio.sleep(_MAX_SESSION_DURATION_SECONDS)
        except asyncio.CancelledError:
            return

        log.warning(
            "Maximum session duration (%.0f hours) reached for guild=%d — auto-stopping",
            _MAX_SESSION_DURATION_SECONDS / 3600,
            self.guild_id,
        )
        await self.stop()

    async def _cancel_background_tasks(self) -> None:
        """Cancel all running background tasks and await their completion.

        Awaits cancellation with a timeout to ensure tasks have actually
        stopped before proceeding with the next operation.
        """
        tasks_to_cancel: list[asyncio.Task[None]] = []
        for task in (self._advance_task, self._duration_task):
            if task is not None and not task.done():
                task.cancel()
                tasks_to_cancel.append(task)

        # Await cancellation with timeout
        for task in tasks_to_cancel:
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=_TASK_CANCEL_TIMEOUT
                )
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

        self._advance_task = None
        self._duration_task = None
# bust 1787382769
