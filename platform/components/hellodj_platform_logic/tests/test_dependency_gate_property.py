"""Property test for the Graviton/x86 dependency-gate decision (task 4.6).

Property 5 (design "Graviton/x86 dependency-gate decision"): for any component
described by a map of its runtime dependencies to their ARM64 (Graviton)
compatibility, the Dependency_Compatibility_Gate selects ARM64-only *if and
only if* every dependency is ARM64-compatible. If any dependency is
incompatible it selects x86-64 (or a documented substitute), lists exactly the
dependencies that were incompatible, and never selects ARM64-only.

The gate is pure, so the property is exercised directly over arbitrary
dependency-compatibility maps generated with Hypothesis ``dictionaries`` (text
keys, boolean values), including the empty map (which trivially selects
ARM64-only). >=100 iterations.

Feature: aws-saas-replatform, Property 5

Validates: Requirements 4.1, 4.2, 4.3
"""

from __future__ import annotations

from collections.abc import Mapping

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.dependency_gate import gate
from hellodj_platform_logic.types import CpuArch, GateDecision

# Arbitrary dependency-compatibility maps: text dependency names -> ARM64
# compatibility booleans. ``max_size`` is kept modest to keep iterations fast
# while still spanning the empty map, all-compatible, all-incompatible, and
# mixed cases.
_dep_maps = st.dictionaries(
    keys=st.text(max_size=24),
    values=st.booleans(),
    max_size=8,
)


@settings(max_examples=200)
@given(dep_arm64_map=_dep_maps)
def test_dependency_gate_decision(dep_arm64_map: Mapping[str, bool]) -> None:
    """ARM64-only iff all deps compatible; else x86-64 listing the failures.

    Feature: aws-saas-replatform, Property 5

    Validates: Requirements 4.1, 4.2, 4.3
    """
    decision = gate(dep_arm64_map)
    assert isinstance(decision, GateDecision)

    all_compatible = all(dep_arm64_map.values())
    expected_incompatible = {
        name for name, ok in dep_arm64_map.items() if not ok
    }

    if all_compatible:
        # ARM64-only iff every dependency is ARM64-compatible (R4.1, R4.2).
        # The empty map trivially satisfies this and selects ARM64-only.
        assert decision.arch is CpuArch.ARM64
        assert decision.incompatible_dependencies == ()
    else:
        # Any incompatible dependency forces x86-64 (R4.3) and the gate must
        # never select ARM64-only in this case (Property 5).
        assert decision.arch is CpuArch.X86_64
        assert decision.arch is not CpuArch.ARM64
        # The listed dependencies are exactly those mapped to False (R4.4
        # documentation of the specific dependency requiring x86-64).
        assert set(decision.incompatible_dependencies) == expected_incompatible
        # Only genuinely-incompatible deps are reported; none are duplicated.
        assert len(decision.incompatible_dependencies) == len(
            expected_incompatible
        )
        assert all(
            not dep_arm64_map[name]
            for name in decision.incompatible_dependencies
        )


@settings(max_examples=200)
@given(dep_arm64_map=_dep_maps)
def test_arm64_only_never_when_any_incompatible(
    dep_arm64_map: Mapping[str, bool],
) -> None:
    """The gate never selects ARM64-only when any dependency is incompatible.

    Feature: aws-saas-replatform, Property 5

    Validates: Requirements 4.2, 4.3
    """
    decision = gate(dep_arm64_map)
    if any(not ok for ok in dep_arm64_map.values()):
        assert decision.arch is not CpuArch.ARM64
