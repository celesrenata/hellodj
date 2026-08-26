"""Deployment stages, auth routing, GPU strategy, autoscaling, draining, cost model.

Types related to the deployment pipeline, authentication, GPU placement,
autoscaling decisions, connection draining, hybrid GPU controller, and cost
model tiers.

Requirements: 2.2, 8.x, 3.10, 16.x, 17.x, 20.1
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from hellodj_platform_logic.types.constants import (
    DEFAULT_SCALE_IN_THRESHOLD,
    DEFAULT_SCALE_OUT_THRESHOLD,
)

__all__ = [
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
]


# ---------------------------------------------------------------------------
# Deployment stages (Property 9, R11 / R12)
# ---------------------------------------------------------------------------


class DeploymentStage(Enum):
    """Pipeline deployment stages in fixed promotion order.

    The declaration order encodes the mandatory promotion sequence
    Beta -> Staging -> Production (Property 9). ``PRODUCTION`` is treated
    specially by the DNS-naming logic (``production.<region>.hellodj.bot`` plus
    an apex alias) whereas the non-production stages derive
    ``<stage>.<region>.hellodj.bot`` (Property 1).
    """

    BETA = "beta"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def order(self) -> int:
        """Zero-based position in the fixed Beta -> Staging -> Production sequence."""
        return list(DeploymentStage).index(self)

    @property
    def is_production(self) -> bool:
        """Whether this stage is the production stage."""
        return self is DeploymentStage.PRODUCTION


class StageResult(Enum):
    """Outcome of deploying a single pipeline stage (Property 9)."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Authentication routing (Property 2, R8 / R9)
# ---------------------------------------------------------------------------


class AuthPurpose(Enum):
    """Purpose of an authentication request.

    Every authentication request is routed by *purpose* (auth-routing
    invariant). Administrator authentication, initial registration and account
    recovery route to Cognito; day-to-day login routes to Discord OAuth; Tidal
    source authentication routes to the first-party Tidal OAuth and must never
    route to Cognito.
    """

    ADMIN_AUTH = "admin_auth"
    INITIAL_REGISTRATION = "initial_registration"
    ACCOUNT_RECOVERY = "account_recovery"
    DAY_TO_DAY_LOGIN = "day_to_day_login"
    TIDAL_SOURCE_AUTH = "tidal_source_auth"


class UserType(Enum):
    """Category of user associated with an authentication request (R8)."""

    ADMIN = "admin"
    REGISTERED = "registered"
    APPOINTED = "appointed"
    ANONYMOUS = "anonymous"


class AuthProvider(Enum):
    """Identity provider an authentication request is routed to (Property 2)."""

    COGNITO = "cognito"
    DISCORD_OAUTH = "discord_oauth"
    TIDAL_FIRST_PARTY = "tidal_first_party"


# ---------------------------------------------------------------------------
# GPU acquisition strategy and placement (Properties 3 & 4, D2 / D3)
# ---------------------------------------------------------------------------


class GpuStrategy(Enum):
    """Candidate GPU acquisition strategies (Decision D3).

    ``PER_JOB`` (per-job / AWS Batch provisioning) has a 1-3 minute cold start
    and is rejected for interactive work. ``WARM_SHARED`` is a warm,
    time-sliced G5g node shared across jobs. ``SOFTWARE_CPU`` is libx264 on the
    already-paid-for Graviton CPU and is the selected default.
    """

    PER_JOB = "per_job"
    WARM_SHARED = "warm_shared"
    SOFTWARE_CPU = "software_cpu"


@dataclass(frozen=True)
class GpuStrategyCandidate:
    """A GPU strategy annotated with its cost and warm-start latency.

    Consumed by the GPU acquisition selection function (Property 3), which
    returns the lowest-cost candidate whose ``warm_start_latency_seconds`` is
    within :data:`INTERACTIVE_LATENCY_BUDGET_SECONDS`.
    """

    strategy: GpuStrategy
    incremental_cost: float
    warm_start_latency_seconds: float


class GpuPlacement(Enum):
    """Where GPU transcode workloads run (Decision D2, Property 4).

    ``CO_LOCATED`` places transcode pods on/near the media producers so the
    producer -> transcoder hop is loopback/intra-node. ``SEPARATE_HOST`` reaches
    a dedicated GPU host over the network, adding an inter-host streaming leg.
    """

    CO_LOCATED = "co_located"
    SEPARATE_HOST = "separate_host"


# ---------------------------------------------------------------------------
# CPU architecture and dependency gate (Property 5, D4 / R4)
# ---------------------------------------------------------------------------


class CpuArch(Enum):
    """Target CPU architecture selected by the dependency-compatibility gate.

    The gate selects :attr:`ARM64` only when every dependency is
    ARM64-compatible; otherwise it selects :attr:`X86_64` (or a documented
    substitute) and never selects ARM64-only (Property 5).
    """

    ARM64 = "arm64"
    X86_64 = "x86_64"


