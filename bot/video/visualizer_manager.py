"""HelloDJ — Per-guild visualizer state machine and rendering lifecycle.

Manages visualizer state transitions, engine lifecycle, and viewer-driven
demand rendering. Each guild gets one VisualizerManager instance that
coordinates between WebSocket viewer events, video session lifecycle,
and the configured visualizer engine.

Audio Independence (Req 8): This module MUST NOT import from player.py or
share any mutable state with the Lavalink audio playback pipeline.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from video.audio_feature_bus import AudioFeatureBus
from video.hls_transcode import HLSTranscodePipeline
from video.visualizer_engines import VisualizerRenderer, create_engine
from video.visualizer_engines.base import TrackMetadata

if TYPE_CHECKING:
    from video.ws_hub import WebSocketHub

import guild_settings

log = logging.getLogger(__name__)


class VisualizerState(enum.Enum):
    """Runtime states for the per-guild visualizer state machine.

    Transitions:
        DISABLED → IDLE_NO_VIEWERS: video ends OR engine set (when not "off")
        IDLE_NO_VIEWERS → STARTING: viewer joins + audio playing
        STARTING → ACTIVE: engine init completes
        ACTIVE → SUSPENDING: last viewer disconnects
        SUSPENDING → IDLE_NO_VIEWERS: debounce expires with 0 viewers
        SUSPENDING → ACTIVE: viewer rejoins during debounce
        ANY → DISABLED: video starts OR engine set to "off"
        ANY → ERROR: unrecoverable error during rendering
    """

    DISABLED = "disabled"
    IDLE_NO_VIEWERS = "idle_no_viewers"
    STARTING = "starting"
    ACTIVE = "active"
    SUSPENDING = "suspending"
    ERROR = "error"


class VisualizerManager:
    """Per-guild visualizer state machine and rendering lifecycle.

    Coordinates viewer-driven demand rendering: zero resources when no viewers
    are connected, instant activation when a viewer appears.

    Args:
        guild_id: The guild this manager controls.
        ws_hub: The WebSocket hub for broadcasting visualizer messages.
        bot_avatar_url: The bot's Discord avatar URL (used by DVD engine).
    """

    # Engines eligible for "random" mode selection. Only includes engines that
    # are fully implemented (not stubs). Expand this list as new engines become
    # production-ready.
    _RANDOM_POOL_ENGINES: list[str] = ["native"]

    def __init__(
        self,
        guild_id: int,
        ws_hub: WebSocketHub,
        bot_avatar_url: str = "https://cdn.discordapp.com/embed/avatars/0.png",
    ) -> None:
        self.guild_id = guild_id
        self.state = VisualizerState.DISABLED
        self._ws_hub = ws_hub
        self._bot_avatar_url = bot_avatar_url
        self._engine: VisualizerRenderer | None = None
        self._engine_type: str = guild_settings.get_visualizer_engine(guild_id)
        self._suspend_task: asyncio.Task | None = None
        self._track_metadata: TrackMetadata | None = None

        # Server-rendered engine resources
        self._audio_bus: AudioFeatureBus | None = None
        self._pipeline: HLSTranscodePipeline | None = None
        self._render_task: asyncio.Task | None = None

        # Random mode cycling state
        self._random_cycle: list[str] = []
        self._random_index: int = 0

        log.debug(
            "VisualizerManager created for guild %d (engine=%s, state=%s)",
            guild_id,
            self._engine_type,
            self.state.value,
        )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def on_viewer_join(self) -> None:
        """Handle a viewer connecting to the guild's Activity.

        State transitions:
            IDLE_NO_VIEWERS → STARTING → ACTIVE (engine initialized)
            SUSPENDING → ACTIVE (cancel pending suspension)
        """
        if self.state == VisualizerState.IDLE_NO_VIEWERS:
            await self._start_engine()
        elif self.state == VisualizerState.SUSPENDING:
            await self._cancel_suspension()
        elif self.state == VisualizerState.ACTIVE:
            # Already active — additional viewer, nothing to do
            pass
        elif self.state == VisualizerState.STARTING:
            # Already starting — additional viewer, nothing to do
            pass
        else:
            # DISABLED or ERROR — no action
            log.debug(
                "Guild %d: viewer joined in state %s — no action",
                self.guild_id,
                self.state.value,
            )

    async def on_viewer_leave(self, viewer_count: int) -> None:
        """Handle a viewer disconnecting from the guild's Activity.

        Args:
            viewer_count: The number of viewers REMAINING after this disconnect.

        State transitions:
            ACTIVE → SUSPENDING → IDLE_NO_VIEWERS (when viewer_count == 0)
        """
        if viewer_count == 0 and self.state == VisualizerState.ACTIVE:
            await self._begin_suspension()
        elif viewer_count == 0 and self.state == VisualizerState.STARTING:
            # Engine was starting but all viewers left — go idle
            await self._stop_engine()
            self.state = VisualizerState.IDLE_NO_VIEWERS
            log.info(
                "Guild %d: all viewers left during STARTING — transitioning to IDLE_NO_VIEWERS",
                self.guild_id,
            )

    async def on_video_start(self) -> None:
        """Handle a video session starting for the guild.

        ANY state → DISABLED. Stop engine if running.
        """
        previous = self.state
        await self._stop_engine()
        self._cancel_suspend_task()
        self.state = VisualizerState.DISABLED
        log.info(
            "Guild %d: video started — transitioning %s → DISABLED",
            self.guild_id,
            previous.value,
        )

    async def on_video_end(self) -> None:
        """Handle a video session ending for the guild.

        If engine is not "off" → IDLE_NO_VIEWERS (ready for viewers).
        """
        if self._engine_type == "off":
            self.state = VisualizerState.DISABLED
            log.debug(
                "Guild %d: video ended but engine is 'off' — staying DISABLED",
                self.guild_id,
            )
            return

        self.state = VisualizerState.IDLE_NO_VIEWERS
        log.info(
            "Guild %d: video ended — transitioning to IDLE_NO_VIEWERS (engine=%s)",
            self.guild_id,
            self._engine_type,
        )

    async def on_track_change(self, metadata: dict) -> None:
        """Handle a track change event.

        Stores metadata regardless of state. If the engine is active and
        client-side, broadcasts updated config to viewers.

        For "random" mode: advances the random engine cycle index so the next
        activation (e.g., after suspension/reactivation) uses a different engine.
        Hot-swapping during active rendering is avoided to prevent visual gaps.

        Args:
            metadata: Dict with keys: title, artist, artwork_url, duration_ms, position_ms.
        """
        self._track_metadata = TrackMetadata(
            title=metadata.get("title", ""),
            artist=metadata.get("artist", ""),
            artwork_url=metadata.get("artwork_url"),
            duration_ms=metadata.get("duration_ms", 0),
            position_ms=metadata.get("position_ms", 0),
        )

        # For random mode: advance the cycle so the next engine selection is
        # different. This takes effect on the next suspension/reactivation cycle
        # rather than hot-swapping mid-render (which would cause a visual gap).
        if self._engine_type == "random" and self.state == VisualizerState.ACTIVE:
            # Pre-compute the next selection (advances the index internally)
            next_engine = self._select_next_random_engine()
            log.debug(
                "Guild %d: random mode track change — next engine will be '%s'",
                self.guild_id,
                next_engine,
            )

        if self._engine is not None and self.state == VisualizerState.ACTIVE:
            try:
                await self._engine.on_track_change(self._track_metadata)
            except Exception:
                log.exception(
                    "Guild %d: engine on_track_change failed", self.guild_id
                )

            # If client-side engine, broadcast updated config to viewers
            if self._engine.is_client_side:
                await self._broadcast_visualizer_state()

    async def set_engine(self, engine_type: str) -> None:
        """Change the visualizer engine type for this guild.

        Validates the engine, updates guild_settings, and transitions state:
            - "off" → DISABLED
            - Otherwise → stop current engine, update type. If already in a
              viewer-active state, restart with new engine.

        Args:
            engine_type: The new engine type (must be in VALID_VISUALIZER_ENGINES).

        Raises:
            ValueError: If engine_type is not valid.
        """
        # Validate
        if engine_type not in guild_settings.VALID_VISUALIZER_ENGINES:
            raise ValueError(
                f"Invalid visualizer engine '{engine_type}'; must be one of: "
                f"{', '.join(sorted(guild_settings.VALID_VISUALIZER_ENGINES))}"
            )

        # Persist to guild settings
        guild_settings.set_visualizer_engine(self.guild_id, engine_type)

        old_type = self._engine_type
        self._engine_type = engine_type

        log.info(
            "Guild %d: engine changed %s → %s (state=%s)",
            self.guild_id,
            old_type,
            engine_type,
            self.state.value,
        )

        if engine_type == "off":
            # ANY → DISABLED
            await self._stop_engine()
            self._cancel_suspend_task()
            self.state = VisualizerState.DISABLED
            return

        # Engine set to something valid (not "off")
        if self.state == VisualizerState.DISABLED:
            # Transition to IDLE_NO_VIEWERS — ready for viewers
            self.state = VisualizerState.IDLE_NO_VIEWERS
        elif self.state in (
            VisualizerState.ACTIVE,
            VisualizerState.STARTING,
        ):
            # Hot-swap: stop current engine, restart with new one
            await self._stop_engine()
            await self._start_engine()
        elif self.state == VisualizerState.SUSPENDING:
            # Cancel suspension, restart with new engine
            self._cancel_suspend_task()
            await self._stop_engine()
            await self._start_engine()

    async def shutdown(self) -> None:
        """Full cleanup — stop engine, cancel tasks, release all resources."""
        log.info("Guild %d: VisualizerManager shutting down", self.guild_id)
        await self._stop_engine()
        self._cancel_suspend_task()
        self.state = VisualizerState.DISABLED

    # ------------------------------------------------------------------
    # Internal state machine helpers
    # ------------------------------------------------------------------

    async def _start_engine(self) -> None:
        """Initialize and activate the configured engine.

        Transitions: → STARTING → ACTIVE on success, → ERROR on failure.

        For client-side engines (e.g., DVD): broadcasts config and transitions
        to ACTIVE immediately.

        For server-rendered engines: starts the HLS pipeline, render loop,
        and AudioFeatureBus subscription. Transitions to ACTIVE once the
        first HLS segment is ready.
        """
        self.state = VisualizerState.STARTING
        log.debug(
            "Guild %d: starting engine '%s'", self.guild_id, self._engine_type
        )

        try:
            self._engine = self._create_engine_instance()
            await self._engine.initialize(metadata=self._track_metadata)
            await self._engine.activate(metadata=self._track_metadata)
        except Exception:
            log.exception(
                "Guild %d: failed to start engine '%s'",
                self.guild_id,
                self._engine_type,
            )
            self._engine = None
            self.state = VisualizerState.ERROR
            return

        if not self._engine.is_client_side:
            # Server-rendered engine — start pipeline and render loop
            # Broadcast "starting" state while pipeline initializes
            await self._ws_hub.broadcast(self.guild_id, {
                "type": "visualizer",
                "state": "starting",
                "engine": self._engine_type,
            })
            try:
                await self._start_server_render_pipeline()
            except Exception:
                log.exception(
                    "Guild %d: failed to start server render pipeline",
                    self.guild_id,
                )
                await self._stop_server_render_resources()
                self.state = VisualizerState.ERROR
                return
        else:
            # Client-side engine — broadcast config to viewers
            self.state = VisualizerState.ACTIVE
            log.info(
                "Guild %d: engine '%s' active (client_side=True)",
                self.guild_id,
                self._engine_type,
            )
            await self._broadcast_visualizer_state()

    async def _stop_engine(self) -> None:
        """Stop the current engine and release resources.

        For server-rendered engines, also tears down the render loop,
        AudioFeatureBus subscription, and HLS pipeline.
        """
        # Clean up server-rendered resources first
        await self._stop_server_render_resources()

        if self._engine is not None:
            try:
                await self._engine.stop()
            except Exception:
                log.exception(
                    "Guild %d: error stopping engine", self.guild_id
                )
            self._engine = None

    def _create_engine_instance(self) -> VisualizerRenderer:
        """Instantiate the configured engine from the registry.

        Returns:
            A configured VisualizerRenderer instance.

        Raises:
            ValueError: If the engine type is unknown.
        """
        engine_type = self._engine_type

        # For "random" mode, select the next engine from the pool.
        if engine_type == "random":
            engine_type = self._select_next_random_engine()

        # Build kwargs based on engine type
        kwargs: dict = {}
        if engine_type == "dvd":
            kwargs["bot_avatar_url"] = self._bot_avatar_url

        return create_engine(engine_type, **kwargs)

    def _select_next_random_engine(self) -> str:
        """Select the next engine from the random pool, cycling to avoid repeats.

        Builds the cycle list on first call or when the pool changes.
        Cycles through available engines sequentially, skipping the currently
        active engine when possible to ensure variety on each track change.

        Returns:
            The engine type string to use for this activation.
        """
        pool = self._RANDOM_POOL_ENGINES

        if not pool:
            # No engines in the pool — fall back to dvd
            log.warning(
                "Guild %d: random pool is empty, falling back to dvd",
                self.guild_id,
            )
            return "dvd"

        if len(pool) == 1:
            # Only one engine available — always return it
            return pool[0]

        # Rebuild the cycle list if it doesn't match the current pool
        if self._random_cycle != pool:
            self._random_cycle = list(pool)
            self._random_index = 0

        # Select the next engine, avoiding the currently active one if possible
        current_engine = (
            self._engine_type
            if self._engine is None
            else type(self._engine).__name__.lower().replace("engine", "")
        )

        # Try up to len(pool) times to find a different engine
        for _ in range(len(self._random_cycle)):
            candidate = self._random_cycle[self._random_index]
            self._random_index = (self._random_index + 1) % len(self._random_cycle)
            if candidate != current_engine:
                return candidate

        # All engines are the same as current (shouldn't happen with len > 1)
        # Just return whatever is next in the cycle
        result = self._random_cycle[self._random_index]
        self._random_index = (self._random_index + 1) % len(self._random_cycle)
        return result

    # ------------------------------------------------------------------
    # Suspension debounce
    # ------------------------------------------------------------------

    async def _begin_suspension(self) -> None:
        """Start 2-second debounce before suspending the engine.

        For Task 5.1, this transitions immediately to IDLE_NO_VIEWERS.
        Task 5.3 will add the actual 2s asyncio timer.
        """
        self.state = VisualizerState.SUSPENDING
        log.debug("Guild %d: beginning suspension", self.guild_id)

        # Immediate transition for now — Task 5.3 adds the 2s timer
        self._suspend_task = asyncio.create_task(self._suspension_timer())

    async def _suspension_timer(self) -> None:
        """Wait 2s, then re-check viewer count before suspending.

        Task 5.3 will flesh this out with proper debounce. For now,
        transitions immediately to IDLE_NO_VIEWERS.
        """
        try:
            await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            return

        # Re-check viewer count after debounce
        viewer_count = self._ws_hub.viewer_count(self.guild_id)
        if viewer_count == 0:
            await self._execute_suspension()
        else:
            # Someone reconnected during debounce window
            self.state = VisualizerState.ACTIVE
            log.debug(
                "Guild %d: suspension cancelled — %d viewers reconnected",
                self.guild_id,
                viewer_count,
            )

    async def _execute_suspension(self) -> None:
        """Actually suspend: stop engine, clean segments, transition to IDLE_NO_VIEWERS."""
        await self._stop_engine()
        self.state = VisualizerState.IDLE_NO_VIEWERS

        # Segment cleanup within 5 seconds of suspension
        viz_dir = Path(f"/tmp/hellodj_hls/{self.guild_id}/viz")
        if viz_dir.exists():
            try:
                shutil.rmtree(viz_dir, ignore_errors=True)
                log.debug(
                    "Guild %d: cleaned up viz segments at %s",
                    self.guild_id,
                    viz_dir,
                )
            except Exception:
                log.exception(
                    "Guild %d: error cleaning viz segments", self.guild_id
                )

        log.info(
            "Guild %d: suspension complete — IDLE_NO_VIEWERS", self.guild_id
        )

    async def _cancel_suspension(self) -> None:
        """Cancel pending suspension and resume ACTIVE state."""
        self._cancel_suspend_task()
        self.state = VisualizerState.ACTIVE
        log.debug("Guild %d: suspension cancelled — back to ACTIVE", self.guild_id)

    def _cancel_suspend_task(self) -> None:
        """Cancel the suspension asyncio task if it's running."""
        if self._suspend_task is not None and not self._suspend_task.done():
            self._suspend_task.cancel()
        self._suspend_task = None

    # ------------------------------------------------------------------
    # WebSocket broadcasting
    # ------------------------------------------------------------------

    async def _broadcast_visualizer_state(self) -> None:
        """Broadcast current visualizer state to all connected viewers.

        For client-side engines, sends the engine config so the frontend
        can render locally. For server-rendered engines, sends HLS info.
        """
        if self._engine is None:
            return

        if self._engine.is_client_side:
            message = {
                "type": "visualizer",
                "state": "active",
                "engine": self._engine_type,
                "config": self._engine.client_config,
            }
        else:
            # Server-rendered engine — include HLS readiness and playlist URL
            hls_ready = self._pipeline is not None and self._pipeline.ready.is_set()
            message = {
                "type": "visualizer",
                "state": "active",
                "engine": self._engine_type,
                "hls_ready": hls_ready,
            }
            if hls_ready:
                message["playlist_url"] = (
                    f"/activity/stream/{self.guild_id}/viz/playlist.m3u8"
                )

        await self._ws_hub.broadcast(self.guild_id, message)

    # ------------------------------------------------------------------
    # Server-rendered engine lifecycle
    # ------------------------------------------------------------------

    async def _start_server_render_pipeline(self) -> None:
        """Initialize AudioFeatureBus, HLS pipeline, and render loop.

        Called when a server-rendered engine is activated. Sets up:
        1. AudioFeatureBus — subscribes the engine's audio callback
        2. HLSTranscodePipeline — ffmpeg visualizer mode (rawvideo stdin)
        3. Render loop — async task piping frames to ffmpeg stdin
        4. Ready watcher — notifies frontend when first segment appears

        Raises:
            Exception: If the pipeline fails to start.
        """
        # 1. Create and subscribe to AudioFeatureBus
        self._audio_bus = AudioFeatureBus(self.guild_id)
        if hasattr(self._engine, "on_audio_features"):
            await self._audio_bus.subscribe(self._engine.on_audio_features)
            log.debug(
                "Guild %d: subscribed engine to AudioFeatureBus", self.guild_id
            )

        # 2. Create HLS pipeline in visualizer mode
        self._pipeline = HLSTranscodePipeline(
            guild_id=self.guild_id,
            session_id="viz",  # Overridden by start_visualizer()
        )
        await self._pipeline.start_visualizer()
        log.debug(
            "Guild %d: HLS visualizer pipeline started", self.guild_id
        )

        # 3. Start the render loop task
        self._render_task = asyncio.create_task(
            self._render_loop(),
            name=f"viz-render-{self.guild_id}",
        )

        # 4. Start a task to wait for pipeline readiness and notify frontend
        asyncio.create_task(
            self._wait_for_hls_ready(),
            name=f"viz-ready-{self.guild_id}",
        )

    async def _render_loop(self) -> None:
        """Pipe engine render_frames() output to ffmpeg stdin.

        Runs until the engine is stopped, the pipeline dies, or an error
        occurs. On error, triggers the render error handler for fallback.
        """
        try:
            async for frame_data in self._engine.render_frames():
                if self._pipeline and self._pipeline.stdin_pipe:
                    self._pipeline.stdin_pipe.write(frame_data)
                    await self._pipeline.stdin_pipe.drain()
                else:
                    # Pipeline gone — stop rendering
                    break
        except (asyncio.CancelledError, BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception(
                "Guild %d: render loop error", self.guild_id
            )
            await self._handle_render_error()

    async def _wait_for_hls_ready(self) -> None:
        """Wait for the first HLS segment and notify the frontend.

        Once the pipeline's ready event is set (first .ts segment on disk),
        transitions to ACTIVE and broadcasts the playlist URL to viewers.
        """
        try:
            if self._pipeline is None:
                return

            # Wait up to 30s for first segment
            await asyncio.wait_for(self._pipeline.ready.wait(), timeout=30.0)

            # Transition to ACTIVE and broadcast
            self.state = VisualizerState.ACTIVE
            log.info(
                "Guild %d: server-rendered engine '%s' active — HLS ready",
                self.guild_id,
                self._engine_type,
            )

            await self._ws_hub.broadcast(self.guild_id, {
                "type": "visualizer",
                "state": "active",
                "engine": self._engine_type,
                "hls_ready": True,
                "playlist_url": f"/activity/stream/{self.guild_id}/viz/playlist.m3u8",
            })
        except asyncio.TimeoutError:
            log.error(
                "Guild %d: HLS pipeline timed out waiting for first segment",
                self.guild_id,
            )
            await self._handle_render_error()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception(
                "Guild %d: error waiting for HLS ready", self.guild_id
            )

    async def _stop_server_render_resources(self) -> None:
        """Tear down all server-rendered engine resources.

        Cancels the render task, unsubscribes from AudioFeatureBus,
        shuts down the bus, and kills the HLS pipeline.
        """
        # Cancel render loop
        if self._render_task is not None and not self._render_task.done():
            self._render_task.cancel()
            try:
                await self._render_task
            except (asyncio.CancelledError, Exception):
                pass
        self._render_task = None

        # Unsubscribe and shutdown AudioFeatureBus
        if self._audio_bus is not None:
            if self._engine is not None and hasattr(self._engine, "on_audio_features"):
                try:
                    await self._audio_bus.unsubscribe(self._engine.on_audio_features)
                except Exception:
                    pass
            try:
                await self._audio_bus.shutdown()
            except Exception:
                log.exception(
                    "Guild %d: error shutting down AudioFeatureBus",
                    self.guild_id,
                )
            self._audio_bus = None

        # Kill HLS pipeline
        if self._pipeline is not None:
            try:
                await self._pipeline.stop()
            except Exception:
                log.exception(
                    "Guild %d: error stopping HLS pipeline", self.guild_id
                )
            self._pipeline = None

    async def _handle_render_error(self) -> None:
        """Handle a render loop or pipeline error.

        Stops all server-rendered resources, transitions to ERROR state,
        and attempts fallback to the DVD engine (client-side, zero resources).
        """
        log.error(
            "Guild %d: render error — stopping server-rendered resources, "
            "falling back to DVD engine",
            self.guild_id,
        )

        # Stop everything
        await self._stop_server_render_resources()
        if self._engine is not None:
            try:
                await self._engine.stop()
            except Exception:
                pass
            self._engine = None

        self.state = VisualizerState.ERROR

        # Attempt fallback to DVD engine (client-side, zero server resources)
        try:
            self._engine_type = "dvd"
            self._engine = self._create_engine_instance()
            await self._engine.initialize(metadata=self._track_metadata)
            await self._engine.activate(metadata=self._track_metadata)
            self.state = VisualizerState.ACTIVE
            log.info(
                "Guild %d: fallback to DVD engine successful", self.guild_id
            )
            await self._broadcast_visualizer_state()
        except Exception:
            log.exception(
                "Guild %d: DVD fallback also failed — staying in ERROR",
                self.guild_id,
            )
            self._engine = None
            self.state = VisualizerState.ERROR
