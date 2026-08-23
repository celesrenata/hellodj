"""Visualizer engine registry and factory.

This module maintains a mapping of engine type strings to their implementation
classes. Use ``create_engine()`` to instantiate an engine by name, and
``get_available_engines()`` to discover which engines are usable given
current GPU availability.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .audiovis import AudioVisEngine
from .base import AudioFeatures, TrackMetadata, VisualizerRenderer
from .dvd import DVDEngine
from .fosfora import FosforaEngine
from .native import NativeEngine
from .projectm import ProjectMEngine
from .varda import VardaEngine

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

__all__ = [
    "AudioFeatures",
    "AudioVisEngine",
    "DVDEngine",
    "FosforaEngine",
    "NativeEngine",
    "ProjectMEngine",
    "TrackMetadata",
    "VardaEngine",
    "VisualizerRenderer",
    "create_engine",
    "get_available_engines",
    "ENGINE_REGISTRY",
]

# Engine name → class mapping.  Engines are registered as they are implemented.
# Client-side engines (e.g. "dvd") and server-rendered engines (e.g. "native")
# are treated identically by the factory — the caller inspects ``is_client_side``
# to determine rendering strategy.
ENGINE_REGISTRY: dict[str, type[VisualizerRenderer]] = {
    "audiovis": AudioVisEngine,
    "dvd": DVDEngine,
    "fosfora": FosforaEngine,
    "native": NativeEngine,
    "projectm": ProjectMEngine,
    "varda": VardaEngine,
}

# Engines eligible for "random" mode selection (all GPU-accelerated engines).
_RANDOM_POOL_ENGINES: list[str] = ["projectm", "audiovis", "fosfora", "varda"]

# Engines that require a GPU (server-rendered). When no GPU is detected,
# these are excluded from the available set.
_GPU_REQUIRED_ENGINES: set[str] = {"projectm", "audiovis", "fosfora", "varda", "native"}


def get_available_engines(*, gpu_available: bool | None = None) -> list[str]:
    """Return the list of engine names currently usable.

    Consults the GPUProbe (via the Video cog's shared instance) to determine
    whether server-rendered engines are feasible. When no GPU is available,
    only client-side engines (e.g. "dvd") are returned.

    The returned list also includes the meta-entries "random" and "off" when
    at least one GPU engine is available (for "random") or always (for "off").

    Args:
        gpu_available: Explicit GPU availability override. If None, attempts
            to read from the module-level ``_gpu_available`` flag (set by
            ``set_gpu_available()`` at startup).

    Returns:
        Sorted list of engine name strings usable for the ``/visualizer engine``
        command autocomplete.
    """
    if gpu_available is None:
        gpu_available = _gpu_available

    available: list[str] = []

    for engine_name in ENGINE_REGISTRY:
        if engine_name in _GPU_REQUIRED_ENGINES and not gpu_available:
            continue
        available.append(engine_name)

    # Add "off" always
    available.append("off")

    # Add "random" only when at least one GPU engine is usable
    if gpu_available and any(e in available for e in _RANDOM_POOL_ENGINES):
        available.append("random")

    return sorted(available)


# Module-level GPU state, updated by set_gpu_available() after GPUProbe runs.
_gpu_available: bool = False


def set_gpu_available(available: bool) -> None:
    """Update the module-level GPU availability flag.

    Called by the Video cog after GPUProbe.probe() completes at startup.
    This allows get_available_engines() to work without requiring a probe
    reference to be passed around.
    """
    global _gpu_available
    _gpu_available = available
    log.info("Visualizer engine registry: GPU available = %s", available)


def create_engine(engine_type: str, **kwargs: object) -> VisualizerRenderer:
    """Instantiate a visualizer engine by its registered name.

    Args:
        engine_type: The engine identifier (e.g. "dvd", "native", "projectm").
        **kwargs: Engine-specific constructor arguments.

    Returns:
        A configured VisualizerRenderer instance ready for ``initialize()``.

    Raises:
        ValueError: If the engine_type is not found in the registry.
    """
    cls = ENGINE_REGISTRY.get(engine_type)
    if cls is None:
        available = ", ".join(sorted(ENGINE_REGISTRY.keys())) or "(none)"
        raise ValueError(
            f"Unknown visualizer engine {engine_type!r}. "
            f"Available engines: {available}"
        )
    return cls(**kwargs)  # type: ignore[arg-type]
