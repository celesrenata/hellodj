"""Factory presets for GPU visualizer engines.

Provides curated preset configurations for each engine. Factory presets
are immutable and always available. User presets can shadow factory names
but cannot delete them.
"""

from __future__ import annotations

from typing import Any


# Each preset entry: {"engine": str, "config": dict, "factory": True}
FACTORY_PRESETS: dict[str, dict[str, Any]] = {
    # --- projectM presets (Milkdrop category-based) ---
    "milkdrop-classic": {
        "engine": "projectm",
        "config": {"preset_category": "Classic"},
        "factory": True,
    },
    "psychedelic": {
        "engine": "projectm",
        "config": {"preset_category": "Abstract", "brightness": 1.5},
        "factory": True,
    },
    "chill": {
        "engine": "projectm",
        "config": {
            "preset_category": "Fluid Motion",
            "blend_duration": 5.0,
            "sensitivity": 0.7,
        },
        "factory": True,
    },
    "trippy": {
        "engine": "projectm",
        "config": {"preset_category": "Trippy", "preset_duration": 15.0, "brightness": 1.3},
        "factory": True,
    },
    "geometric": {
        "engine": "projectm",
        "config": {"preset_category": "Geometric"},
        "factory": True,
    },
    "space": {
        "engine": "projectm",
        "config": {"preset_category": "Space", "blend_duration": 4.0},
        "factory": True,
    },
    "energy": {
        "engine": "projectm",
        "config": {"preset_category": "Energy", "sensitivity": 1.5, "preset_duration": 15.0},
        "factory": True,
    },
    "minimal": {
        "engine": "projectm",
        "config": {"preset_category": "Simple", "brightness": 0.8},
        "factory": True,
    },
    # --- audiovis presets ---
    "spectrum-bars": {
        "engine": "audiovis",
        "config": {
            "style": "bars",
            "color_scheme": "neon",
            "fft_bins": 7,
            "glow_intensity": 0.6,
        },
        "factory": True,
    },
    "full-spectrum": {
        "engine": "audiovis",
        "config": {
            "style": "bars",
            "fft_bins": 128,
            "glow_intensity": 0.8,
            "background_opacity": 1.0,
        },
        "factory": True,
    },
    "waveform": {
        "engine": "audiovis",
        "config": {"style": "waveform", "glow_intensity": 0.4},
        "factory": True,
    },
    "waterfall": {
        "engine": "audiovis",
        "config": {"style": "waterfall", "fft_bins": 64, "background_opacity": 1.0},
        "factory": True,
    },
    "circular": {
        "engine": "audiovis",
        "config": {"style": "circular", "fft_bins": 32, "glow_intensity": 0.7},
        "factory": True,
    },
    "vinyl": {
        "engine": "audiovis",
        "config": {"style": "circular", "color_scheme": "warm", "glow_intensity": 0.3},
        "factory": True,
    },
    "neon-city": {
        "engine": "audiovis",
        "config": {
            "style": "bars",
            "color_scheme": "synthwave",
            "fft_bins": 32,
            "glow_intensity": 0.9,
        },
        "factory": True,
    },
    # --- fosfora presets (particle system) ---
    "stardust": {
        "engine": "fosfora",
        "config": {
            "particle_count": 3000,
            "gravity": 0.3,
            "emission_style": "rain",
            "trail_length": 0.5,
        },
        "factory": True,
    },
    "fireworks": {
        "engine": "fosfora",
        "config": {
            "particle_count": 5000,
            "gravity": 1.0,
            "emission_style": "burst",
            "trail_length": 0.2,
        },
        "factory": True,
    },
    "aurora": {
        "engine": "fosfora",
        "config": {
            "particle_count": 4000,
            "gravity": 0.1,
            "emission_style": "stream",
            "color_mode": "gradient",
            "trail_length": 0.8,
        },
        "factory": True,
    },
    "vortex": {
        "engine": "fosfora",
        "config": {
            "particle_count": 6000,
            "gravity": 0.0,
            "emission_style": "stream",
            "trail_length": 0.6,
        },
        "factory": True,
    },
    "rain": {
        "engine": "fosfora",
        "config": {
            "particle_count": 3000,
            "gravity": 1.5,
            "emission_style": "rain",
            "trail_length": 0.4,
        },
        "factory": True,
    },
    "nebula": {
        "engine": "fosfora",
        "config": {
            "particle_count": 8000,
            "gravity": 0.05,
            "emission_style": "stream",
            "color_mode": "gradient",
            "trail_length": 0.9,
        },
        "factory": True,
    },
    "pulse": {
        "engine": "fosfora",
        "config": {
            "particle_count": 4000,
            "gravity": 0.5,
            "emission_style": "burst",
            "trail_length": 0.1,
        },
        "factory": True,
    },
    # --- varda presets (GLSL shaders) ---
    "fractal-zoom": {
        "engine": "varda",
        "config": {"shader_name": "varda_kaleidoscope", "speed": 0.5, "complexity": "high"},
        "factory": True,
    },
    "tunnel": {
        "engine": "varda",
        "config": {"shader_name": "varda_tunnel", "speed": 1.5, "complexity": "medium"},
        "factory": True,
    },
    "plasma": {
        "engine": "varda",
        "config": {"shader_name": "plasma", "color_intensity": 1.2, "speed": 1.0},
        "factory": True,
    },
    "voronoi-pulse": {
        "engine": "varda",
        "config": {"shader_name": "varda_voronoi", "complexity": "high"},
        "factory": True,
    },
    "raymarched-orbs": {
        "engine": "varda",
        "config": {"shader_name": "varda_rayorbs", "speed": 0.75, "complexity": "high"},
        "factory": True,
    },
    "kaleidoscope": {
        "engine": "varda",
        "config": {"shader_name": "varda_kaleidoscope", "speed": 1.5, "color_intensity": 1.5},
        "factory": True,
    },
    "neon-grid": {
        "engine": "varda",
        "config": {"shader_name": "varda_neon_grid", "speed": 1.0, "color_intensity": 1.3},
        "factory": True,
    },
    "star-field": {
        "engine": "varda",
        "config": {"shader_name": "varda_nebula", "speed": 2.0},
        "factory": True,
    },
    "liquid-metal": {
        "engine": "varda",
        "config": {"shader_name": "varda_liquid", "speed": 0.5, "complexity": "high"},
        "factory": True,
    },
    "cosmic-web": {
        "engine": "varda",
        "config": {"shader_name": "varda_nebula", "complexity": "medium", "color_intensity": 1.1},
        "factory": True,
    },
    # --- dvd presets (client-side bounce) ---
    "classic": {
        "engine": "dvd",
        "config": {"speed": 1.0, "hue_shift": True, "icon_size": 15},
        "factory": True,
    },
    "fast": {
        "engine": "dvd",
        "config": {"speed": 2.5, "hue_shift": True, "icon_size": 15},
        "factory": True,
    },
    "slow": {
        "engine": "dvd",
        "config": {"speed": 0.5, "hue_shift": True, "icon_size": 15},
        "factory": True,
    },
    "no-hue": {
        "engine": "dvd",
        "config": {"speed": 1.0, "hue_shift": False, "icon_size": 15},
        "factory": True,
    },
}


def is_factory_preset(name: str) -> bool:
    """Check if a preset name is a factory preset.

    Args:
        name: Preset name to check.

    Returns:
        True if the name exists in FACTORY_PRESETS.
    """
    return name in FACTORY_PRESETS


def get_factory_preset(name: str) -> dict[str, Any] | None:
    """Get a factory preset by name.

    Args:
        name: Preset name.

    Returns:
        A copy of the preset dict, or None if not found.
    """
    preset = FACTORY_PRESETS.get(name)
    if preset is None:
        return None
    # Return a deep-ish copy to prevent mutation of the factory data
    return {
        "engine": preset["engine"],
        "config": dict(preset["config"]),
        "factory": True,
    }


def list_factory_presets(engine: str | None = None) -> dict[str, dict[str, Any]]:
    """List factory presets, optionally filtered by engine.

    Args:
        engine: If provided, only return presets for this engine.

    Returns:
        Dict mapping preset names to their preset data (copies).
    """
    results: dict[str, dict[str, Any]] = {}
    for name, preset in FACTORY_PRESETS.items():
        if engine is None or preset["engine"] == engine:
            results[name] = {
                "engine": preset["engine"],
                "config": dict(preset["config"]),
                "factory": True,
            }
    return results
