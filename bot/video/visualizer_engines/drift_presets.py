"""Drift Engine preset system with crossfade interpolation.

Provides factory presets for the Drift multipass feedback visualizer,
linear interpolation between presets over configurable duration, and
auto-advance logic triggered by track changes or timed intervals.

Each preset is a Python dict defining warp parameters, decay factor,
composite configuration, bloom settings, and optional shader name.

Usage:
    manager = DriftPresetManager(presets=DRIFT_FACTORY_PRESETS)
    manager.advance()                # Start crossfading to next preset
    manager.on_track_change()        # Auto-advance on track change
    current = manager.get_current()  # Returns interpolated preset dict
"""

from __future__ import annotations

import logging
import time
from copy import deepcopy
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Preset data model
# ---------------------------------------------------------------------------

DriftPreset = dict[str, Any]
"""Type alias for a Drift preset configuration dict.

Structure:
    name: str — human-readable preset name
    warp: dict — warp mesh parameters (zoom, rotation, displacement)
    decay: float — frame decay factor (0.0–1.0, higher = longer trails)
    composite: dict — visual element config (wave, ring, particles, colors)
    bloom: dict — bloom post-process config (intensity)
    shader: str | None — optional per-pixel composite shader name
"""


# ---------------------------------------------------------------------------
# Factory presets (12 distinct aesthetics)
# ---------------------------------------------------------------------------

