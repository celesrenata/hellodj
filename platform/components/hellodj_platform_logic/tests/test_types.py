"""Unit tests for the shared typed contracts and enums (task 1.2).

These tests pin down the structural invariants that later decision-logic
modules and the correctness properties rely on: the fixed
Beta -> Staging -> Production stage ordering, the closed membership of each
decision enum, the immutability of the value objects, and the design-phase
threshold constants.
"""

from __future__ import annotations

import dataclasses

import pytest

from hellodj_platform_logic.types import (
    DEFAULT_DRAIN_TIMEOUT_SECONDS,
    DEFAULT_SCALE_IN_THRESHOLD,
    DEFAULT_SCALE_OUT_THRESHOLD,
    HELLODJ_ZONE,
    INTERACTIVE_LATENCY_BUDGET_SECONDS,
    AuthProvider,
    AuthPurpose,
    AutoscaleDecision,
    BaseImageDescriptor,
    CostCategory,
    CostTier,
    CpuArch,
    DeploymentStage,
    DrainState,
    GateDecision,
    GpuPlacement,
    GpuStrategy,
    GpuStrategyCandidate,
    HybridGpuState,
    HybridGpuThresholds,
    InFlightTask,
    ScaleThresholds,
    StageResult,
    UserType,
    UtilizationReading,
)


def test_deployment_stage_fixed_promotion_order() -> None:
    """Stages declare the mandatory Beta -> Staging -> Production order (Property 9)."""
    assert [s.order for s in DeploymentStage] == [0, 1, 2]
    assert list(DeploymentStage) == [
        DeploymentStage.BETA,
        DeploymentStage.STAGING,
        DeploymentStage.PRODUCTION,
    ]


def test_only_production_is_production() -> None:
    """Exactly one stage is production, and it is PRODUCTION (Property 1)."""
    production = [s for s in DeploymentStage if s.is_production]
    assert production == [DeploymentStage.PRODUCTION]


def test_stage_result_members() -> None:
    """Stage results cover success, failure, and skip (Property 9)."""
    assert {r.value for r in StageResult} == {"succeeded", "failed", "skipped"}


def test_auth_purpose_space_is_closed() -> None:
    """The auth-routing purpose space matches the design invariant (R8/R9)."""
    assert {p.value for p in AuthPurpose} == {
        "admin_auth",
        "initial_registration",
        "account_recovery",
        "day_to_day_login",
        "tidal_source_auth",
    }


def test_auth_providers_cover_all_routes() -> None:
    """Providers cover Cognito, Discord OAuth, and first-party Tidal."""
    assert {p for p in AuthProvider} == {
        AuthProvider.COGNITO,
        AuthProvider.DISCORD_OAUTH,
        AuthProvider.TIDAL_FIRST_PARTY,
    }


def test_user_types_present() -> None:
    """User categories include admin, registered, appointed, anonymous (R8)."""
    assert {u.value for u in UserType} == {
        "admin",
        "registered",
        "appointed",
        "anonymous",
    }


def test_gpu_strategy_and_placement_members() -> None:
    """GPU strategy and placement enums match Decision D2/D3."""
    assert {s for s in GpuStrategy} == {
        GpuStrategy.PER_JOB,
        GpuStrategy.WARM_SHARED,
        GpuStrategy.SOFTWARE_CPU,
    }
    assert {p for p in GpuPlacement} == {
        GpuPlacement.CO_LOCATED,
        GpuPlacement.SEPARATE_HOST,
    }


def test_cpu_arch_members() -> None:
    """The dependency gate chooses between ARM64 and x86-64 (Property 5)."""
    assert {a.value for a in CpuArch} == {"arm64", "x86_64"}


def test_autoscale_and_drain_and_hybrid_states() -> None:
    """Decision/state enums expose the expected closed spaces."""
    assert {d for d in AutoscaleDecision} == {
        AutoscaleDecision.SCALE_OUT,
        AutoscaleDecision.SCALE_IN,
        AutoscaleDecision.HOLD,
    }
    assert {s.value for s in DrainState} == {"active", "draining", "drained"}
    assert {s.value for s in HybridGpuState} == {
        "electric_only",
        "engine_starting",
        "hybrid_gpu",
        "coasting",
    }


def test_cost_tier_and_category_members() -> None:
    """Cost model exposes three tiers and six categories (Property 13, R20.2)."""
    assert list(CostTier) == [
        CostTier.MINIMUM,
        CostTier.RECOMMENDED,
        CostTier.RECOMMENDED_WITH_HEADROOM,
    ]
    assert {c.value for c in CostCategory} == {
        "compute",
        "gpu",
        "data_layer",
        "edge_cache",
        "log_store",
        "observability",
    }


def test_value_objects_are_frozen() -> None:
    """The typed value objects are immutable frozen dataclasses."""
    frozen_types = [
        GpuStrategyCandidate,
        GateDecision,
        BaseImageDescriptor,
        UtilizationReading,
        ScaleThresholds,
        InFlightTask,
        HybridGpuThresholds,
    ]
    for cls in frozen_types:
        params = dataclasses.fields(cls)
        assert params is not None
        assert cls.__dataclass_params__.frozen is True


def test_utilization_reading_is_immutable() -> None:
    """Mutating a frozen reading raises (defensive immutability)."""
    reading = UtilizationReading(cpu=0.1, ram=0.2, gpu=0.3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        reading.cpu = 0.9  # type: ignore[misc]


def test_default_scale_thresholds() -> None:
    """Default thresholds provide hysteresis: scale-in < scale-out (R16)."""
    thresholds = ScaleThresholds()
    assert thresholds.scale_out.cpu == DEFAULT_SCALE_OUT_THRESHOLD
    assert thresholds.scale_in.cpu == DEFAULT_SCALE_IN_THRESHOLD
    assert DEFAULT_SCALE_IN_THRESHOLD < DEFAULT_SCALE_OUT_THRESHOLD


def test_design_phase_constants() -> None:
    """Threshold/zone constants match the design's Testing Strategy."""
    assert HELLODJ_ZONE == "hellodj.bot"
    assert INTERACTIVE_LATENCY_BUDGET_SECONDS == 5.0
    assert DEFAULT_DRAIN_TIMEOUT_SECONDS == 120.0
