"""Shared pure decision/derivation logic for the HelloDJ AWS platform.

This package is the single source of truth for the platform's pure decision
functions (DNS naming, auth routing, GPU strategy/placement, dependency gate,
base-image gate, autoscaling, connection draining, pipeline promotion,
clean-slate migration filter, Hive partition keys, cost model, Tidal token
refresh, and the hybrid GPU controller).

Every function here is pure: it performs no live AWS calls and depends only on
its inputs, so it can be imported by both the CDK infrastructure layer and the
runtime components, and exercised directly by property-based tests.

Modules are added by later tasks in the ``aws-saas-replatform`` implementation
plan; this package skeleton establishes the importable namespace (task 1.1).
"""

from .auth_routing import route_auth
from .binary_cache import cache_fetch_policy, resolve_closure
from .endpoint_routing import route_endpoint
from .ephemeral_build import ephemeral_teardown
from .gpu_idle import gpu_idle_decision
from .jar_validation import JarDescriptor, is_real_jar
from .migration import filter_legacy, migrate_forks
from .pinning import verify_pin

__all__: list[str] = [
    "JarDescriptor",
    "cache_fetch_policy",
    "ephemeral_teardown",
    "filter_legacy",
    "gpu_idle_decision",
    "is_real_jar",
    "migrate_forks",
    "resolve_closure",
    "route_auth",
    "route_endpoint",
    "verify_pin",
]

__version__ = "0.1.0"