DRIFT_FACTORY_PRESETS: list[DriftPreset] = [
    {
        "name": "Cosmic Drift",
        "warp": {
            "zoom_base": 1.005,
            "zoom_bass": 0.02,
            "zoom_beat": 0.04,
            "rot_base": 0.003,
            "rot_mids": 0.008,
            "warp_x_freq": 2.0,
            "warp_y_freq": 3.0,
            "warp_amplitude": 0.006,
        },
        "decay": 0.965,
        "composite": {
            "wave_enabled": True,
            "wave_thickness": 3.0,
            "wave_color": [0.2, 0.7, 1.0],
            "ring_enabled": True,
            "ring_radius": 0.25,
            "ring_glow": 1.2,
            "particles_enabled": True,
            "particle_count": 40,
            "particle_size": 5.0,
        },
        "bloom": {"intensity": 0.35},
        "shader": None,
    },
    {
        "name": "Neon Pulse",
        "warp": {
            "zoom_base": 1.01,
            "zoom_bass": 0.04,
            "zoom_beat": 0.08,
            "rot_base": 0.0,
            "rot_mids": 0.002,
            "warp_x_freq": 1.0,
            "warp_y_freq": 1.0,
            "warp_amplitude": 0.003,
        },
        "decay": 0.94,
        "composite": {
            "wave_enabled": True,
            "wave_thickness": 5.0,
            "wave_color": [1.0, 0.1, 0.6],
            "ring_enabled": True,
            "ring_radius": 0.35,
            "ring_glow": 2.0,
            "particles_enabled": True,
            "particle_count": 60,
            "particle_size": 4.0,
        },
        "bloom": {"intensity": 0.5},
        "shader": None,
    },
    {
        "name": "Deep Ocean",
        "warp": {
            "zoom_base": 1.002,
            "zoom_bass": 0.01,
            "zoom_beat": 0.02,
            "rot_base": 0.001,
            "rot_mids": 0.004,
            "warp_x_freq": 0.8,
            "warp_y_freq": 1.2,
            "warp_amplitude": 0.012,
        },
        "decay": 0.98,
        "composite": {
            "wave_enabled": True,
            "wave_thickness": 2.0,
            "wave_color": [0.0, 0.4, 0.8],
            "ring_enabled": False,
            "ring_radius": 0.2,
            "ring_glow": 0.8,
            "particles_enabled": True,
            "particle_count": 25,
            "particle_size": 6.0,
        },
        "bloom": {"intensity": 0.25},
        "shader": None,
    },
    {
        "name": "Solar Flare",
        "warp": {
            "zoom_base": 1.015,
            "zoom_bass": 0.05,
            "zoom_beat": 0.1,
            "rot_base": 0.005,
            "rot_mids": 0.015,
            "warp_x_freq": 3.0,
            "warp_y_freq": 2.5,
            "warp_amplitude": 0.01,
        },
        "decay": 0.92,
        "composite": {
            "wave_enabled": True,
            "wave_thickness": 4.0,
            "wave_color": [1.0, 0.5, 0.0],
            "ring_enabled": True,
            "ring_radius": 0.3,
            "ring_glow": 1.8,
            "particles_enabled": True,
            "particle_count": 80,
            "particle_size": 3.0,
        },
        "bloom": {"intensity": 0.55},
        "shader": None,
    },
    {
        "name": "Ethereal Mist",
        "warp": {
            "zoom_base": 1.001,
            "zoom_bass": 0.005,
            "zoom_beat": 0.01,
            "rot_base": 0.002,
            "rot_mids": 0.003,
            "warp_x_freq": 0.5,
            "warp_y_freq": 0.7,
            "warp_amplitude": 0.015,
        },
        "decay": 0.985,
        "composite": {
            "wave_enabled": True,
            "wave_thickness": 1.5,
            "wave_color": [0.6, 0.3, 0.9],
            "ring_enabled": False,
            "ring_radius": 0.15,
            "ring_glow": 0.6,
            "particles_enabled": True,
            "particle_count": 15,
            "particle_size": 8.0,
        },
        "bloom": {"intensity": 0.45},
        "shader": None,
    },
    {
        "name": "Hyperdrive",
        "warp": {
            "zoom_base": 1.025,
            "zoom_bass": 0.06,
            "zoom_beat": 0.12,
            "rot_base": 0.0,
            "rot_mids": 0.0,
            "warp_x_freq": 0.0,
            "warp_y_freq": 0.0,
            "warp_amplitude": 0.0,
        },
        "decay": 0.96,
        "composite": {
            "wave_enabled": False,
            "wave_thickness": 2.0,
            "wave_color": [1.0, 1.0, 1.0],
            "ring_enabled": True,
            "ring_radius": 0.1,
            "ring_glow": 2.5,
            "particles_enabled": True,
            "particle_count": 100,
            "particle_size": 2.0,
        },
        "bloom": {"intensity": 0.6},
        "shader": None,
    },
    {
        "name": "Acid Rain",
        "warp": {
            "zoom_base": 1.008,
            "zoom_bass": 0.03,
            "zoom_beat": 0.05,
            "rot_base": -0.004,
            "rot_mids": 0.01,
            "warp_x_freq": 4.0,
            "warp_y_freq": 5.0,
            "warp_amplitude": 0.008,
        },
        "decay": 0.955,
        "composite": {
            "wave_enabled": True,
            "wave_thickness": 2.5,
            "wave_color": [0.3, 1.0, 0.2],
            "ring_enabled": True,
            "ring_radius": 0.28,
            "ring_glow": 1.5,
            "particles_enabled": True,
            "particle_count": 50,
            "particle_size": 3.5,
        },
        "bloom": {"intensity": 0.4},
        "shader": None,
    },
    {
        "name": "Velvet Void",
        "warp": {
            "zoom_base": 1.003,
            "zoom_bass": 0.015,
            "zoom_beat": 0.03,
            "rot_base": 0.006,
            "rot_mids": 0.012,
            "warp_x_freq": 1.5,
            "warp_y_freq": 2.0,
            "warp_amplitude": 0.005,
        },
        "decay": 0.975,
        "composite": {
            "wave_enabled": True,
            "wave_thickness": 2.0,
            "wave_color": [0.8, 0.1, 0.3],
            "ring_enabled": True,
            "ring_radius": 0.2,
            "ring_glow": 1.0,
            "particles_enabled": False,
            "particle_count": 20,
            "particle_size": 6.0,
        },
        "bloom": {"intensity": 0.3},
        "shader": None,
    },
    {
        "name": "Crystal Cavern",
        "warp": {
            "zoom_base": 1.004,
            "zoom_bass": 0.01,
            "zoom_beat": 0.025,
            "rot_base": -0.002,
            "rot_mids": 0.006,
            "warp_x_freq": 6.0,
            "warp_y_freq": 4.0,
            "warp_amplitude": 0.004,
        },
        "decay": 0.97,
        "composite": {
            "wave_enabled": True,
            "wave_thickness": 1.0,
            "wave_color": [0.4, 0.9, 0.9],
            "ring_enabled": True,
            "ring_radius": 0.32,
            "ring_glow": 1.4,
            "particles_enabled": True,
            "particle_count": 35,
            "particle_size": 4.5,
        },
        "bloom": {"intensity": 0.38},
        "shader": None,
    },
    {
        "name": "Molten Core",
        "warp": {
            "zoom_base": 1.012,
            "zoom_bass": 0.04,
            "zoom_beat": 0.07,
            "rot_base": 0.004,
            "rot_mids": 0.01,
            "warp_x_freq": 2.5,
            "warp_y_freq": 1.5,
            "warp_amplitude": 0.009,
        },
        "decay": 0.945,
        "composite": {
            "wave_enabled": True,
            "wave_thickness": 4.5,
            "wave_color": [1.0, 0.3, 0.0],
            "ring_enabled": True,
            "ring_radius": 0.22,
            "ring_glow": 1.6,
            "particles_enabled": True,
            "particle_count": 70,
            "particle_size": 3.0,
        },
        "bloom": {"intensity": 0.48},
        "shader": None,
    },
    {
        "name": "Frozen Aurora",
        "warp": {
            "zoom_base": 1.002,
            "zoom_bass": 0.008,
            "zoom_beat": 0.015,
            "rot_base": 0.001,
            "rot_mids": 0.005,
            "warp_x_freq": 1.0,
            "warp_y_freq": 3.5,
            "warp_amplitude": 0.018,
        },
        "decay": 0.982,
        "composite": {
            "wave_enabled": True,
            "wave_thickness": 2.0,
            "wave_color": [0.1, 0.9, 0.5],
            "ring_enabled": False,
            "ring_radius": 0.18,
            "ring_glow": 0.9,
            "particles_enabled": True,
            "particle_count": 20,
            "particle_size": 7.0,
        },
        "bloom": {"intensity": 0.32},
        "shader": None,
    },
    {
        "name": "Synthwave Sunset",
        "warp": {
            "zoom_base": 1.007,
            "zoom_bass": 0.025,
            "zoom_beat": 0.06,
            "rot_base": 0.0,
            "rot_mids": 0.003,
            "warp_x_freq": 2.0,
            "warp_y_freq": 0.5,
            "warp_amplitude": 0.007,
        },
        "decay": 0.958,
        "composite": {
            "wave_enabled": True,
            "wave_thickness": 3.5,
            "wave_color": [1.0, 0.2, 0.8],
            "ring_enabled": True,
            "ring_radius": 0.4,
            "ring_glow": 1.8,
            "particles_enabled": True,
            "particle_count": 45,
            "particle_size": 4.0,
        },
        "bloom": {"intensity": 0.42},
        "shader": None,
    },
]


