"""Shared typed contracts and enums for the platform decision logic.

This module is the single source of truth for the enumerations and typed
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

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# ---------------------------------------------------------------------------
# Shared constants (design-phase settings / Testing Strategy thresholds)
# ---------------------------------------------------------------------------

#: The DNS zone that every derived environment name is a subdomain of (R12).
HELLODJ_ZONE = "hellodj.bot"

#: Interactive latency budget in seconds (R3.13). GPU strategy selection must
#: never return a strategy whose warm-start latency exceeds this budget.
INTERACTIVE_LATENCY_BUDGET_SECONDS = 5.0

#: Default connection-draining timeout in seconds for app/transcode workloads
#: (R17.3); tunable per component.
DEFAULT_DRAIN_TIMEOUT_SECONDS = 120.0

#: Default autoscaling thresholds as utilization fractions (R16): scale out when
#: any signal exceeds ``SCALE_OUT``; scale in only when all signals are below
#: ``SCALE_IN``. The gap between them provides hysteresis.
DEFAULT_SCALE_OUT_THRESHOLD = 0.70
DEFAULT_SCALE_IN_THRESHOLD = 0.40


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
# Base-image gate (Property 6, R5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BaseImageDescriptor:
    """Descriptor of a container image base for the Nix base-image gate.

    The build-stage gate accepts an image if and only if its base was produced
    by the Nix build system (``nix_produced`` is True); an Ubuntu/Debian or any
    other non-Nix base is rejected (Property 6, R5.4).
    """

    base_name: str
    nix_produced: bool


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
# Clean-slate migration filter (Property 12, R19)
# ---------------------------------------------------------------------------


class LegacyRecordType(Enum):
    """Kind discriminator for a record in the legacy platform dataset.

    The clean-slate migration (R19) carries exactly one kind of record forward:
    the :attr:`ADMIN_BOOTSTRAP_CREDENTIAL`, which seeds the Platform_Owner's
    first administrator login through Cognito (R19.1, R19.3). Every other kind
    -- legacy playback, session, playlist and configuration data -- is excluded
    from the migration and initialized fresh on AWS (R19.2, R19.4).
    """

    ADMIN_BOOTSTRAP_CREDENTIAL = "admin_bootstrap_credential"
    PLAYBACK = "playback"
    SESSION = "session"
    PLAYLIST = "playlist"
    CONFIGURATION = "configuration"


@dataclass(frozen=True)
class LegacyRecord:
    """A single record drawn from the legacy platform dataset (Property 12).

    ``record_type`` is the kind discriminator the migration filter keys on;
    ``record_id`` identifies the record within its kind and ``payload`` carries
    the opaque legacy data. The migration filter reasons purely over
    ``record_type`` -- only :attr:`LegacyRecordType.ADMIN_BOOTSTRAP_CREDENTIAL`
    records survive the migration (R19.1, R19.4).
    """

    record_type: LegacyRecordType
    record_id: str = ""
    payload: str = ""


# ---------------------------------------------------------------------------
# Flake input pinning (Property 13, R11)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlakeInputPin:
    """One github:owner/repo/branch flake input and its pinned identifier (R11)."""

    input_name: str          # e.g. "lavalink", "temurin", "nixpkgs"
    owner: str               # e.g. "hellodj", "NixOS"
    repo: str
    branch: str              # github:owner/repo/branch
    pinned_identifier: str   # revision/tag/version captured in flake.lock at pin time


@dataclass(frozen=True)
class PinVerification:
    """Outcome of verifying a pin against upstream at pin time (R11.5/11.6)."""

    input_name: str
    accepted: bool
    upstream_identifier: str | None  # None when upstream could not be resolved (R11.6)
    reason: str = ""                 # set when rejected/unresolved; prior pin retained


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


# ---------------------------------------------------------------------------
# Binary-cache closure records (Properties 5-7, R7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClosureRef:
    """A Nix closure identified by its store path hash (R7.2/7.3)."""

    store_path: str          # /nix/store/<hash>-<name>
    store_path_hash: str     # the <hash> segment — the build-once identity key


@dataclass(frozen=True)
class ClosureResolution:
    """Deploy-time resolution of a required closure from the cache (R7.4)."""

    requested: ClosureRef
    present_in_cache: bool
    halt: bool               # True (and no substitution) when absent (R7.4)
    reason: str = ""


@dataclass(frozen=True)
class CacheFetchOutcome:
    """Cache reachability outcome during a build (R7.6)."""

    responded_within_timeout: bool  # 30 s budget
    retries_exhausted: bool          # 3 consecutive retries
    rebuilt_locally: bool            # True when falling back to local rebuild
    reason: str = ""


# ---------------------------------------------------------------------------
# Ephemeral build compute (Property 4, R6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EphemeralCompute:
    """An ephemeral builder's lifecycle facts (R6.6/6.7/6.8/6.9)."""

    resource_id: str
    teardown_deadline_seconds: float = 300.0     # torn down within 300 s (R6.6)
    max_lifetime_seconds: float = 10800.0        # hard 3 h cap (R6.7)


@dataclass(frozen=True)
class TeardownResult:
    """Result of tearing down ephemeral build compute (R6.8/6.9)."""

    resource_id: str
    confirmed_stopped: bool
    teardown_timestamp: str          # retained on confirmation (R6.9)
    alert_emitted: bool = False      # True when stop not confirmed (R6.8)


# ---------------------------------------------------------------------------
# Fork migration (Property 1, R1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForkMigration:
    """One fork's migration outcome (R1.6)."""

    repo: str                        # Lavalink / lavaplayer / LavaSrc / youtube-source
    created: bool
    upstream_remote_ok: bool
    error: str = ""                  # names the affected fork on failure (R1.6)


# ---------------------------------------------------------------------------
# GPU idle decision (Property 8, R8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GpuIdleConfig:
    """Idle-window config for GPU scale-to-zero (R8.5)."""

    idle_window_seconds: float = 300.0   # default; valid range [60, 900]

    def __post_init__(self) -> None:
        if not (60.0 <= self.idle_window_seconds <= 900.0):
            raise ValueError("idle window must be within 60-900 seconds (R8.5)")