@dataclass(frozen=True)
class GateDecision:
    """Result of the Graviton/x86 dependency-compatibility gate (Property 5).

    ``incompatible_dependencies`` lists the dependency names that were not
    ARM64-compatible; it is empty exactly when :attr:`arch` is
    :attr:`CpuArch.ARM64`.
    """

    arch: CpuArch
    incompatible_dependencies: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Autoscaling (Property 7, R16)
# ---------------------------------------------------------------------------


class AutoscaleDecision(Enum):
    """Outcome of the autoscaling decision function (Property 7).

    Scale out when any signal exceeds its scale-out threshold; scale in only
    when all signals are below their scale-in thresholds; otherwise hold.
    """

    SCALE_OUT = "scale_out"
    SCALE_IN = "scale_in"
    HOLD = "hold"


@dataclass(frozen=True)
class UtilizationReading:
    """A triple of utilization readings as fractions in ``[0.0, 1.0]`` (R16)."""

    cpu: float
    ram: float
    gpu: float


@dataclass(frozen=True)
class ScaleThresholds:
    """Per-signal scale-out and scale-in thresholds for the autoscaler (R16)."""

    scale_out: UtilizationReading = field(
        default_factory=lambda: UtilizationReading(
            cpu=DEFAULT_SCALE_OUT_THRESHOLD,
            ram=DEFAULT_SCALE_OUT_THRESHOLD,
            gpu=DEFAULT_SCALE_OUT_THRESHOLD,
        )
    )
    scale_in: UtilizationReading = field(
        default_factory=lambda: UtilizationReading(
            cpu=DEFAULT_SCALE_IN_THRESHOLD,
            ram=DEFAULT_SCALE_IN_THRESHOLD,
            gpu=DEFAULT_SCALE_IN_THRESHOLD,
        )
    )


# ---------------------------------------------------------------------------
# Connection draining (Property 8, R17)
# ---------------------------------------------------------------------------


class DrainState(Enum):
    """Lifecycle states of the connection-draining state machine (Property 8).

    ``ACTIVE`` accepts new connections. Once ``DRAINING`` begins the host
    accepts no new connections and lets in-flight tasks finishing within the
    drain timeout complete. ``DRAINED`` is reached when all tasks have either
    completed or been force-terminated at the timeout.
    """

    ACTIVE = "active"
    DRAINING = "draining"
    DRAINED = "drained"


@dataclass(frozen=True)
class InFlightTask:
    """An in-flight task carried by a draining host (Property 8).

    ``remaining_seconds`` is how long the task still needs at the moment
    draining begins; tasks with ``remaining_seconds`` beyond the drain timeout
    are force-terminated and recorded with exactly one termination event.
    """

    task_id: str
    remaining_seconds: float


# ---------------------------------------------------------------------------
# Hybrid GPU controller (Property 15, D3)
# ---------------------------------------------------------------------------


class HybridGpuState(Enum):
    """States of the gas/electric hybrid transcode controller (Property 15).

    The CPU path always serves (the electric motor). ``ELECTRIC_ONLY`` runs CPU
    transcode alone; ``ENGINE_STARTING`` is the GPU spin-up window during which
    the CPU keeps serving; ``HYBRID_GPU`` prefers NVENC while the GPU node is
    Ready; ``COASTING`` drains jobs back to CPU before the GPU node scales to
    zero.
    """

    ELECTRIC_ONLY = "electric_only"
    ENGINE_STARTING = "engine_starting"
    HYBRID_GPU = "hybrid_gpu"
    COASTING = "coasting"


@dataclass(frozen=True)
class HybridGpuThresholds:
    """Hysteresis thresholds and sustained windows for the hybrid controller.

    ``spin_down_threshold`` must be strictly less than ``spin_up_threshold`` so
    the GPU node does not flap; each transition also requires demand to stay
    beyond the threshold for its sustained window (Property 15).
    """

    spin_up_threshold: float
    spin_down_threshold: float
    spin_up_window_seconds: float
    spin_down_window_seconds: float


# ---------------------------------------------------------------------------
# Cost model (Property 13, R20)
# ---------------------------------------------------------------------------


class CostTier(Enum):
    """The three budgeting tiers of the cost model (Property 13, R20).

    Tier totals are monotonically non-decreasing
    (Minimum <= Recommended <= Recommended-with-Headroom), and the headroom
    tier equals the Recommended tier plus a non-negative reserve.
    """

    MINIMUM = "minimum"
    RECOMMENDED = "recommended"
    RECOMMENDED_WITH_HEADROOM = "recommended_with_headroom"


class CostCategory(Enum):
    """The six cost categories itemized in every tier (R20.2, Property 13)."""

    COMPUTE = "compute"
    GPU = "gpu"
    DATA_LAYER = "data_layer"
    EDGE_CACHE = "edge_cache"
    LOG_STORE = "log_store"
    OBSERVABILITY = "observability"


# ---------------------------------------------------------------------------
# Stage endpoint mapping (Property 9, R8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StageEndpoint:
    """A single stage's isolated endpoint on the shared GPU host (R8.2)."""

    stage: DeploymentStage
    namespace: str   # hellodj-beta / hellodj-staging / hellodj-production
    port: int
    hostname: str    # <stage>.<region>.hellodj.bot (from dns_naming, R9.3)
