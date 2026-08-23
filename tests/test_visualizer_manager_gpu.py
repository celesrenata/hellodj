"""Integration tests for VisualizerManager GPU scheduler wiring.

Tests the integration between VisualizerManager, GPUResourceScheduler,
and the engine registry changes (vgalizer removal, random pool update).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the bot directory is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from video.gpu_scheduler import GPUCapacityExceededError, GPUResourceScheduler
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
        mock_gs.get_visualizer_engine.return_value = "projectm"
        mock_gs.VALID_VISUALIZER_ENGINES = {
            "dvd", "projectm", "varda", "fosfora",
            "audiovis", "native", "random", "off",
        }
        mock_gs.set_visualizer_engine = MagicMock()
        mgr = VisualizerManager(
            guild_id=100,
            ws_hub=mock_ws_hub,
            bot_avatar_url="https://example.com/avatar.png",
        )
    return mgr


@pytest.fixture
def dvd_manager(mock_ws_hub):
    """Create a VisualizerManager configured with the client-side DVD engine."""
    with patch("video.visualizer_manager.guild_settings") as mock_gs:
        mock_gs.get_visualizer_engine.return_value = "dvd"
        mock_gs.VALID_VISUALIZER_ENGINES = {
            "dvd", "projectm", "varda", "fosfora",
            "audiovis", "native", "random", "off",
        }
        mock_gs.set_visualizer_engine = MagicMock()
        mgr = VisualizerManager(
            guild_id=200,
            ws_hub=mock_ws_hub,
            bot_avatar_url="https://example.com/avatar.png",
        )
    return mgr


# ---------------------------------------------------------------------------
# GPU Scheduler Allocation on Engine Start
# ---------------------------------------------------------------------------


class TestGPUAllocationOnStart:
    """_start_engine() allocates a GPU VF for server-rendered engines."""

    @pytest.mark.asyncio
    async def test_allocates_vf_for_server_engine(self, manager, mock_ws_hub):
        """Server-rendered engine start allocates a GPU VF slot."""
        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.on_audio_features = MagicMock()

        with patch(
            "video.visualizer_manager.VisualizerManager._create_engine_instance",
            return_value=mock_engine,
        ), patch(
            "video.visualizer_manager.VisualizerManager._start_server_render_pipeline",
            new_callable=AsyncMock,
        ):
            manager.state = VisualizerState.IDLE_NO_VIEWERS
            await manager._start_engine()

        assert _gpu_scheduler.is_allocated(100)
        assert _gpu_scheduler.active_sessions == 1

    @pytest.mark.asyncio
    async def test_no_allocation_for_client_side_engine(self, dvd_manager, mock_ws_hub):
        """Client-side engine (DVD) does NOT allocate a GPU VF."""
        dvd_manager.state = VisualizerState.IDLE_NO_VIEWERS
        await dvd_manager._start_engine()

        assert not _gpu_scheduler.is_allocated(200)
        assert _gpu_scheduler.active_sessions == 0
        assert dvd_manager.state == VisualizerState.ACTIVE

    @pytest.mark.asyncio
    async def test_capacity_exceeded_stays_idle(self, manager, mock_ws_hub):
        """GPUCapacityExceededError keeps manager in IDLE_NO_VIEWERS."""
        mock_engine = AsyncMock()
        mock_engine.is_client_side = False

        # Fill up all VF slots
        for i in range(7):
            _gpu_scheduler.allocate(guild_id=1000 + i, engine_type="varda")

        with patch(
            "video.visualizer_manager.VisualizerManager._create_engine_instance",
            return_value=mock_engine,
        ):
            manager.state = VisualizerState.IDLE_NO_VIEWERS
            await manager._start_engine()

        assert manager.state == VisualizerState.IDLE_NO_VIEWERS
        assert manager._engine is None
        assert not _gpu_scheduler.is_allocated(100)

    @pytest.mark.asyncio
    async def test_engine_init_failure_releases_vf(self, manager, mock_ws_hub):
        """If engine initialization fails after VF allocation, VF is released."""
        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.initialize.side_effect = RuntimeError("EGL init failed")

        with patch(
            "video.visualizer_manager.VisualizerManager._create_engine_instance",
            return_value=mock_engine,
        ):
            manager.state = VisualizerState.IDLE_NO_VIEWERS
            await manager._start_engine()

        assert manager.state == VisualizerState.ERROR
        assert not _gpu_scheduler.is_allocated(100)
        assert _gpu_scheduler.active_sessions == 0


# ---------------------------------------------------------------------------
# GPU Scheduler Release on Engine Stop
# ---------------------------------------------------------------------------


class TestGPUReleaseOnStop:
    """_stop_engine() releases GPU VF for server-rendered engines."""

    @pytest.mark.asyncio
    async def test_releases_vf_on_stop(self, manager, mock_ws_hub):
        """Stopping a server-rendered engine releases the GPU VF."""
        # Simulate an active server-rendered engine
        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.on_audio_features = MagicMock()
        manager._engine = mock_engine
        manager.state = VisualizerState.ACTIVE

        # Manually allocate the VF (simulating what _start_engine did)
        _gpu_scheduler.allocate(100, "projectm")
        assert _gpu_scheduler.is_allocated(100)

        await manager._stop_engine()

        assert not _gpu_scheduler.is_allocated(100)
        assert manager._engine is None

    @pytest.mark.asyncio
    async def test_no_release_for_client_side_stop(self, dvd_manager, mock_ws_hub):
        """Stopping a client-side engine does not call release (no allocation)."""
        mock_engine = AsyncMock()
        mock_engine.is_client_side = True
        dvd_manager._engine = mock_engine
        dvd_manager.state = VisualizerState.ACTIVE

        await dvd_manager._stop_engine()

        # Should complete without error (no VF was allocated)
        assert not _gpu_scheduler.is_allocated(200)
        assert dvd_manager._engine is None


# ---------------------------------------------------------------------------
# GPU Scheduler Release on Suspension
# ---------------------------------------------------------------------------


class TestGPUReleaseOnSuspension:
    """_execute_suspension() releases the GPU VF."""

    @pytest.mark.asyncio
    async def test_releases_vf_on_suspension(self, manager, mock_ws_hub):
        """Suspension releases the GPU VF before stopping engine."""
        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.on_audio_features = MagicMock()
        manager._engine = mock_engine
        manager.state = VisualizerState.SUSPENDING

        _gpu_scheduler.allocate(100, "projectm")
        assert _gpu_scheduler.is_allocated(100)

        await manager._execute_suspension()

        assert not _gpu_scheduler.is_allocated(100)
        assert manager.state == VisualizerState.IDLE_NO_VIEWERS

    @pytest.mark.asyncio
    async def test_full_suspension_debounce_releases_vf(self, manager, mock_ws_hub):
        """Full debounce cycle: ACTIVE → SUSPENDING → IDLE releases VF."""
        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.on_audio_features = MagicMock()
        manager._engine = mock_engine
        manager.state = VisualizerState.ACTIVE

        _gpu_scheduler.allocate(100, "projectm")
        mock_ws_hub.viewer_count.return_value = 0

        await manager.on_viewer_leave(viewer_count=0)
        assert manager.state == VisualizerState.SUSPENDING

        # Wait for the 2s debounce timer to complete
        await asyncio.sleep(2.2)

        assert manager.state == VisualizerState.IDLE_NO_VIEWERS
        assert not _gpu_scheduler.is_allocated(100)


# ---------------------------------------------------------------------------
# Engine Registry Changes (vgalizer removal)
# ---------------------------------------------------------------------------


class TestEngineRegistryChanges:
    """vgalizer is removed from ENGINE_REGISTRY and VALID_VISUALIZER_ENGINES."""

    def test_vgalizer_not_in_engine_registry(self):
        from video.visualizer_engines import ENGINE_REGISTRY

        assert "vgalizer" not in ENGINE_REGISTRY

    def test_vgalizer_not_in_valid_engines(self):
        import guild_settings

        assert "vgalizer" not in guild_settings.VALID_VISUALIZER_ENGINES

    def test_legacy_vgalizer_config_returns_default(self):
        """Guilds with vgalizer stored get the default engine instead."""
        import guild_settings

        # Simulate a guild with vgalizer in their settings
        guild_settings._settings[99999] = {"visualizer_engine": "vgalizer"}
        result = guild_settings.get_visualizer_engine(99999)
        assert result == guild_settings.DEFAULT_VISUALIZER_ENGINE
        # Cleanup
        del guild_settings._settings[99999]

    def test_random_pool_engines_updated(self):
        from video.visualizer_engines import _RANDOM_POOL_ENGINES

        assert _RANDOM_POOL_ENGINES == ["projectm", "audiovis", "fosfora", "varda"]

    def test_random_pool_in_manager_updated(self):
        assert VisualizerManager._RANDOM_POOL_ENGINES == [
            "projectm", "audiovis", "fosfora", "varda"
        ]


# ---------------------------------------------------------------------------
# Multiple Guild GPU Allocation
# ---------------------------------------------------------------------------


class TestMultiGuildAllocation:
    """Multiple guilds can allocate GPU VFs concurrently up to capacity."""

    @pytest.mark.asyncio
    async def test_multiple_guilds_allocate_independently(self, mock_ws_hub):
        """Multiple guilds can each hold a VF slot."""
        managers = []
        for gid in range(101, 104):
            with patch("video.visualizer_manager.guild_settings") as mock_gs:
                mock_gs.get_visualizer_engine.return_value = "varda"
                mock_gs.VALID_VISUALIZER_ENGINES = {
                    "dvd", "projectm", "varda", "fosfora",
                    "audiovis", "native", "random", "off",
                }
                mgr = VisualizerManager(guild_id=gid, ws_hub=mock_ws_hub)
                managers.append(mgr)

        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.on_audio_features = MagicMock()

        for mgr in managers:
            with patch(
                "video.visualizer_manager.VisualizerManager._create_engine_instance",
                return_value=mock_engine,
            ), patch(
                "video.visualizer_manager.VisualizerManager._start_server_render_pipeline",
                new_callable=AsyncMock,
            ):
                mgr.state = VisualizerState.IDLE_NO_VIEWERS
                await mgr._start_engine()

        assert _gpu_scheduler.active_sessions == 3
        for gid in range(101, 104):
            assert _gpu_scheduler.is_allocated(gid)

    @pytest.mark.asyncio
    async def test_release_frees_slot_for_another(self, mock_ws_hub):
        """Releasing a VF allows another guild to allocate."""
        # Fill up all 7 slots
        for i in range(7):
            _gpu_scheduler.allocate(guild_id=500 + i, engine_type="fosfora")

        assert _gpu_scheduler.available_vfs == 0

        # Release one
        _gpu_scheduler.release(500)
        assert _gpu_scheduler.available_vfs == 1

        # Now a new guild can allocate
        _gpu_scheduler.allocate(guild_id=600, engine_type="projectm")
        assert _gpu_scheduler.is_allocated(600)
        assert _gpu_scheduler.available_vfs == 0


# ---------------------------------------------------------------------------
# Integration: Viewer Join → GPU Allocate → Viewer Leave → GPU Release
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    """End-to-end lifecycle: viewer join allocates GPU, viewer leave releases."""

    @pytest.mark.asyncio
    async def test_viewer_join_allocates_leave_releases(self, manager, mock_ws_hub):
        """Complete lifecycle: join → allocate → leave → debounce → release."""
        mock_engine = AsyncMock()
        mock_engine.is_client_side = False
        mock_engine.on_audio_features = MagicMock()

        with patch(
            "video.visualizer_manager.VisualizerManager._create_engine_instance",
            return_value=mock_engine,
        ), patch(
            "video.visualizer_manager.VisualizerManager._start_server_render_pipeline",
            new_callable=AsyncMock,
        ):
            # Start from IDLE (video ended, no viewers yet)
            manager.state = VisualizerState.IDLE_NO_VIEWERS

            # Viewer joins
            await manager.on_viewer_join()

        assert _gpu_scheduler.is_allocated(100)
        # State should be STARTING (server pipeline initializing)
        # or handled via the pipeline ready watcher

        # Simulate that the manager is now ACTIVE
        manager.state = VisualizerState.ACTIVE
        mock_ws_hub.viewer_count.return_value = 0

        # Last viewer leaves
        await manager.on_viewer_leave(viewer_count=0)
        assert manager.state == VisualizerState.SUSPENDING

        # Wait for debounce
        await asyncio.sleep(2.2)

        assert manager.state == VisualizerState.IDLE_NO_VIEWERS
        assert not _gpu_scheduler.is_allocated(100)