# ---------------------------------------------------------------------------
# Interpolation utilities
# ---------------------------------------------------------------------------


def _lerp(a: float, b: float, t: float) -> float:
    """Linear interpolation between two scalar values."""
    return a + (b - a) * t


def _lerp_color(a: list[float], b: list[float], t: float) -> list[float]:
    """Linear interpolation between two RGB color lists."""
    return [_lerp(a[i], b[i], t) for i in range(min(len(a), len(b)))]


def _lerp_bool(a: bool, b: bool, t: float) -> bool:
    """Interpolate booleans — switch at midpoint."""
    return b if t >= 0.5 else a


def interpolate_presets(
    preset_a: DriftPreset,
    preset_b: DriftPreset,
    t: float,
) -> DriftPreset:
    """Linearly interpolate between two presets.

    Args:
        preset_a: Source preset (t=0).
        preset_b: Target preset (t=1).
        t: Interpolation factor, clamped to [0, 1].

    Returns:
        A new preset dict with all numeric values interpolated and
        booleans switched at the midpoint.
    """
    t = max(0.0, min(1.0, t))

    # Interpolate warp params
    warp_a = preset_a.get("warp", {})
    warp_b = preset_b.get("warp", {})
    warp_keys = set(warp_a.keys()) | set(warp_b.keys())
    warp = {}
    for key in warp_keys:
        va = warp_a.get(key, 0.0)
        vb = warp_b.get(key, 0.0)
        warp[key] = _lerp(va, vb, t)

    # Interpolate decay
    decay_a = preset_a.get("decay", 0.965)
    decay_b = preset_b.get("decay", 0.965)
    decay = _lerp(decay_a, decay_b, t)

    # Interpolate composite
    comp_a = preset_a.get("composite", {})
    comp_b = preset_b.get("composite", {})
    composite: dict[str, Any] = {}

    # Numeric fields
    for key in ("wave_thickness", "ring_radius", "ring_glow",
                "particle_count", "particle_size"):
        va = comp_a.get(key, 0.0)
        vb = comp_b.get(key, 0.0)
        composite[key] = _lerp(va, vb, t)

    # Boolean fields
    for key in ("wave_enabled", "ring_enabled", "particles_enabled"):
        va = comp_a.get(key, True)
        vb = comp_b.get(key, True)
        composite[key] = _lerp_bool(va, vb, t)

    # Color fields
    composite["wave_color"] = _lerp_color(
        comp_a.get("wave_color", [1.0, 1.0, 1.0]),
        comp_b.get("wave_color", [1.0, 1.0, 1.0]),
        t,
    )

    # Interpolate bloom
    bloom_a = preset_a.get("bloom", {})
    bloom_b = preset_b.get("bloom", {})
    bloom = {
        "intensity": _lerp(
            bloom_a.get("intensity", 0.3),
            bloom_b.get("intensity", 0.3),
            t,
        )
    }

    # Shader: use target's shader once past midpoint
    shader_a = preset_a.get("shader")
    shader_b = preset_b.get("shader")
    shader = shader_b if t >= 0.5 else shader_a

    # Name: target name once transition is complete
    name = preset_b.get("name", "Unknown") if t >= 1.0 else preset_a.get("name", "Unknown")

    return {
        "name": name,
        "warp": warp,
        "decay": decay,
        "composite": composite,
        "bloom": bloom,
        "shader": shader,
    }


