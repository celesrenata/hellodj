"""Binary-cache closure records and ephemeral build compute types.

Types for Nix closure resolution (R7), cache tier lookups (R4), and ephemeral
builder lifecycle (R6).

Requirements: 4.x, 6.x, 7.x
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "ClosureRef",
    "ClosureResolution",
    "CacheFetchOutcome",
    "CacheTier",
    "CacheTierResolution",
    "EphemeralCompute",
    "TeardownResult",
]


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
# Local Nix cache tier in front of the S3 binary cache
# (hellodj-private-source-and-toolchain Property 3 / Property 4, R4)
# ---------------------------------------------------------------------------


class CacheTier(Enum):
    """Where a required closure is resolved from (R4.2/R4.3/R4.9).

    The ``Local_Nix_Cache`` tier sits *in front of* the existing S3 binary
    cache, so a closure is resolved from the closest tier that holds it:

    * :attr:`LOCAL_HIT` -- the closure is present and integrity-valid in the
      local tier; it is reused with no rebuild and no S3 fetch (R4.2).
    * :attr:`S3_HIT` -- the closure is not usable locally but present in S3; it
      is fetched from S3 and the local tier is repopulated (R4.3/R4.5).
    * :attr:`BUILD` -- the closure is usable at neither tier; it is built, the
      local tier is populated, and the closure is pushed to S3 (R4.9).
    """

    LOCAL_HIT = "local_hit"   # reuse local; no rebuild, no S3 fetch (R4.2)
    S3_HIT = "s3_hit"         # fetch from S3, populate local (R4.3/R4.5)
    BUILD = "build"           # build, populate local, push S3 (R4.9)


@dataclass(frozen=True)
class CacheTierResolution:
    """Outcome of the local-in-front-of-S3 tiered lookup (R4.2-R4.5/R4.9)."""

    tier: CacheTier
    populated_local: bool     # True on S3_HIT and BUILD (local repopulated)
    pushed_s3: bool           # True on BUILD (consistent with existing publish path, R4.9)
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
