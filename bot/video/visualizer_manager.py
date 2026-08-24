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
import time
from pathlib import Path
from typing import TYPE_CHECKING

from video.audio_feature_bus import AudioFeatureBus
from video.audio_pipe import AudioPipeSession
from video.gpu_scheduler import GPUCapacityExceededError, GPUResourceScheduler
from video.hls_transcode import HLSTranscodePipeline
from video.lavalink_pipe_client import LavalinkPipeClient
from video.visualizer_engines import VisualizerRenderer, create_engine
from video.visualizer_engines.base import TrackMetadata
from video.visualizer_engines.gpu_engine_base import GPURenderError

if TYPE_CHECKING:
    from video.ws_hub import WebSocketHub

import guild_settings

log = logging.getLogger(__name__)

# Module-level singleton GPU scheduler (one per process)
_gpu_scheduler = GPUResourceScheduler()


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
    _RANDOM_POOL_ENGINES: list[str] = ["drift", "projectm", "audiovis", "fosfora", "varda"]

    def __init__(
        self,
        guild_id: int,
        ws_hub: WebSocketHub,
        bot_avatar_url: str = "https://cdn.discordapp.com/embed/avatars/0.png",
    ) -> None:
        self.guild_id = guild_id
        self._ws_hub = ws_hub
        self._bot_avatar_url = bot_avatar_url
        self._engine: VisualizerRenderer | None = None
        self._engine_type: str = guild_settings.get_visualizer_engine(guild_id)
        # Start in IDLE_NO_VIEWERS if an engine is configured (ready for viewers),
        # otherwise DISABLED. This ensures the visualizer starts on first viewer
        # connect after a bot restart without requiring a /visualizer command.
        self.state = (
            VisualizerState.IDLE_NO_VIEWERS
            if self._engine_type and self._engine_type != "off"
            else VisualizerState.DISABLED
        )
        self._suspend_task: asyncio.Task | None = None
        self._track_metadata: TrackMetadata | None = None

        # Server-rendered engine resources
        self._audio_bus: AudioFeatureBus | None = None
        self._pipeline: HLSTranscodePipeline | None = None
        self._render_task: asyncio.Task | None = None
        self._device_loss_task: asyncio.Task | None = None
        self._last_frame_time: float = 0.0

        # Audio pipe resources (Lavalink PCM → AudioFeatureBus)
        self._pipe_session: AudioPipeSession | None = None
        self._pipe_client = LavalinkPipeClient()
        self._pipe_reader_task: asyncio.Task | None = None

        # Random mode cycling state
        self._random_cycle: list[str] = []
        self._random_index: int = 0
        self._last_random_engine: str = ""

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

        For server-rendered engines: allocates a GPU VF slot via the scheduler,
        starts the HLS pipeline, render loop, and AudioFeatureBus subscription.
        Transitions to ACTIVE once the first HLS segment is ready.
        """
        self.state = VisualizerState.STARTING
        log.debug(
            "Guild %d: starting engine '%s'", self.guild_id, self._engine_type
        )

        try:
            if self._engine_type == "random":
                # Use fallback chain for random mode (Req 9 AC 3-4)
                self._engine, actual_engine_type = self._create_random_engine_with_fallback()
            else:
                self._engine = self._create_engine_instance()
                actual_engine_type = self._engine_type
        except Exception:
            log.exception(
                "Guild %d: failed to create engine '%s'",
                self.guild_id,
                self._engine_type,
            )
            self._engine = None
            self.state = VisualizerState.ERROR
            return

        # For server-rendered engines, allocate a GPU VF before EGL context
        if not self._engine.is_client_side:
            try:
                _gpu_scheduler.allocate(self.guild_id, self._engine_type)
            except GPUCapacityExceededError:
                log.warning(
                    "Guild %d: GPU capacity exceeded — cannot start engine '%s', "
                    "remaining in IDLE_NO_VIEWERS",
                    self.guild_id,
                    self._engine_type,
                )
                self._engine = None
                self.state = VisualizerState.IDLE_NO_VIEWERS
                return

        try:
            await self._engine.initialize(metadata=self._track_metadata)
            await self._engine.activate(metadata=self._track_metadata)
        except Exception:
            log.exception(
                "Guild %d: failed to start engine '%s'",
                self.guild_id,
                self._engine_type,
            )
            # Release GPU allocation if we had one
            if not self._engine.is_client_side:
                _gpu_scheduler.release(self.guild_id)
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
        AudioFeatureBus subscription, HLS pipeline, and releases the GPU VF.
        """
        # Clean up server-rendered resources first
        await self._stop_server_render_resources()

        if self._engine is not None:
            # Release GPU VF allocation for server-rendered engines
            if not self._engine.is_client_side:
                _gpu_scheduler.release(self.guild_id)
            try:
                await self._engine.stop()
            except Exception:
                log.exception(
                    "Guild %d: error stopping engine", self.guild_id
                )
            self._engine = None

    def _create_engine_instance(self) -> VisualizerRenderer:
        """Instantiate the configured engine from the registry.

        Reads per-guild engine config from guild_settings and passes it
        as constructor kwargs.

        Returns:
            A configured VisualizerRenderer instance.

        Raises:
            ValueError: If the engine type is unknown.
        """
        engine_type = self._engine_type

        # For "random" mode, select the next engine from the pool.
        if engine_type == "random":
            engine_type = self._select_next_random_engine()

        # Build kwargs from guild settings config for this engine
        kwargs: dict = {}
        if engine_type == "dvd":
            kwargs["bot_avatar_url"] = self._bot_avatar_url
        else:
            # Load per-guild engine config (e.g., style, fft_bins, glow_intensity)
            config = guild_settings.get_visualizer_config(self.guild_id, engine_type)
            if config:
                kwargs.update(config)

        return create_engine(engine_type, **kwargs)

    def _create_random_engine_with_fallback(self) -> tuple[VisualizerRenderer, str]:
        """Try each engine in the random pool with fallback chain.

        Selects the next random engine (no consecutive repeat), and if it
        fails to instantiate, tries each remaining pool engine in order.
        If all fail, falls back to the DVD engine.

        Returns:
            Tuple of (engine_instance, engine_type_string).
        """
        pool = list(self._RANDOM_POOL_ENGINES)

        if not pool:
            log.warning(
                "Guild %d: random pool is empty — falling back to dvd",
                self.guild_id,
            )
            return create_engine("dvd", bot_avatar_url=self._bot_avatar_url), "dvd"

        # Get the primary selection (no-repeat logic)
        primary = self._select_next_random_engine()

        # Build ordered attempt list: primary first, then remaining pool engines
        attempt_order = [primary] + [e for e in pool if e != primary]

        for engine_name in attempt_order:
            try:
                engine = create_engine(engine_name)
                log.debug(
                    "Guild %d: random mode selected engine '%s'",
                    self.guild_id,
                    engine_name,
                )
                return engine, engine_name
            except Exception:
                log.warning(
                    "Guild %d: random mode — engine '%s' failed to instantiate, "
                    "trying next in pool",
                    self.guild_id,
                    engine_name,
                    exc_info=True,
                )
                continue

        # All pool engines failed — fall back to DVD (Req 9 AC 4)
        log.warning(
            "Guild %d: all random pool engines failed — falling back to dvd",
            self.guild_id,
        )
        return create_engine("dvd", bot_avatar_url=self._bot_avatar_url), "dvd"

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
        current_engine = self._last_random_engine or ""

        # Try up to len(pool) times to find a different engine
        for _ in range(len(self._random_cycle)):
            candidate = self._random_cycle[self._random_index]
            self._random_index = (self._random_index + 1) % len(self._random_cycle)
            if candidate != current_engine:
                self._last_random_engine = candidate
                return candidate

        # All engines are the same as current (shouldn't happen with len > 1)
        # Just return whatever is next in the cycle
        result = self._random_cycle[self._random_index]
        self._random_index = (self._random_index + 1) % len(self._random_cycle)
        self._last_random_engine = result
        return result

    # ------------------------------------------------------------------
    # Suspension debounce
    # ------------------------------------------------------------------

    # Debounce duration (seconds) before suspending engine after last viewer leaves.
    # 10 seconds handles transient disconnects (Req 12 AC 3).
    SUSPENSION_DEBOUNCE_SECONDS: float = 5.0

    async def _begin_suspension(self) -> None:
        """Start 10-second debounce before suspending the engine.

        Transitions to SUSPENDING state and starts an asyncio timer.
        If a viewer reconnects within 10s, the timer is cancelled and
        the engine remains ACTIVE. After 10s with zero viewers, the
        engine is fully suspended (EGL context destroyed, GPU VF released).
        """
        self.state = VisualizerState.SUSPENDING
        log.debug(
            "Guild %d: beginning suspension (%.0fs debounce)",
            self.guild_id,
            self.SUSPENSION_DEBOUNCE_SECONDS,
        )

        self._suspend_task = asyncio.create_task(self._suspension_timer())

    async def _suspension_timer(self) -> None:
        """Wait SUSPENSION_DEBOUNCE_SECONDS, then re-check viewer count.

        If zero viewers remain after the debounce period, executes full
        suspension (EGL context destroyed, GPU VF released, segments cleaned).
        If viewers reconnected during the debounce window, transitions back
        to ACTIVE without any resource teardown.
        """
        try:
            await asyncio.sleep(self.SUSPENSION_DEBOUNCE_SECONDS)
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
        """Actually suspend: stop engine, release GPU VF, clean segments, transition to IDLE_NO_VIEWERS."""
        # Release GPU VF before stopping engine
        _gpu_scheduler.release(self.guild_id)
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

        message = self.get_current_state_message()
        if message is not None:
            await self._ws_hub.broadcast(self.guild_id, message)

    def get_current_state_message(self) -> dict | None:
        """Return the current visualizer state as a WS message dict.

        Used by ws_hub for late-joiner sync so reconnecting clients get
        the correct engine state (including HLS URL if active).

        Returns:
            A dict ready to send as a WebSocket message, or None if no
            engine is active.
        """
        if self._engine is None:
            return None
        if self.state not in (VisualizerState.ACTIVE, VisualizerState.STARTING):
            return None

        if self._engine.is_client_side:
            return {
                "type": "visualizer",
                "state": "active",
                "engine": self._engine_type,
                "config": self._engine.client_config,
            }
        else:
            # Server-rendered engine — include HLS readiness and playlist URL
            hls_ready = self._pipeline is not None and self._pipeline.ready.is_set()
            message: dict = {
                "type": "visualizer",
                "state": "active",
                "engine": self._engine_type,
                "hls_ready": hls_ready,
            }
            if hls_ready:
                message["playlist_url"] = (
                    f"/activity/stream/{self.guild_id}/viz/playlist.m3u8"
                )
            return message

    # ------------------------------------------------------------------
    # Server-rendered engine lifecycle
    # ------------------------------------------------------------------

    async def _start_server_render_pipeline(self) -> None:
        """Initialize AudioFeatureBus, HLS pipeline, and render loop.

        Called when a server-rendered engine is activated. Sets up:
        1. AudioFeatureBus — subscribes the engine's audio callback
        2. AudioPipeSession — creates FIFO for Lavalink PCM → FFmpeg audio
        3. HLSTranscodePipeline — ffmpeg visualizer mode (rawvideo stdin + pipe audio)
        4. Lavalink pipe enable — tells Lavalink to write PCM to the FIFO
        5. Render loop — async task piping frames to ffmpeg stdin
        6. Ready watcher — notifies frontend when first segment appears

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

        # 2. Audio pipe: Lavalink PCM → AudioFeatureBus for visualizer analysis.
        # The FIFO carries raw PCM from Lavalink's filter chain. A reader task
        # reads from it and feeds AudioFeatureBus.feed_pcm() so the engine gets
        # frequency/beat data to drive the visualization. The audio does NOT go
        # into the HLS stream — users hear audio via Discord VC.
        audio_pipe_path: str | None = None
        pipe_enabled = False

        self._pipe_session = AudioPipeSession(self.guild_id, "viz")
        pipe_ok = await self._pipe_session.start()
        if pipe_ok:
            pipe_enabled = await self._pipe_client.enable_pipe(
                self.guild_id, self._pipe_session.ffmpeg_input_path
            )
            if pipe_enabled:
                # Close the primer fd NOW — Lavalink has opened the FIFO for writing,
                # so we no longer need the primer. If we leave it open, it competes
                # as a reader and absorbs data meant for the pipe reader task.
                self._pipe_session.close_primer()
                audio_pipe_path = self._pipe_session.ffmpeg_input_path
                log.info(
                    "Guild %d: audio pipe enabled for visualizer: %s",
                    self.guild_id,
                    audio_pipe_path,
                )
            else:
                log.info(
                    "Guild %d: audio pipe enable failed — visualizer won't have audio features",
                    self.guild_id,
                )
        else:
            log.warning(
                "Guild %d: audio pipe creation failed — visualizer won't have audio features",
                self.guild_id,
            )
            self._pipe_session = None

        # If pipe wasn't enabled, clean up the session
        if not pipe_enabled and self._pipe_session:
            await self._pipe_session.stop()
            self._pipe_session = None

        # 3. Create HLS pipeline in visualizer mode (VIDEO ONLY — no audio mux)
        self._pipeline = HLSTranscodePipeline(
            guild_id=self.guild_id,
            session_id="viz",
        )
        await self._pipeline.start_visualizer(audio_pipe_path=None)  # No audio in HLS
        log.debug(
            "Guild %d: HLS visualizer pipeline started (video-only)",
            self.guild_id,
        )

        # 4. Start FIFO reader task to feed AudioFeatureBus from the pipe
        if audio_pipe_path and self._pipe_session:
            self._pipe_reader_task = asyncio.create_task(
                self._pipe_reader_loop(audio_pipe_path),
                name=f"viz-pipe-reader-{self.guild_id}",
            )

        # 5. Start the render loop task
        self._render_task = asyncio.create_task(
            self._render_loop(),
            name=f"viz-render-{self.guild_id}",
        )

        # 5b. Start device loss watchdog (Req 11 AC 5)
        self._last_frame_time = time.monotonic()
        self._device_loss_task = asyncio.create_task(
            self._device_loss_watchdog(),
            name=f"viz-devloss-{self.guild_id}",
        )

        # 6. Start a task to wait for pipeline readiness and notify frontend
        asyncio.create_task(
            self._wait_for_hls_ready(),
            name=f"viz-ready-{self.guild_id}",
        )

    async def _render_loop(self) -> None:
        """Pipe engine render_frames() output to ffmpeg stdin.

        Runs until the engine is stopped, the pipeline dies, or an error
        occurs. On error, triggers the render error handler for fallback.

        Exception isolation (Req 11 AC 4): ALL exceptions are caught here
        and never propagate to the bot's main event loop.

        GPU device loss detection (Req 11 AC 5): If no frames are produced
        within 5 seconds while the engine is running, the loop triggers
        graceful degradation.
        """
        last_frame_time = time.monotonic()
        DEVICE_LOSS_TIMEOUT = 5.0
        try:
            async for frame_data in self._engine.render_frames():
                last_frame_time = time.monotonic()
                self._last_frame_time = last_frame_time
                if self._pipeline and self._pipeline.stdin_pipe:
                    self._pipeline.stdin_pipe.write(frame_data)
                    await self._pipeline.stdin_pipe.drain()
                else:
                    # Pipeline gone — stop rendering
                    break
        except asyncio.CancelledError:
            return
        except (BrokenPipeError, ConnectionResetError) as exc:
            log.warning(
                "Guild %d: render loop pipe error (%s) — triggering graceful degradation",
                self.guild_id,
                type(exc).__name__,
            )
            await self._handle_render_error()
        except Exception:
            log.exception(
                "Guild %d: render loop error", self.guild_id
            )
            await self._handle_render_error()

    async def _device_loss_watchdog(self) -> None:
        """Monitor for GPU device loss (Req 11 AC 5).

        Checks every 2 seconds whether the render loop has produced a frame
        within the last 5 seconds. If not, this indicates a GPU device loss
        (broken driver, hot-unplug, hung render node) and triggers graceful
        degradation.
        """
        DEVICE_LOSS_TIMEOUT = 5.0
        CHECK_INTERVAL = 2.0
        try:
            while self.state in (VisualizerState.STARTING, VisualizerState.ACTIVE):
                await asyncio.sleep(CHECK_INTERVAL)
                if self._last_frame_time == 0.0:
                    # No frames yet — still starting up, skip check
                    continue
                elapsed_since_frame = time.monotonic() - self._last_frame_time
                if elapsed_since_frame > DEVICE_LOSS_TIMEOUT:
                    log.error(
                        "Guild %d: GPU device loss detected — no frames for %.1fs",
                        self.guild_id,
                        elapsed_since_frame,
                    )
                    # Cancel the render task before handling the error
                    if self._render_task and not self._render_task.done():
                        self._render_task.cancel()
                    await self._handle_render_error()
                    return
        except asyncio.CancelledError:
            return

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

        Cancels the render task, device loss watchdog, unsubscribes from
        AudioFeatureBus, shuts down the bus, disables audio pipe, and kills
        the HLS pipeline.
        """
        # Cancel render loop
        if self._render_task is not None and not self._render_task.done():
            self._render_task.cancel()
            try:
                await self._render_task
            except (asyncio.CancelledError, Exception):
                pass
        self._render_task = None

        # Cancel device loss watchdog
        if self._device_loss_task is not None and not self._device_loss_task.done():
            self._device_loss_task.cancel()
            try:
                await self._device_loss_task
            except (asyncio.CancelledError, Exception):
                pass
        self._device_loss_task = None

        # Disable Lavalink audio pipe and clean up FIFO
        if self._pipe_reader_task is not None and not self._pipe_reader_task.done():
            self._pipe_reader_task.cancel()
            try:
                await self._pipe_reader_task
            except (asyncio.CancelledError, Exception):
                pass
        self._pipe_reader_task = None

        if self._pipe_session and self._pipe_session.active:
            try:
                await self._pipe_client.disable_pipe(self.guild_id)
            except Exception:
                log.debug(
                    "Guild %d: error disabling Lavalink pipe (non-fatal)",
                    self.guild_id,
                    exc_info=True,
                )
            try:
                await self._pipe_session.stop()
            except Exception:
                log.debug(
                    "Guild %d: error stopping pipe session (non-fatal)",
                    self.guild_id,
                    exc_info=True,
                )
        self._pipe_session = None

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

    async def _pipe_reader_loop(self, pipe_path: str) -> None:
        """Read PCM from the Lavalink audio pipe and feed AudioFeatureBus.

        Resilient to track changes: when Lavalink closes the pipe (track end),
        the reader re-creates the FIFO and re-enables the pipe for the next
        track. This keeps the visualizer running across track transitions
        without tearing down the entire rendering pipeline.

        Runs until cancelled (visualizer shutdown).
        """
        import os

        CHUNK_SIZE = 3840  # 20ms @ 48kHz stereo s16le
        RECONNECT_INTERVAL = 0.5  # Wait before attempting reconnect

        while True:
            fd = -1
            try:
                # Ensure FIFO exists (re-create if cleaned up between tracks)
                if not os.path.exists(pipe_path):
                    os.makedirs(os.path.dirname(pipe_path), exist_ok=True)
                    os.mkfifo(pipe_path)
                    # Prime it so Lavalink's open(O_WRONLY) won't block
                    primer = os.open(pipe_path, os.O_RDWR | os.O_NONBLOCK)
                    # Try to re-enable pipe on Lavalink
                    enabled = await self._pipe_client.enable_pipe(
                        self.guild_id, pipe_path
                    )
                    if enabled:
                        os.close(primer)  # Close primer so reader gets the data
                        log.info(
                            "Guild %d: pipe reader reconnected to Lavalink",
                            self.guild_id,
                        )
                    else:
                        # Keep primer open (no writer yet), wait and retry
                        os.close(primer)
                        await asyncio.sleep(RECONNECT_INTERVAL)
                        continue

                fd = os.open(pipe_path, os.O_RDONLY)
                log.info(
                    "Guild %d: pipe reader started — feeding AudioFeatureBus",
                    self.guild_id,
                )

                loop = asyncio.get_event_loop()
                while True:
                    data = await loop.run_in_executor(None, os.read, fd, CHUNK_SIZE)
                    if not data:
                        # EOF — Lavalink closed (track ended). Reconnect.
                        break
                    if self._audio_bus:
                        self._audio_bus.feed_pcm(data)

            except asyncio.CancelledError:
                return
            except OSError as exc:
                log.debug("Guild %d: pipe reader OSError: %s", self.guild_id, exc)
            except Exception:
                log.debug(
                    "Guild %d: pipe reader error", self.guild_id, exc_info=True
                )
            finally:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

            # Wait before reconnecting (debounce rapid track changes)
            try:
                await asyncio.sleep(RECONNECT_INTERVAL)
            except asyncio.CancelledError:
                return

    async def _handle_render_error(self) -> None:
        """Handle a render loop or pipeline error.

        Graceful degradation sequence (Req 11 AC 1-3, 5):
        1. Stop server-rendered resources (HLS pipeline, audio bus, render task)
        2. Release GPU VF allocation
        3. Transition to ERROR state
        4. Notify connected viewers via WebSocket
        5. Attempt fallback to the DVD engine (client-side, zero resources)
        """
        failed_engine = self._engine_type
        log.error(
            "Guild %d: render error — stopping server-rendered resources, "
            "falling back to DVD engine (was: %s)",
            self.guild_id,
            failed_engine,
        )

        # 1. Stop server-rendered resources
        await self._stop_server_render_resources()
        if self._engine is not None:
            try:
                await self._engine.stop()
            except Exception:
                pass
            self._engine = None

        # 2. Release GPU VF allocation
        _gpu_scheduler.release(self.guild_id)

        # 3. Transition to ERROR state
        self.state = VisualizerState.ERROR

        # 4. Notify connected viewers of the error (Req 11 AC 2)
        try:
            await self._ws_hub.notify_visualizer_error(
                self.guild_id,
                engine=failed_engine,
                message=f"GPU engine '{failed_engine}' encountered an error, switching to fallback",
            )
        except Exception:
            log.debug(
                "Guild %d: failed to send error notification to viewers",
                self.guild_id,
                exc_info=True,
            )

        # 5. Attempt fallback to DVD engine (client-side, zero server resources)
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
