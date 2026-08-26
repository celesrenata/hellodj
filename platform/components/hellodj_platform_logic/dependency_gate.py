"""Graviton/x86 dependency-compatibility gate decision logic.

This module holds the pure decision function that implements the
Dependency_Compatibility_Gate (Requirement 4, Decision D4). It is imported by
both the deployment-pipeline build stage (task 18.4, which runs the gate per
component to decide ARM64-only vs an x86-64 fallback) and the CDK
infrastructure layer, so infrastructure-as-code and the runtime build share a
single source of truth. It makes no live AWS calls, so the correctness
properties can exercise it directly.

The platform defaults every Component to Graviton_Architecture (ARM64) to
minimize compute cost (R4.1). Before a Component may drop x86-64 support, the
gate must verify that *every* runtime dependency of that Component is available
and functional on ARM64 (R4.2). If any dependency is not ARM64-compatible, the
Component either uses a documented substitute or runs on x86-64 (R4.3); the
gate therefore selects ARM64-only if and only if all dependencies are
ARM64-compatible, and otherwise selects x86-64 while reporting exactly which
dependencies forced that choice (so the x86 requirement can be documented per
R4.4).

The dependency classes the gate is required to cover (R4.5) are the wake word
ONNX runtime, the speech-to-text engine, the audio processing libraries, the
media transcode toolchain, the JVM audio services, and the streaming source
clients. Those class names are exposed as :data:`REQUIRED_DEPENDENCY_CLASSES`
so callers (and tests) can confirm coverage; the gate itself is agnostic to the
particular dependency names supplied and simply reasons over the map it is
given.

Design references:
    * Decision D4 / Requirement 4: CPU architecture (Graviton default with
      dependency verification)
    * Correctness Property 5: Graviton/x86 dependency-gate decision

Requirements: 4.1, 4.2, 4.3, 4.5
"""

from __future__ import annotations

from collections.abc import Mapping

from hellodj_platform_logic.types import CpuArch, GateDecision

__all__ = ["REQUIRED_DEPENDENCY_CLASSES", "gate"]

#: The dependency classes the Dependency_Compatibility_Gate is required to
#: cover (R4.5). These are the runtime dependency categories every Component's
#: ``dep_arm64_map`` is expected to describe when the gate is applied across the
#: fleet; the gate function itself reasons over whatever map it is given.
REQUIRED_DEPENDENCY_CLASSES: tuple[str, ...] = (
    "wakeword_onnx_runtime",
    "stt_engine",
    "audio_libraries",
    "transcode_toolchain",
    "jvm_audio_services",
    "streaming_source_clients",
)


def gate(dep_arm64_map: Mapping[str, bool]) -> GateDecision:
    """Decide the CPU architecture for a Component from its dependency map.

    Implements the Dependency_Compatibility_Gate (Requirement 4, Property 5).
    ``dep_arm64_map`` maps each runtime dependency name to a boolean where
    ``True`` means the dependency is available and functional on
    Graviton_Architecture (ARM64) and ``False`` means it is not.

    The gate returns:

    * :class:`~hellodj_platform_logic.types.CpuArch.ARM64` with an empty
      ``incompatible_dependencies`` tuple **if and only if** every dependency
      in the map is ARM64-compatible (R4.1, R4.2). An empty map trivially
      satisfies this and selects ARM64-only.
    * :class:`~hellodj_platform_logic.types.CpuArch.X86_64` with
      ``incompatible_dependencies`` listing every dependency that was not
      ARM64-compatible whenever at least one dependency is incompatible (R4.3).
      The list documents the specific dependencies that require x86-64 (R4.4)
      and the gate never selects ARM64-only in this case (Property 5).

    The reported ``incompatible_dependencies`` preserve the iteration order of
    ``dep_arm64_map`` so the result is deterministic for a given ordered map.

    Args:
        dep_arm64_map: Mapping of dependency name to ARM64 compatibility, where
            ``True`` indicates the dependency runs on Graviton_Architecture.

    Returns:
        A :class:`~hellodj_platform_logic.types.GateDecision` whose ``arch`` is
        ARM64 exactly when every dependency is compatible, and X86_64 otherwise
        with the incompatible dependency names listed.
    """
    incompatible = tuple(
        name for name, arm64_compatible in dep_arm64_map.items() if not arm64_compatible
    )
    if incompatible:
        return GateDecision(
            arch=CpuArch.X86_64,
            incompatible_dependencies=incompatible,
        )
    return GateDecision(arch=CpuArch.ARM64)