# ---------------------------------------------------------------------------
# Preset Manager — handles crossfade and auto-advance
# ---------------------------------------------------------------------------

# Default crossfade duration in seconds
DEFAULT_CROSSFADE_DURATION: float = 3.0

# Default auto-advance interval in seconds (0 = disabled, only on track change)
DEFAULT_AUTO_ADVANCE_INTERVAL: float = 45.0


class DriftPresetManager:
    """Manages preset crossfading and auto-advance for the Drift engine.

    The manager holds an ordered list of presets and smoothly interpolates
    between them over a configurable duration. Auto-advance can be triggered
    by track changes and/or a timed interval.

    Args:
        presets: List of preset dicts to cycle through.
            Defaults to DRIFT_FACTORY_PRESETS.
        crossfade_duration: Crossfade duration in seconds (default 3.0).
        auto_advance_interval: Seconds between auto-advances (0 = disabled).
            Only applies to timed advance; track-change advance always works.
        shuffle: If True, randomize preset order on construction.
    """

    def __init__(
        self,
        presets: list[DriftPreset] | None = None,
        crossfade_duration: float = DEFAULT_CROSSFADE_DURATION,
        auto_advance_interval: float = DEFAULT_AUTO_ADVANCE_INTERVAL,
        shuffle: bool = False,
    ) -> None:
        self._presets: list[DriftPreset] = deepcopy(
            presets if presets is not None else DRIFT_FACTORY_PRESETS
        )
        if not self._presets:
            raise ValueError("At least one preset is required")

        if shuffle:
            import random
            random.shuffle(self._presets)

        self._crossfade_duration: float = max(0.1, crossfade_duration)
        self._auto_advance_interval: float = max(0.0, auto_advance_interval)

        # Current state
        self._current_index: int = 0
        self._target_index: int | None = None
        self._transition_start: float | None = None
        self._last_advance_time: float = time.monotonic()

    @property
    def current_preset_name(self) -> str:
        """Name of the currently active (or transitioning-from) preset."""
        return self._presets[self._current_index].get("name", "Unknown")

    @property
    def target_preset_name(self) -> str | None:
        """Name of the preset being transitioned to, or None if idle."""
        if self._target_index is None:
            return None
        return self._presets[self._target_index].get("name", "Unknown")

    @property
    def is_transitioning(self) -> bool:
        """Whether a crossfade transition is currently in progress."""
        return self._target_index is not None

    @property
    def preset_count(self) -> int:
        """Total number of presets in the cycle."""
        return len(self._presets)

    @property
    def crossfade_duration(self) -> float:
        """Current crossfade duration in seconds."""
        return self._crossfade_duration

    @crossfade_duration.setter
    def crossfade_duration(self, value: float) -> None:
        """Set crossfade duration (minimum 0.1s)."""
        self._crossfade_duration = max(0.1, value)

    @property
    def auto_advance_interval(self) -> float:
        """Current auto-advance interval in seconds (0 = disabled)."""
        return self._auto_advance_interval

    @auto_advance_interval.setter
    def auto_advance_interval(self, value: float) -> None:
        """Set auto-advance interval (0 disables timed advance)."""
        self._auto_advance_interval = max(0.0, value)

    def advance(self, target_index: int | None = None) -> None:
        """Start a crossfade transition to the next (or specified) preset.

        If a transition is already in progress, it completes immediately
        before starting the new one.

        Args:
            target_index: Specific preset index to transition to.
                If None, advances to the next preset in sequence.
        """
        # Complete any in-progress transition
        if self._target_index is not None:
            self._current_index = self._target_index
            self._target_index = None
            self._transition_start = None

        # Determine target
        if target_index is not None:
            next_idx = target_index % len(self._presets)
        else:
            next_idx = (self._current_index + 1) % len(self._presets)

        # Don't transition to the same preset
        if next_idx == self._current_index:
            return

        self._target_index = next_idx
        self._transition_start = time.monotonic()
        self._last_advance_time = time.monotonic()

        log.debug(
            "Drift preset transition: %s → %s (%.1fs)",
            self._presets[self._current_index].get("name"),
            self._presets[next_idx].get("name"),
            self._crossfade_duration,
        )

    def on_track_change(self) -> None:
        """Trigger a preset advance on track change.

        Always advances regardless of auto_advance_interval setting.
        """
        self.advance()

    def tick(self) -> None:
        """Check if a timed auto-advance should fire.

        Call this once per frame (~30fps). If auto_advance_interval > 0
        and enough time has elapsed since the last advance, triggers
        a new transition.
        """
        if self._auto_advance_interval <= 0:
            return

        now = time.monotonic()
        elapsed_since_advance = now - self._last_advance_time

        if elapsed_since_advance >= self._auto_advance_interval:
            if not self.is_transitioning:
                self.advance()

    def get_current(self) -> DriftPreset:
        """Get the current interpolated preset.

        During a transition, returns a blend of source and target presets.
        When idle, returns the current preset unchanged.

        Returns:
            A preset dict (potentially interpolated).
        """
        if self._target_index is None or self._transition_start is None:
            return deepcopy(self._presets[self._current_index])

        # Calculate interpolation factor
        now = time.monotonic()
        elapsed = now - self._transition_start
        t = min(1.0, elapsed / self._crossfade_duration)

        if t >= 1.0:
            # Transition complete
            self._current_index = self._target_index
            self._target_index = None
            self._transition_start = None
            return deepcopy(self._presets[self._current_index])

        # Interpolate between source and target
        source = self._presets[self._current_index]
        target = self._presets[self._target_index]
        return interpolate_presets(source, target, t)

    def get_preset_names(self) -> list[str]:
        """Return ordered list of all preset names."""
        return [p.get("name", "Unknown") for p in self._presets]

    def set_preset_by_name(self, name: str) -> bool:
        """Jump directly to a named preset (no crossfade).

        Args:
            name: Preset name to switch to.

        Returns:
            True if preset was found and switched, False otherwise.
        """
        for i, preset in enumerate(self._presets):
            if preset.get("name") == name:
                # Complete any in-progress transition
                if self._target_index is not None:
                    self._target_index = None
                    self._transition_start = None
                self._current_index = i
                self._last_advance_time = time.monotonic()
                return True
        return False

    def crossfade_to_name(self, name: str) -> bool:
        """Start a crossfade to a named preset.

        Args:
            name: Preset name to crossfade to.

        Returns:
            True if preset was found and transition started, False otherwise.
        """
        for i, preset in enumerate(self._presets):
            if preset.get("name") == name:
                self.advance(target_index=i)
                return True
        return False
