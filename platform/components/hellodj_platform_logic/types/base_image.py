"""Base-image gate types (Property 6, R5).

Requirements: 5.4
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "BaseImageDescriptor",
]


@dataclass(frozen=True)
class BaseImageDescriptor:
    """Descriptor of a container image base for the Nix base-image gate.

    The build-stage gate accepts an image if and only if its base was produced
    by the Nix build system (``nix_produced`` is True); an Ubuntu/Debian or any
    other non-Nix base is rejected (Property 6, R5.4).
    """

    base_name: str
    nix_produced: bool
