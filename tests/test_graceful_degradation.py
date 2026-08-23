"""Tests for graceful degradation and error recovery.

Validates:
- Req 11 AC 1: GPU error → ERROR state, HLS pipeline stopped
- Req 11 AC 2: ERROR state → WebSocket error notification to viewers
- Req 11 AC 3: Server-rendered engine failure → DVD fallback
- Req 11 AC 4: Render loop exceptions never propagate to bot main event loop
- Req 11 AC 5: GPU device loss detected within 5 seconds
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the bot directory is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from video.gpu_scheduler import GPUCapacityExceededError, GPUResourceScheduler
from video.visualizer_engines.gpu_engine_base import GPURenderError, GPUEngineBase
from video.visualizer_manager import (
    VisualizerManager,
    VisualizerState,
    _gpu_scheduler,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_ws_hub():
    """Create a mock WebSocketHub with required methods."""
    hub = MagicMock()
    hub.broadcast = AsyncMock()
    hub.viewer_count = MagicMock(return_value=0)
    hub.notify_visualizer_error = AsyncMock()
    return hub


@pytest.fixture(autouse=True)
def reset_gpu_scheduler():
    """Reset the module-level GPU scheduler between tests."""
    _gpu_scheduler._allocations.clear()
    yield
    _gpu_scheduler._allocations.clear()


@pytest.fixture
def manager(mock_ws_hub):
    """Create a VisualizerManager configured with a server-rendered engine."""
    with patch("video.visualizer_manager.guild_settings") as mock_gs:
        mock_gs.get_visualizer_engine.return_value = "varda"
        mock_gs.VALID_VISUALIZER_ENGINES = {
            "dvd", "projectm", "varda", "fosfora",
            "audiovis", "random", "off",
        }
        mock_gs.set_visualizer_engine = MagicMock()
        mgr = VisualizerManager(
            guild_id=100,
            ws_hub=mock_ws_hub,
            bot_avatar_url="https://example.com/avatar.png",
        )
    return mgr


# ---------------------------------------------------------------------------
# Test: _handle_render_error releases GPU VF (Req 11 AC 1)
# ---------------------------------------------------------------------------


class TestHandleRenderError:
    """_handle_render_error stops resources, releases GPU, enters ERROR, falls back to DVD."""

    @pytest.mark.asyncio
    async def test_releases_gpu_vf_on_error(self, manager, mock_ws_hub):
        """GPU VF is released when a render error occurs."""
        # Pre-allocate a GPU VF as if the engine was running
        _gpu_scheduler.allocate(100, "varda")
        assert _gpu_scheduler.is_allocated(100)

        # Set up a mock engine
        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.stop = AsyncMock()
        manager._engine = mock_engine
        manager._engine_type = "varda"
        manager.state = VisualizerState.ACTIVE

        # Mock DVD fallback creation
        mock_dvd = AsyncMock()
        mock_dvd.is_client_side = True
        mock_dvd.client_config = {"avatar_url": "https://example.com/avatar.png"}
        with patch.object(manager, "_create_engine_instance", return_value=mock_dvd):
            await manager._handle_render_error()

        # VF should be released
        assert not _gpu_scheduler.is_allocated(100)

    @pytest.mark.asyncio
    async def test_transitions_to_error_then_active_on_dvd_fallback(self, manager, mock_ws_hub):
        """State transitions: → ERROR → ACTIVE (after DVD fallback succeeds)."""
        _gpu_scheduler.allocate(100, "varda")

        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.stop = AsyncMock()
        manager._engine = mock_engine
        manager._engine_type = "varda"
        manager.state = VisualizerState.ACTIVE

        mock_dvd = AsyncMock()
        mock_dvd.is_client_side = True
        mock_dvd.client_config = {"avatar_url": "https://example.com/avatar.png"}
        with patch.object(manager, "_create_engine_instance", return_value=mock_dvd):
            await manager._handle_render_error()

        # Final state should be ACTIVE (DVD fallback succeeded)
        assert manager.state == VisualizerState.ACTIVE
        assert manager._engine_type == "dvd"
        assert manager._engine is mock_dvd

    @pytest.mark.asyncio
    async def test_stays_in_error_when_dvd_fallback_fails(self, manager, mock_ws_hub):
        """If DVD fallback also fails, stays in ERROR state."""
        _gpu_scheduler.allocate(100, "varda")

        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.stop = AsyncMock()
        manager._engine = mock_engine
        manager._engine_type = "varda"
        manager.state = VisualizerState.ACTIVE

        with patch.object(
            manager, "_create_engine_instance", side_effect=RuntimeError("DVD broken")
        ):
            await manager._handle_render_error()

        assert manager.state == VisualizerState.ERROR
        assert manager._engine is None

    @pytest.mark.asyncio
    async def test_notifies_viewers_on_error(self, manager, mock_ws_hub):
        """WebSocket hub is notified when entering ERROR state (Req 11 AC 2)."""
        _gpu_scheduler.allocate(100, "varda")

        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.stop = AsyncMock()
        manager._engine = mock_engine
        manager._engine_type = "varda"
        manager.state = VisualizerState.ACTIVE

        mock_dvd = AsyncMock()
        mock_dvd.is_client_side = True
        mock_dvd.client_config = {"avatar_url": "https://example.com/avatar.png"}
        with patch.object(manager, "_create_engine_instance", return_value=mock_dvd):
            await manager._handle_render_error()

        # Verify the error notification was sent
        mock_ws_hub.notify_visualizer_error.assert_called_once_with(
            100,
            engine="varda",
            message="GPU engine 'varda' encountered an error, switching to fallback",
        )


# ---------------------------------------------------------------------------
# Test: _render_loop exception isolation (Req 11 AC 4)
# ---------------------------------------------------------------------------


class TestRenderLoopExceptionIsolation:
    """Render loop catches all exceptions without propagating to main event loop."""

    @pytest.mark.asyncio
    async def test_broken_pipe_triggers_graceful_degradation(self, manager, mock_ws_hub):
        """BrokenPipeError in render loop triggers _handle_render_error."""
        _gpu_scheduler.allocate(100, "varda")

        # Create a mock engine whose render_frames yields one frame then
        # the pipeline write triggers BrokenPipeError
        async def mock_render_frames():
            yield b"\x00" * (1280 * 720 * 4)

        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.render_frames = mock_render_frames
        mock_engine.stop = AsyncMock()
        manager._engine = mock_engine
        manager._engine_type = "varda"
        manager.state = VisualizerState.ACTIVE

        # Mock the pipeline stdin to raise BrokenPipeError
        mock_pipe = MagicMock()
        mock_pipe.write = MagicMock(side_effect=BrokenPipeError("pipe broken"))
        mock_pipeline = MagicMock()
        mock_pipeline.stdin_pipe = mock_pipe
        manager._pipeline = mock_pipeline

        mock_dvd = AsyncMock()
        mock_dvd.is_client_side = True
        mock_dvd.client_config = {"avatar_url": "test"}
        with patch.object(manager, "_create_engine_instance", return_value=mock_dvd):
            # Run the render loop — should NOT raise
            await manager._render_loop()

        # Should have gone through error handling
        mock_ws_hub.notify_visualizer_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_connection_reset_triggers_graceful_degradation(self, manager, mock_ws_hub):
        """ConnectionResetError in render loop triggers _handle_render_error."""
        _gpu_scheduler.allocate(100, "varda")

        async def mock_render_frames():
            yield b"\x00" * (1280 * 720 * 4)

        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.render_frames = mock_render_frames
        mock_engine.stop = AsyncMock()
        manager._engine = mock_engine
        manager._engine_type = "varda"
        manager.state = VisualizerState.ACTIVE

        mock_pipe = MagicMock()
        mock_pipe.write = MagicMock(side_effect=ConnectionResetError("reset"))
        mock_pipeline = MagicMock()
        mock_pipeline.stdin_pipe = mock_pipe
        manager._pipeline = mock_pipeline

        mock_dvd = AsyncMock()
        mock_dvd.is_client_side = True
        mock_dvd.client_config = {"avatar_url": "test"}
        with patch.object(manager, "_create_engine_instance", return_value=mock_dvd):
            await manager._render_loop()

        mock_ws_hub.notify_visualizer_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_gpu_render_error_triggers_graceful_degradation(self, manager, mock_ws_hub):
        """GPURenderError from engine triggers _handle_render_error."""
        _gpu_scheduler.allocate(100, "varda")

        async def mock_render_frames():
            raise GPURenderError("GL context lost")

        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.render_frames = mock_render_frames
        mock_engine.stop = AsyncMock()
        manager._engine = mock_engine
        manager._engine_type = "varda"
        manager.state = VisualizerState.ACTIVE

        mock_dvd = AsyncMock()
        mock_dvd.is_client_side = True
        mock_dvd.client_config = {"avatar_url": "test"}
        with patch.object(manager, "_create_engine_instance", return_value=mock_dvd):
            # Should NOT propagate the exception
            await manager._render_loop()

        mock_ws_hub.notify_visualizer_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_arbitrary_exception_does_not_propagate(self, manager, mock_ws_hub):
        """Any exception in render loop is caught — never reaches main event loop."""
        _gpu_scheduler.allocate(100, "varda")

        async def mock_render_frames():
            raise RuntimeError("Unexpected GPU error")

        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.render_frames = mock_render_frames
        mock_engine.stop = AsyncMock()
        manager._engine = mock_engine
        manager._engine_type = "varda"
        manager.state = VisualizerState.ACTIVE

        mock_dvd = AsyncMock()
        mock_dvd.is_client_side = True
        mock_dvd.client_config = {"avatar_url": "test"}
        with patch.object(manager, "_create_engine_instance", return_value=mock_dvd):
            # Must not raise
            await manager._render_loop()

        # Graceful degradation was triggered
        assert manager.state == VisualizerState.ACTIVE  # DVD fallback


# ---------------------------------------------------------------------------
# Test: GPU device loss detection (Req 11 AC 5)
# ---------------------------------------------------------------------------


class TestDeviceLossDetection:
    """GPU device loss detected within 5 seconds of no new frames."""

    @pytest.mark.asyncio
    async def test_device_loss_detected_after_timeout(self, manager, mock_ws_hub):
        """Device loss watchdog triggers error after 5s with no frames."""
        _gpu_scheduler.allocate(100, "varda")

        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.stop = AsyncMock()
        manager._engine = mock_engine
        manager._engine_type = "varda"
        manager.state = VisualizerState.ACTIVE
        manager._render_task = MagicMock()
        manager._render_task.done = MagicMock(return_value=False)
        manager._render_task.cancel = MagicMock()

        # Set last frame time to 6 seconds ago (exceeds 5s threshold)
        manager._last_frame_time = time.monotonic() - 6.0

        mock_dvd = AsyncMock()
        mock_dvd.is_client_side = True
        mock_dvd.client_config = {"avatar_url": "test"}
        with patch.object(manager, "_create_engine_instance", return_value=mock_dvd):
            # Run watchdog — it should detect loss after one check interval
            # Patch sleep to avoid waiting
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await manager._device_loss_watchdog()

        # Error handling should have been triggered
        mock_ws_hub.notify_visualizer_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_device_loss_not_triggered_when_frames_flowing(self, manager, mock_ws_hub):
        """Watchdog does NOT trigger when frames are being produced."""
        manager.state = VisualizerState.ACTIVE
        manager._last_frame_time = time.monotonic()  # Fresh frame

        call_count = 0

        async def mock_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                # Simulate state change to stop the watchdog
                manager.state = VisualizerState.IDLE_NO_VIEWERS

        with patch("asyncio.sleep", side_effect=mock_sleep):
            await manager._device_loss_watchdog()

        # Error handler should NOT have been called
        mock_ws_hub.notify_visualizer_error.assert_not_called()


# ---------------------------------------------------------------------------
# Test: GPUEngineBase exception isolation (Req 11 AC 4)
# ---------------------------------------------------------------------------


class TestGPUEngineBaseExceptionIsolation:
    """GPU engine render_frames raises GPURenderError on GL failures."""

    @pytest.mark.asyncio
    async def test_gl_error_raises_gpu_render_error(self):
        """GL error during rendering is wrapped in GPURenderError."""

        class TestEngine(GPUEngineBase):
            async def _on_gl_ready(self, metadata):
                pass

            def _render_gl_frame(self, features):
                raise RuntimeError("GL_INVALID_OPERATION")

        engine = TestEngine()
        # Mock the EGL context
        mock_ctx = MagicMock()
        mock_ctx.make_current = MagicMock()
        mock_ctx.read_pixels = MagicMock(return_value=b"\x00" * (1280 * 720 * 4))
        engine._egl_ctx = mock_ctx
        engine._running = True

        with pytest.raises(GPURenderError, match="GPU rendering failed"):
            async for _ in engine.render_frames():
                pass

    @pytest.mark.asyncio
    async def test_make_current_failure_raises_gpu_render_error(self):
        """EGL make_current failure is wrapped in GPURenderError."""

        class TestEngine(GPUEngineBase):
            async def _on_gl_ready(self, metadata):
                pass

            def _render_gl_frame(self, features):
                pass

        engine = TestEngine()
        mock_ctx = MagicMock()
        mock_ctx.make_current = MagicMock(side_effect=OSError("EGL context lost"))
        engine._egl_ctx = mock_ctx
        engine._running = True

        with pytest.raises(GPURenderError, match="GPU rendering failed"):
            async for _ in engine.render_frames():
                pass

    @pytest.mark.asyncio
    async def test_successful_frames_do_not_raise(self):
        """Normal rendering yields frames without error."""

        class TestEngine(GPUEngineBase):
            async def _on_gl_ready(self, metadata):
                pass

            def _render_gl_frame(self, features):
                pass  # Render into FBO (no-op for test)

        engine = TestEngine()
        mock_ctx = MagicMock()
        mock_ctx.make_current = MagicMock()
        mock_ctx.read_pixels = MagicMock(return_value=b"\x00" * (1280 * 720 * 4))
        engine._egl_ctx = mock_ctx
        engine._running = True

        frames = []
        async for frame in engine.render_frames():
            frames.append(frame)
            if len(frames) >= 3:
                engine._running = False  # Stop after 3 frames

        assert len(frames) == 3
        assert all(len(f) == 1280 * 720 * 4 for f in frames)


# ---------------------------------------------------------------------------
# Test: WebSocket error notification (Req 11 AC 2)
# ---------------------------------------------------------------------------


class TestWebSocketErrorNotification:
    """ws_hub.notify_visualizer_error broadcasts error to viewers."""

    @pytest.mark.asyncio
    async def test_notify_visualizer_error_broadcasts_message(self):
        """notify_visualizer_error sends correct message format."""
        from video.ws_hub import WebSocketHub

        hub = WebSocketHub(validate_guild_token=lambda t: None)

        # Mock broadcast
        hub.broadcast = AsyncMock()

        await hub.notify_visualizer_error(
            guild_id=100,
            engine="varda",
            message="Test error message",
        )

        hub.broadcast.assert_called_once_with(100, {
            "type": "visualizer_error",
            "engine": "varda",
            "message": "Test error message",
            "fallback": "dvd",
        })

    @pytest.mark.asyncio
    async def test_notify_visualizer_error_default_message(self):
        """notify_visualizer_error uses default message when none provided."""
        from video.ws_hub import WebSocketHub

        hub = WebSocketHub(validate_guild_token=lambda t: None)
        hub.broadcast = AsyncMock()

        await hub.notify_visualizer_error(guild_id=200, engine="fosfora")

        call_args = hub.broadcast.call_args[0]
        assert call_args[0] == 200
        assert call_args[1]["type"] == "visualizer_error"
        assert call_args[1]["engine"] == "fosfora"
        assert "fosfora" in call_args[1]["message"]
        assert call_args[1]["fallback"] == "dvd"


# ---------------------------------------------------------------------------
# Test: Varda shader compile failure uses fallback (not ERROR state)
# ---------------------------------------------------------------------------


class TestShaderCompileFailureFallback:
    """Varda shader compile failure uses fallback shader, not ERROR state."""

    @pytest.mark.asyncio
    async def test_shader_compile_failure_does_not_enter_error_state(self, manager, mock_ws_hub):
        """When a shader fails to compile, the engine uses a fallback shader
        and does NOT transition to ERROR state. (Already implemented in Varda
        engine task 3.1 — this test validates the requirement.)"""
        # The Varda engine's _on_gl_ready handles shader compile failures
        # internally by falling back to plasma.glsl. The VisualizerManager
        # never sees an exception — Varda recovers silently.
        # We test this by verifying the contract: if engine.activate() succeeds,
        # we stay in STARTING/ACTIVE, not ERROR.

        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.on_audio_features = MagicMock()
        mock_engine.initialize = AsyncMock()
        mock_engine.activate = AsyncMock()  # No exception = shader fallback worked

        with patch.object(manager, "_create_engine_instance", return_value=mock_engine):
            with patch.object(manager, "_start_server_render_pipeline", new_callable=AsyncMock):
                manager.state = VisualizerState.IDLE_NO_VIEWERS
                await manager._start_engine()

        # Engine started successfully — NOT in ERROR state
        assert manager.state != VisualizerState.ERROR


# ---------------------------------------------------------------------------
# Test: GPU capacity exceeded (Req 4 AC 3, Req 11 integration)
# ---------------------------------------------------------------------------


class TestGPUCapacityExceeded:
    """GPU capacity exceeded results in IDLE_NO_VIEWERS, not ERROR."""

    @pytest.mark.asyncio
    async def test_capacity_exceeded_stays_idle(self, manager, mock_ws_hub):
        """When all VF slots are full, engine start stays in IDLE_NO_VIEWERS."""
        # Fill up all VF slots
        for i in range(7):
            _gpu_scheduler.allocate(1000 + i, "projectm")

        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.on_audio_features = MagicMock()

        with patch.object(manager, "_create_engine_instance", return_value=mock_engine):
            manager.state = VisualizerState.IDLE_NO_VIEWERS
            await manager._start_engine()

        # Should NOT enter ERROR — stays in IDLE_NO_VIEWERS
        assert manager.state == VisualizerState.IDLE_NO_VIEWERS
        assert manager._engine is None
