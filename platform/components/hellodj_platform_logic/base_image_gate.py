"""Non-Nix base-image gate for the deployment build stage.

This module holds the pure decision function that the Beta -> Gamma -> Prod
pipeline's build stage invokes to reject any container image whose base was not
produced by the Nix build system. It is imported by both the CDK infrastructure
layer (the build-stage gate step) and any runtime tooling, so infrastructure
and tooling share a single source of truth, and it makes no live AWS calls so
the correctness property can exercise it directly.

Implemented here:

* :func:`check_base` — Property 6 / R5.4. Given a
  :class:`~hellodj_platform_logic.types.BaseImageDescriptor`, accept the image
  if and only if its base was produced by the Nix build system, and reject it
  otherwise (for example an Ubuntu or Debian base). Well-known non-Nix bases
  (``ubuntu``, ``debian``) are rejected regardless of the ``nix_produced``
  flag, so a mislabelled descriptor cannot slip a Debian/Ubuntu base past the
  gate.

Design references:
    * Deployment Pipeline build-stage base-image gate (R5.4)
    * Correctness Property 6: Non-Nix base-image gate

Requirements: 5.1, 5.2, 5.3, 5.4
"""

from __future__ import annotations

from dataclasses import dataclass

from hellodj_platform_logic.types import BaseImageDescriptor

__all__ = [
    "FORBIDDEN_BASE_NAMES",
    "BaseImageGateResult",
    "check_base",
]

#: Well-known non-Nix base names that are rejected regardless of the descriptor's
#: ``nix_produced`` flag (R5.2, R5.3). Matching is case-insensitive and also
#: matches tagged/qualified names such as ``ubuntu:22.04`` or
#: ``docker.io/library/debian``.
FORBIDDEN_BASE_NAMES: frozenset[str] = frozenset({"ubuntu", "debian"})


@dataclass(frozen=True)
class BaseImageGateResult:
    """Outcome of the base-image gate for a single image (Property 6).

    ``accepted`` is True exactly when the image base was produced by the Nix
    build system and is not a forbidden (Ubuntu/Debian) base. When the image is
    rejected, ``reason`` carries a short human-readable explanation for the
    build log; when accepted, ``reason`` is an empty string.
    """

    accepted: bool
    reason: str = ""


def _matches_forbidden_base(base_name: str) -> bool:
    """Return whether ``base_name`` names a forbidden Ubuntu/Debian base.

    The comparison is case-insensitive and tolerant of registry prefixes and
    tags/digests, so ``Ubuntu``, ``ubuntu:22.04`` and
    ``docker.io/library/debian@sha256:...`` all match. Only the final path
    segment's leading identifier (before any ``:`` tag or ``@`` digest) is
    considered.
    """
    # Strip any registry/repository prefix, then any tag or digest suffix.
    final_segment = base_name.strip().rsplit("/", maxsplit=1)[-1]
    identifier = final_segment.split("@", maxsplit=1)[0]
    identifier = identifier.split(":", maxsplit=1)[0]
    return identifier.casefold() in FORBIDDEN_BASE_NAMES


def check_base(image_descriptor: BaseImageDescriptor) -> BaseImageGateResult:
    """Accept or reject a container image by its base descriptor.

    Implements Property 6 / R5.4. The image is accepted **iff** its base was
    produced by the Nix build system (``nix_produced`` is True) and its
    ``base_name`` is not a well-known non-Nix base (Ubuntu/Debian). A forbidden
    base name is rejected even if ``nix_produced`` is mistakenly True, so an
    Ubuntu/Debian base can never pass the gate (R5.2, R5.3).

    Args:
        image_descriptor: The base descriptor of the container image to gate.

    Returns:
        A :class:`BaseImageGateResult` whose ``accepted`` field is True only for
        Nix-produced, non-forbidden bases and False otherwise, with a
        build-log-friendly ``reason`` when rejected.
    """
    if _matches_forbidden_base(image_descriptor.base_name):
        return BaseImageGateResult(
            accepted=False,
            reason=(
                f"base image '{image_descriptor.base_name}' is a non-Nix "
                "base (Ubuntu/Debian bases are not permitted)"
            ),
        )
    if not image_descriptor.nix_produced:
        return BaseImageGateResult(
            accepted=False,
            reason=(
                f"base image '{image_descriptor.base_name}' was not produced "
                "by the Nix build system"
            ),
        )
    return BaseImageGateResult(accepted=True)
