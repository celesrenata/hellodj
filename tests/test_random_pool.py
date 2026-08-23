"""Tests for random engine pool: no-repeat, fallback chain, GPU-unavailable.

Validates:
- Random engine selection does not repeat consecutively (Req 9 AC 2)
- Fallback chain tries next engine on failure (Req 9 AC 3)
- All fail → DVD fallback (Req 9 AC 4)
- GPU unavailable → only client-side engines available (Req 10 AC 4)
- get_available_engines() reflects GPU state (Req 10 AC 3)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the bot directory is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from video.visualizer_engines import (
    ENGINE_REGISTRY,
    _RANDOM_POOL_ENGINES,
    get_available_engines,
    set_gpu_available,
)
from video.visualizer_manager import VisualizerManager, VisualizerState


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


@pytest.fixture
def manager(mock_ws_hub):
    """Create a VisualizerManager configured for random mode."""
    with patch("video.visualizer_manager.guild_settings") as mock_gs:
        mock_gs.get_visualizer_engine.return_value = "random"
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


@pytest.fixture(autouse=True)
def reset_gpu_state():
    """Reset GPU availability between tests."""
    set_gpu_available(False)
    yield
    set_gpu_available(False)


# ---------------------------------------------------------------------------
# get_available_engines() tests (Req 10)
# ---------------------------------------------------------------------------


class TestGetAvailableEngines:
    """Tests for the engine feasibility gate."""

    def test_no_gpu_only_client_side(self):
        """When no GPU is available, only dvd and off are returned."""
        engines = get_available_engines(gpu_available=False)
        assert "dvd" in engines
        assert "off" in engines
        # GPU engines should NOT be present
        assert "projectm" not in engines
        assert "audiovis" not in engines
        assert "fosfora" not in engines
        assert "varda" not in engines
        assert "native" not in engines
        # random requires GPU engines
        assert "random" not in engines

    def test_gpu_available_all_engines(self):
        """When GPU is available, all registered engines plus meta-entries are returned."""
        engines = get_available_engines(gpu_available=True)
        assert "dvd" in engines
        assert "off" in engines
        assert "random" in engines
        assert "projectm" in engines
        assert "audiovis" in engines
        assert "fosfora" in engines
        assert "varda" in engines
        assert "native" in engines

    def test_module_level_state(self):
        """get_available_engines() uses the module-level _gpu_available flag."""
        set_gpu_available(True)
        engines = get_available_engines()
        assert "projectm" in engines
        assert "random" in engines

        set_gpu_available(False)
        engines = get_available_engines()
        assert "projectm" not in engines
        assert "random" not in engines

    def test_returns_sorted(self):
        """The returned list is sorted alphabetically."""
        engines = get_available_engines(gpu_available=True)
        assert engines == sorted(engines)

    def test_all_random_pool_engines_in_registry(self):
        """Every engine in _RANDOM_POOL_ENGINES is in ENGINE_REGISTRY."""
        for engine in _RANDOM_POOL_ENGINES:
            assert engine in ENGINE_REGISTRY, (
                f"Random pool engine '{engine}' not found in ENGINE_REGISTRY"
            )


# ---------------------------------------------------------------------------
# No-repeat tests (Req 9 AC 2)
# ---------------------------------------------------------------------------


class TestNoConsecutiveRepeat:
    """Random selection should not repeat the same engine consecutively."""

    def test_no_consecutive_repeat(self, manager):
        """Selecting multiple times never gives the same engine twice in a row."""
        selections: list[str] = []
        for _ in range(20):
            engine = manager._select_next_random_engine()
            selections.append(engine)

        # Check no consecutive duplicates
        for i in range(1, len(selections)):
            assert selections[i] != selections[i - 1], (
                f"Consecutive repeat at index {i}: {selections[i-1]} == {selections[i]}"
            )

    def test_single_engine_pool_repeats_allowed(self, manager):
        """If pool has only one engine, repeats are unavoidable."""
        manager._RANDOM_POOL_ENGINES = ["projectm"]
        engine1 = manager._select_next_random_engine()
        engine2 = manager._select_next_random_engine()
        assert engine1 == "projectm"
        assert engine2 == "projectm"

    def test_all_pool_engines_eventually_selected(self, manager):
        """All engines in the pool are selected over enough iterations."""
        seen: set[str] = set()
        for _ in range(100):
            engine = manager._select_next_random_engine()
            seen.add(engine)
        # All pool engines should have appeared
        for engine in _RANDOM_POOL_ENGINES:
            assert engine in seen, f"Engine '{engine}' was never selected"


# ---------------------------------------------------------------------------
# Fallback chain tests (Req 9 AC 3-4)
# ---------------------------------------------------------------------------


class TestFallbackChain:
    """When engines fail to instantiate, the fallback chain kicks in."""

    def test_primary_succeeds(self, manager):
        """Normal case: first engine succeeds, no fallback needed."""
        engine, engine_type = manager._create_random_engine_with_fallback()
        assert engine is not None
        assert engine_type in _RANDOM_POOL_ENGINES

    def test_primary_fails_tries_next(self, manager):
        """If the primary engine fails, the next in the pool is tried."""
        call_count = 0
        original_create = None

        def mock_create(engine_type, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Simulated engine failure")
            # For subsequent calls, return a mock engine
            mock_engine = MagicMock()
            mock_engine.is_client_side = False
            return mock_engine

        with patch("video.visualizer_manager.create_engine", side_effect=mock_create):
            engine, engine_type = manager._create_random_engine_with_fallback()

        assert engine is not None
        assert call_count >= 2  # First failed, second succeeded

    def test_all_fail_dvd_fallback(self, manager):
        """If all pool engines fail, DVD fallback is used (Req 9 AC 4)."""

        def mock_create(engine_type, **kwargs):
            if engine_type == "dvd":
                mock_engine = MagicMock()
                mock_engine.is_client_side = True
                return mock_engine
            raise RuntimeError(f"Simulated failure for {engine_type}")

        with patch("video.visualizer_manager.create_engine", side_effect=mock_create):
            engine, engine_type = manager._create_random_engine_with_fallback()

        assert engine_type == "dvd"
        assert engine is not None

    def test_empty_pool_dvd_fallback(self, manager):
        """If the pool is empty, DVD is used."""
        manager._RANDOM_POOL_ENGINES = []

        engine, engine_type = manager._create_random_engine_with_fallback()
        assert engine_type == "dvd"


# ---------------------------------------------------------------------------
# GPU unavailable behaviour (Req 10 AC 4)
# ---------------------------------------------------------------------------


class TestGPUUnavailable:
    """When GPU is unavailable, server-rendered engines are disabled."""

    def test_no_gpu_engines_excluded(self):
        """No GPU → only dvd and off are available for autocomplete."""
        engines = get_available_engines(gpu_available=False)
        gpu_engines = {"projectm", "audiovis", "fosfora", "varda", "native"}
        for ge in gpu_engines:
            assert ge not in engines

    def test_no_gpu_random_not_available(self):
        """No GPU → 'random' meta-entry is not offered."""
        engines = get_available_engines(gpu_available=False)
        assert "random" not in engines

    def test_set_gpu_available_updates_state(self):
        """set_gpu_available() correctly updates module state."""
        set_gpu_available(True)
        engines = get_available_engines()
        assert "projectm" in engines

        set_gpu_available(False)
        engines = get_available_engines()
        assert "projectm" not in engines
