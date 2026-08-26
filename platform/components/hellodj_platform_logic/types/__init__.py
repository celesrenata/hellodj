"""Shared typed contracts and enums for the platform decision logic.

This package is the single source of truth for the enumerations and typed
value objects consumed by the pure decision/derivation modules created in the
later ``aws-saas-replatform`` tasks (``dns_naming``, ``promotion``,
``auth_routing``, ``tidal_refresh``, ``gpu_strategy``, ``dependency_gate``,
``base_image_gate``, ``autoscale``, ``draining``, ``hybrid_gpu``,
``data_access``, ``hive_partition``, ``migration`` and ``cost_model``) and
mirrored by the CDK infrastructure layer.

Everything here is pure data: enumerations model the finite, closed decision
spaces the correctness properties reason over, and the frozen dataclasses model
the immutable inputs/outputs those decisions consume and produce. No module in
this package performs live AWS calls, so these types can be imported by both the
CDK layer and the runtime components and exercised directly by property tests.

Design references:
    * Decision D1/D2/D3 (orchestrator, GPU placement, GPU acquisition)
    * Auth routing invariant (Property 2)
    * Correctness Properties 1-15
    * Cost Model (three-tier estimate) and Testing Strategy thresholds

Requirements: 2.2, 8.x, 3.10, 4.1, 16.x, 17.x, 20.1
"""
# ruff: noqa: F405

# Re-export everything so `from hellodj_platform_logic.types import X` works.
from hellodj_platform_logic.types.base_image import *  # noqa: F401, F403
from hellodj_platform_logic.types.cache import *  # noqa: F401, F403
from hellodj_platform_logic.types.constants import *  # noqa: F401, F403
from hellodj_platform_logic.types.deployment import *  # noqa: F401, F403
from hellodj_platform_logic.types.migration import *  # noqa: F401, F403
from hellodj_platform_logic.types.pinning import *  # noqa: F401, F403

__all__ = [
    # constants
    "HELLODJ_ZONE",
    "INTERACTIVE_LATENCY_BUDGET_SECONDS",
    "DEFAULT_DRAIN_TIMEOUT_SECONDS",
    "DEFAULT_SCALE_OUT_THRESHOLD",
    "DEFAULT_SCALE_IN_THRESHOLD",
    # deployment
    "DeploymentStage",
    "StageResult",
    "AuthPurpose",
    "UserType",
    "AuthProvider",
    "GpuStrategy",
    "GpuStrategyCandidate",
    "GpuPlacement",
    "CpuArch",
    "GateDecision",
    "AutoscaleDecision",
    "UtilizationReading",
    "ScaleThresholds",
    "DrainState",
    "InFlightTask",
    "HybridGpuState",
    "HybridGpuThresholds",
    "CostTier",
    "CostCategory",
    "StageEndpoint",
    # base_image
    "BaseImageDescriptor",
    # cache
    "ClosureRef",
    "ClosureResolution",
    "CacheFetchOutcome",
    "CacheTier",
    "CacheTierResolution",
    "EphemeralCompute",
    "TeardownResult",
    # migration
    "LegacyRecordType",
    "LegacyRecord",
    "ForkMigration",
    "CodeCommitRepo",
    "GpuIdleConfig",
    "DependencyCheck",
    "PythonComponentMigration",
    # pinning
    "FlakeInputPin",
    "PinVerification",
    "InputForm",
    "CodeCommitInput",
    "StalePin",
    "AlarmNotification",
    "EmailDelivery",
]
