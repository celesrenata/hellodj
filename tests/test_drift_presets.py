"""Tests for drift_presets.py — preset system with crossfade interpolation."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from video.visualizer_engines.drift_presets import (
    DEFAULT_AUTO_ADVANCE_INTERVAL,
    DEFAULT_CROSSFADE_DURATION,
    DRIFT_FACTORY_PRESETS,
    DriftPresetManager,
    interpolate_presets,
    _lerp,
    _lerp_bool,
    _lerp_color,
)


# ---------------------------------------------------------------------------
# Interpolation unit tests
# ---------------------------------------------------------------------------


class TestLerp:
    """Test basic linear interpolation helpers."""

    def test_lerp_at_zero(self):
        assert _lerp(1.0, 5.0, 0.0) == 1.0

    def test_lerp_at_one(self):
        assert _lerp(1.0, 5.0, 1.0) == 5.0

    def test_lerp_at_midpoint(self):
        assert _lerp(0.0, 10.0, 0.5) == pytest.approx(5.0)

    def test_lerp_color_at_zero(self):
        assert _lerp_color([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], 0.0) == [1.0, 0.0, 0.0]

    def test_lerp_color_at_one(self):
        assert _lerp_color([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], 1.0) == [0.0, 1.0, 0.0]

    def test_lerp_color_midpoint(self):
        result = _lerp_color([0.0, 0.0, 0.0], [1.0, 1.0, 1.0], 0.5)
        assert result == pytest.approx([0.5, 0.5, 0.5])

    def test_lerp_bool_before_midpoint(self):
        assert _lerp_bool(True, False, 0.4) is True

    def test_lerp_bool_at_midpoint(self):
        assert _lerp_bool(True, False, 0.5) is False

    def test_lerp_bool_after_midpoint(self):
        assert _lerp_bool(False, True, 0.6) is True


# ---------------------------------------------------------------------------
# Preset interpolation tests
# ---------------------------------------------------------------------------


class TestInterpolatePresets:
    """Test full preset interpolation between two presets."""

    @pytest.fixture
    def preset_a(self):
        return DRIFT_FACTORY_PRESETS[0]  # Cosmic Drift

    @pytest.fixture
    def preset_b(self):
        return DRIFT_FACTORY_PRESETS[1]  # Neon Pulse

    def test_at_zero_returns_source(self, preset_a, preset_b):
        result = interpolate_presets(preset_a, preset_b, 0.0)
        assert result["warp"]["zoom_base"] == preset_a["warp"]["zoom_base"]
        assert result["decay"] == preset_a["decay"]
        assert result["composite"]["wave_color"] == preset_a["composite"]["wave_color"]

    def test_at_one_returns_target(self, preset_a, preset_b):
        result = interpolate_presets(preset_a, preset_b, 1.0)
        assert result["warp"]["zoom_base"] == preset_b["warp"]["zoom_base"]
        assert result["decay"] == preset_b["decay"]
        assert result["composite"]["wave_color"] == pytest.approx(preset_b["composite"]["wave_color"])

    def test_midpoint_averages_numeric_values(self, preset_a, preset_b):
        result = interpolate_presets(preset_a, preset_b, 0.5)
        expected_zoom = (preset_a["warp"]["zoom_base"] + preset_b["warp"]["zoom_base"]) / 2
        assert result["warp"]["zoom_base"] == pytest.approx(expected_zoom)

    def test_midpoint_averages_decay(self, preset_a, preset_b):
        result = interpolate_presets(preset_a, preset_b, 0.5)
        expected_decay = (preset_a["decay"] + preset_b["decay"]) / 2
        assert result["decay"] == pytest.approx(expected_decay)

    def test_bloom_intensity_interpolates(self, preset_a, preset_b):
        result = interpolate_presets(preset_a, preset_b, 0.5)
        expected = (preset_a["bloom"]["intensity"] + preset_b["bloom"]["intensity"]) / 2
        assert result["bloom"]["intensity"] == pytest.approx(expected)

    def test_clamps_t_below_zero(self, preset_a, preset_b):
        result = interpolate_presets(preset_a, preset_b, -0.5)
        assert result["warp"]["zoom_base"] == preset_a["warp"]["zoom_base"]

    def test_clamps_t_above_one(self, preset_a, preset_b):
        result = interpolate_presets(preset_a, preset_b, 1.5)
        assert result["warp"]["zoom_base"] == preset_b["warp"]["zoom_base"]

    def test_name_uses_source_until_complete(self, preset_a, preset_b):
        result = interpolate_presets(preset_a, preset_b, 0.99)
        assert result["name"] == preset_a["name"]

    def test_name_uses_target_at_one(self, preset_a, preset_b):
        result = interpolate_presets(preset_a, preset_b, 1.0)
        assert result["name"] == preset_b["name"]


# ---------------------------------------------------------------------------
# Preset Manager tests
# ---------------------------------------------------------------------------


class TestDriftPresetManager:
    """Test the DriftPresetManager crossfade and auto-advance logic."""

    def test_initialization_defaults(self):
        mgr = DriftPresetManager()
        assert mgr.preset_count == len(DRIFT_FACTORY_PRESETS)
        assert mgr.current_preset_name == DRIFT_FACTORY_PRESETS[0]["name"]
        assert mgr.target_preset_name is None
        assert mgr.is_transitioning is False

    def test_requires_at_least_one_preset(self):
        with pytest.raises(ValueError, match="At least one preset"):
            DriftPresetManager(presets=[])

    def test_advance_starts_transition(self):
        mgr = DriftPresetManager()
        mgr.advance()
        assert mgr.is_transitioning is True
        assert mgr.target_preset_name == DRIFT_FACTORY_PRESETS[1]["name"]

    def test_advance_wraps_around(self):
        presets = DRIFT_FACTORY_PRESETS[:3]
        mgr = DriftPresetManager(presets=presets)
        # Advance to index 1
        mgr.advance()
        # Complete transition manually
        mgr._current_index = 1
        mgr._target_index = None
        mgr._transition_start = None
        # Advance to index 2
        mgr.advance()
        mgr._current_index = 2
        mgr._target_index = None
        mgr._transition_start = None
        # Advance wraps to index 0
        mgr.advance()
        assert mgr.target_preset_name == presets[0]["name"]

    def test_advance_to_specific_index(self):
        mgr = DriftPresetManager()
        mgr.advance(target_index=5)
        assert mgr.target_preset_name == DRIFT_FACTORY_PRESETS[5]["name"]

    def test_advance_completes_existing_transition(self):
        mgr = DriftPresetManager()
        mgr.advance()  # 0 → 1
        # Advance again should complete 0→1, then start 1→2
        mgr.advance()
        assert mgr.current_preset_name == DRIFT_FACTORY_PRESETS[1]["name"]
        assert mgr.target_preset_name == DRIFT_FACTORY_PRESETS[2]["name"]

    def test_advance_same_index_does_nothing(self):
        mgr = DriftPresetManager()
        mgr.advance(target_index=0)
        assert mgr.is_transitioning is False

    def test_on_track_change_advances(self):
        mgr = DriftPresetManager()
        mgr.on_track_change()
        assert mgr.is_transitioning is True

    def test_get_current_returns_source_when_idle(self):
        mgr = DriftPresetManager()
        result = mgr.get_current()
        assert result["name"] == DRIFT_FACTORY_PRESETS[0]["name"]

    def test_get_current_returns_interpolated_during_transition(self):
        mgr = DriftPresetManager(crossfade_duration=3.0)
        mgr.advance()
        # Simulate being 1.5s into transition (midpoint)
        mgr._transition_start = time.monotonic() - 1.5
        result = mgr.get_current()
        # Should be between preset 0 and preset 1
        src_decay = DRIFT_FACTORY_PRESETS[0]["decay"]
        tgt_decay = DRIFT_FACTORY_PRESETS[1]["decay"]
        expected = (src_decay + tgt_decay) / 2
        assert result["decay"] == pytest.approx(expected, abs=0.01)

    def test_get_current_completes_transition_at_end(self):
        mgr = DriftPresetManager(crossfade_duration=3.0)
        mgr.advance()
        # Simulate being 4s in (past duration)
        mgr._transition_start = time.monotonic() - 4.0
        result = mgr.get_current()
        # Transition should be complete
        assert mgr.is_transitioning is False
        assert result["name"] == DRIFT_FACTORY_PRESETS[1]["name"]

    def test_tick_auto_advances_after_interval(self):
        mgr = DriftPresetManager(auto_advance_interval=1.0)
        # Simulate time passing
        mgr._last_advance_time = time.monotonic() - 2.0
        mgr.tick()
        assert mgr.is_transitioning is True

    def test_tick_does_not_advance_during_transition(self):
        mgr = DriftPresetManager(auto_advance_interval=1.0)
        mgr.advance()  # Start a transition
        mgr._last_advance_time = time.monotonic() - 2.0
        # Tick should not start a new advance (already transitioning)
        target_before = mgr.target_preset_name
        mgr.tick()
        assert mgr.target_preset_name == target_before

    def test_tick_disabled_when_interval_zero(self):
        mgr = DriftPresetManager(auto_advance_interval=0.0)
        mgr._last_advance_time = time.monotonic() - 1000.0
        mgr.tick()
        assert mgr.is_transitioning is False

    def test_set_preset_by_name(self):
        mgr = DriftPresetManager()
        result = mgr.set_preset_by_name("Neon Pulse")
        assert result is True
        assert mgr.current_preset_name == "Neon Pulse"

    def test_set_preset_by_name_not_found(self):
        mgr = DriftPresetManager()
        result = mgr.set_preset_by_name("Nonexistent Preset")
        assert result is False
        assert mgr.current_preset_name == DRIFT_FACTORY_PRESETS[0]["name"]

    def test_crossfade_to_name(self):
        mgr = DriftPresetManager()
        result = mgr.crossfade_to_name("Solar Flare")
        assert result is True
        assert mgr.is_transitioning is True
        assert mgr.target_preset_name == "Solar Flare"

    def test_crossfade_to_name_not_found(self):
        mgr = DriftPresetManager()
        result = mgr.crossfade_to_name("Does Not Exist")
        assert result is False
        assert mgr.is_transitioning is False

    def test_get_preset_names(self):
        mgr = DriftPresetManager()
        names = mgr.get_preset_names()
        assert len(names) == len(DRIFT_FACTORY_PRESETS)
        assert names[0] == "Cosmic Drift"
        assert names[1] == "Neon Pulse"

    def test_crossfade_duration_property(self):
        mgr = DriftPresetManager(crossfade_duration=5.0)
        assert mgr.crossfade_duration == 5.0
        mgr.crossfade_duration = 2.0
        assert mgr.crossfade_duration == 2.0

    def test_crossfade_duration_minimum(self):
        mgr = DriftPresetManager(crossfade_duration=0.01)
        assert mgr.crossfade_duration == 0.1

    def test_auto_advance_interval_property(self):
        mgr = DriftPresetManager(auto_advance_interval=30.0)
        assert mgr.auto_advance_interval == 30.0
        mgr.auto_advance_interval = 60.0
        assert mgr.auto_advance_interval == 60.0

    def test_shuffle_randomizes_order(self):
        # Run a few times — with 12 presets, the chance of getting
        # the same first preset twice in a row is 1/12
        names = set()
        for _ in range(10):
            mgr = DriftPresetManager(shuffle=True)
            names.add(mgr.current_preset_name)
        # Should have gotten at least 2 different starting presets
        assert len(names) >= 2


# ---------------------------------------------------------------------------
# Factory presets integrity tests
# ---------------------------------------------------------------------------


class TestFactoryPresetIntegrity:
    """Verify factory presets have valid structure."""

    @pytest.mark.parametrize("preset", DRIFT_FACTORY_PRESETS)
    def test_preset_has_required_keys(self, preset):
        assert "name" in preset
        assert "warp" in preset
        assert "decay" in preset
        assert "composite" in preset
        assert "bloom" in preset
        assert "shader" in preset

    @pytest.mark.parametrize("preset", DRIFT_FACTORY_PRESETS)
    def test_decay_in_valid_range(self, preset):
        assert 0.0 < preset["decay"] < 1.0

    @pytest.mark.parametrize("preset", DRIFT_FACTORY_PRESETS)
    def test_bloom_intensity_positive(self, preset):
        assert preset["bloom"]["intensity"] > 0.0

    @pytest.mark.parametrize("preset", DRIFT_FACTORY_PRESETS)
    def test_wave_color_is_rgb(self, preset):
        color = preset["composite"]["wave_color"]
        assert len(color) == 3
        for c in color:
            assert 0.0 <= c <= 1.0

    @pytest.mark.parametrize("preset", DRIFT_FACTORY_PRESETS)
    def test_warp_has_all_params(self, preset):
        warp = preset["warp"]
        required = [
            "zoom_base", "zoom_bass", "zoom_beat",
            "rot_base", "rot_mids",
            "warp_x_freq", "warp_y_freq", "warp_amplitude",
        ]
        for key in required:
            assert key in warp, f"Missing warp key: {key}"

    def test_all_presets_have_unique_names(self):
        names = [p["name"] for p in DRIFT_FACTORY_PRESETS]
        assert len(names) == len(set(names))

    def test_at_least_10_presets(self):
        assert len(DRIFT_FACTORY_PRESETS) >= 10
