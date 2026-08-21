"""Visualizer engine registry and factory.

This module maintains a mapping of engine type strings to their implementation
classes. Use ``create_engine()`` to instantiate an engine by name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .audiovis import AudioVisEngine
from .base import AudioFeatures, TrackMetadata, VisualizerRenderer
from .dvd import DVDEngine
from .fosfora import FosforaEngine
from .native import NativeEngine
from .projectm import ProjectMEngine
from .varda import VardaEngine
from .vgalizer import VgalizerEngine

if TYPE_CHECKING:
    pass

__all__ = [
    "AudioFeatures",
    "AudioVisEngine",
    "DVDEngine",
    "FosforaEngine",
    "NativeEngine",
    "ProjectMEngine",
    "TrackMetadata",
    "VardaEngine",
    "VgalizerEngine",
    "VisualizerRenderer",
    "create_engine",
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
    "vgalizer": VgalizerEngine,
}


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
