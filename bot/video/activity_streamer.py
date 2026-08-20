"""HelloDJ — Activity session manager: per-guild video streaming lifecycle.

Orchestrates the full lifecycle of a Discord Activity video session for a
single guild. Manages state transitions, HLS pipeline control, queue
management, elapsed time tracking, and background tasks for auto-advance
and max session duration enforcement.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from pathlib import Path

from video import StreamState, VideoSource
from video.hls_transcode import HLSTranscodePipeline, Resolution, _HLS_BASE_DIR

log = logging.getLogger(__name__)

# Maximum session duration: 8 hours in seconds
_MAX_SESSION_DURATION_SECONDS: float = 28800.0

# Maximum queue capacity
_MAX_QUEUE_SIZE: int = 50


class QueueFullError(Exception):
    """Raised when attempting to enqueue beyond the maximum capacity."""


class ActivityStreamer:
    """Per-guild Activity session manager.

    Replaces VideoStreamer. Manages the state machine for a single guild's
    video Activity session, including playback, queue, and cleanup.

    State machine:
        IDLE → BUFFERING → STREAMING → STOPPING → IDLE
        Any state → ERROR (on unrecoverable failure)
    """

    def __init__(self, guild_id: int, channel_id: int) -> None:
        self.guild_id: int = guild_id
        self.channel_id: int = channel_id
        self.state: StreamState = StreamState.IDLE
        self.session_id: str = ""
        self.source: VideoSource | None = None
        self.pipeline: HLSTranscodePipeline | None = None
        self.queue: list[VideoSource] = []
        self.start_time: float = 0.0
        self.max_queue_size: int = _MAX_QUEUE_SIZE

        # Background tasks
        self._advance_task: asyncio.Task[None] | None = None
        self._duration_task: asyncio.Task[None] | None = None

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
            await self._play_source(source)
        else:
            self.enqueue(source)

    async def stop(self) -> None:
        """Stop the current session and clean up all resources.

        Transitions through STOPPING to IDLE, kills the pipeline, clears
        the queue, cancels background tasks, and removes HLS files.
        """
        if self.state == StreamState.IDLE:
            return

        self.state = StreamState.STOPPING
        log.info(
            "Stopping Activity session for guild=%d session=%s",
            self.guild_id,
            self.session_id,
        )

        # Cancel background tasks
        self._cancel_background_tasks()

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

        # Clear state
        self.queue.clear()
        self.source = None
        self.start_time = 0.0
        self.state = StreamState.IDLE

        log.info("Activity session stopped for guild=%d", self.guild_id)

    async def skip(self) -> None:
        """Skip the current video.

        If the queue has items, plays the next one. Otherwise stops the
        session entirely.
        """
        if self.queue:
            next_source = self.queue.pop(0)
            log.info(
                "Skipping to next in queue for guild=%d: %s",
                self.guild_id,
                next_source.title,
            )
            # Cancel current background tasks before starting new source
            self._cancel_background_tasks()

            # Stop current pipeline without full cleanup (we'll reuse session dir logic)
            if self.pipeline is not None:
                try:
                    await self.pipeline.stop()
                except Exception:
                    log.exception(
                        "Error stopping pipeline during skip for guild=%d",
                        self.guild_id,
                    )
                self.pipeline = None

            # Clean up current session's HLS files before starting new one
            await self.cleanup()

            # Play next source (starts a fresh sub-session)
            await self._play_source(next_source)
        else:
            await self.stop()

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

        Returns 0.0 if not currently streaming.
        """
        if self.state != StreamState.STREAMING or self.start_time == 0.0:
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

    async def _play_source(self, source: VideoSource) -> None:
        """Start a new playback session for the given source.

        Generates a new session ID, creates the HLS pipeline, starts
        transcoding, waits for readiness, then transitions to STREAMING.

        Args:
            source: Resolved video source to begin playing.
        """
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
        )

        try:
            await self.pipeline.start(source.file_path, Resolution.RES_720P)
        except Exception as exc:
            log.error(
                "Failed to start HLS pipeline for guild=%d: %s",
                self.guild_id,
                exc,
            )
            self.state = StreamState.ERROR
            self.pipeline = None
            return

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
            return

        # Transition to STREAMING
        self.state = StreamState.STREAMING
        self.start_time = time.monotonic()

        log.info(
            "Activity now streaming for guild=%d session=%s title='%s'",
            self.guild_id,
            self.session_id,
            source.title,
        )

        # Start background tasks
        self._advance_task = asyncio.create_task(
            self._auto_advance(),
            name=f"activity-advance-{self.guild_id}",
        )
        self._duration_task = asyncio.create_task(
            self._max_duration_timer(),
            name=f"activity-duration-{self.guild_id}",
        )

    async def _auto_advance(self) -> None:
        """Wait for pipeline completion and advance to the next queue item.

        Runs as a background task. When the current transcode finishes,
        either plays the next queued source or stops the session.
        """
        if self.pipeline is None:
            return

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

        # Auto-advance: play next in queue or stop
        if self.queue:
            next_source = self.queue.pop(0)
            log.info(
                "Auto-advancing to next in queue for guild=%d: %s",
                self.guild_id,
                next_source.title,
            )

            # Stop current pipeline and clean up before next
            if self.pipeline is not None:
                self.pipeline = None
            await self.cleanup()

            # Cancel duration timer (will be re-created for new source)
            if self._duration_task is not None and not self._duration_task.done():
                self._duration_task.cancel()
                try:
                    await self._duration_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._duration_task = None

            await self._play_source(next_source)
        else:
            log.info(
                "Queue empty after playback for guild=%d — stopping session",
                self.guild_id,
            )
            await self.stop()

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

    def _cancel_background_tasks(self) -> None:
        """Cancel all running background tasks."""
        for task in (self._advance_task, self._duration_task):
            if task is not None and not task.done():
                task.cancel()
        self._advance_task = None
        self._duration_task = None
