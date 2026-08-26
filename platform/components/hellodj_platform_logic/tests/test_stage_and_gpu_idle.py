"""Unit tests for the enum reconciliation and dataclass invariants (task 1.3).

These example-based tests lock in the two structural changes introduced by the
``hellodj-nix-native-delivery`` data-model tasks (1.1 / 1.2):

* the ``DeploymentStage`` enum is reconciled to exactly
  ``{BETA, STAGING, PRODUCTION}`` — the prior ``GAMMA`` member is gone and the
  declaration order encodes the fixed Beta -> Staging -> Production promotion
  sequence (Requirement 9.1);
* ``GpuIdleConfig`` accepts the 300-second default and rejects any idle window
  outside the inclusive ``[60, 900]`` range (Requirement 8.5).
"""

from __future__ import annotations

import pytest

from hellodj_platform_logic.types import DeploymentStage, GpuIdleConfig


# ---------------------------------------------------------------------------
# DeploymentStage reconciliation (Requirement 9.1)
# ---------------------------------------------------------------------------


def test_deployment_stage_membership_is_exactly_beta_staging_production() -> None:
    """The enum names exactly Beta, Staging, and Production (Requirement 9.1)."""
    assert {s.name for s in DeploymentStage} == {"BETA", "STAGING", "PRODUCTION"}
    assert {s.value for s in DeploymentStage} == {"beta", "staging", "production"}


def test_deployment_stage_has_no_gamma_member() -> None:
    """The prior GAMMA member is fully removed (Requirement 9.1)."""
    assert not hasattr(DeploymentStage, "GAMMA")
    assert "gamma" not in {s.value for s in DeploymentStage}
    assert "GAMMA" not in {s.name for s in DeploymentStage}


def test_deployment_stage_declaration_order_is_beta_staging_production() -> None:
    """Declaration order encodes Beta -> Staging -> Production (Requirement 9.1)."""
    assert list(DeploymentStage) == [
        DeploymentStage.BETA,
        DeploymentStage.STAGING,
        DeploymentStage.PRODUCTION,
    ]
    assert [s.order for s in DeploymentStage] == [0, 1, 2]
    assert DeploymentStage.BETA.order < DeploymentStage.STAGING.order
    assert DeploymentStage.STAGING.order < DeploymentStage.PRODUCTION.order


def test_only_production_reports_is_production() -> None:
    """The is_production flag holds only for the Production stage (Requirement 9.1)."""
    assert DeploymentStage.PRODUCTION.is_production is True
    assert DeploymentStage.BETA.is_production is False
    assert DeploymentStage.STAGING.is_production is False


# ---------------------------------------------------------------------------
# GpuIdleConfig idle-window invariants (Requirement 8.5)
# ---------------------------------------------------------------------------


def test_gpu_idle_config_default_is_300() -> None:
    """The default idle window is the accepted 300-second value (Requirement 8.5)."""
    assert GpuIdleConfig().idle_window_seconds == 300.0


@pytest.mark.parametrize("window", [60.0, 300.0, 900.0, 61.0, 899.0])
def test_gpu_idle_config_accepts_windows_within_range(window: float) -> None:
    """Windows within the inclusive [60, 900] range are accepted (Requirement 8.5)."""
    assert GpuIdleConfig(idle_window_seconds=window).idle_window_seconds == window


@pytest.mark.parametrize("window", [59.0, 0.0, -1.0, 901.0, 1000.0])
def test_gpu_idle_config_rejects_windows_outside_range(window: float) -> None:
    """Windows below 60 or above 900 are rejected (Requirement 8.5)."""
    with pytest.raises(ValueError):
        GpuIdleConfig(idle_window_seconds=window)
