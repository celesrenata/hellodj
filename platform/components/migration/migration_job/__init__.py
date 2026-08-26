"""HelloDJ admin-bootstrap migration component.

One-time, independently deployable migration Job for the AWS re-platform. It
performs the *clean-slate* migration (R19): it loads the legacy export, runs the
shared :func:`hellodj_platform_logic.migration.filter_legacy` decision function
to keep **only** the ``Admin_Bootstrap_Credential`` (R19.1, R19.2, R19.4), seeds
that single credential into the Cognito user pool so the Platform_Owner can log
in as the administrator for the first time (R19.3), and initializes all other
data fresh in DynamoDB (no legacy playback/session/playlist/config carried over
— R19.4).

It is designed to run once as a Kubernetes Job / pre-deploy init step. ``boto3``
is imported lazily inside the client factories so this package imports for tests
/ ``py_compile`` without AWS libraries present, and every AWS client is
injectable so the flow is unit-testable without live AWS.

Requirements: 19.1, 19.2, 19.3, 19.4
"""

from __future__ import annotations

from .cognito_seeder import (
    AdminBootstrapCredential,
    CognitoAdminSeeder,
    build_cognito_client,
)
from .fresh_init import FreshDataInitializer
from .job import MigrationJob, MigrationResult
from .legacy_source import (
    InMemoryLegacySource,
    JsonFileLegacySource,
    S3JsonLegacySource,
    parse_legacy_records,
)

__all__ = [
    "AdminBootstrapCredential",
    "CognitoAdminSeeder",
    "build_cognito_client",
    "FreshDataInitializer",
    "MigrationJob",
    "MigrationResult",
    "InMemoryLegacySource",
    "JsonFileLegacySource",
    "S3JsonLegacySource",
    "parse_legacy_records",
]
