"""Entry point for the one-time admin-bootstrap migration Job.

Wires the legacy export source (local file or S3), the Cognito seeder, and the
fresh-data initializer into the :class:`MigrationJob` and runs it once, then
exits. Intended to run as a Kubernetes Job / pre-deploy init step (R19).

Usage::

    python -m migration_job

Environment:
    HELLODJ_USER_POOL_ID       Cognito user pool id to seed the admin into
                               (required).
    HELLODJ_ADMIN_GROUP        Cognito group for the admin (default ``admins``).
    HELLODJ_LEGACY_EXPORT_FILE Path to a local JSON legacy export, OR
    HELLODJ_LEGACY_EXPORT_S3   ``s3://bucket/key`` location of the JSON export.
                               Exactly one of the two export vars is required.
    HELLODJ_FRESH_INIT_VERIFY  When ``1``/``true``, probe the fresh DynamoDB
                               tables for reachability (default off).
    AWS_REGION / AWS_DEFAULT_REGION  Region for the AWS clients (optional).

Requirements: 19.1, 19.2, 19.3, 19.4
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

from .cognito_seeder import DEFAULT_ADMIN_GROUP, CognitoAdminSeeder
from .fresh_init import FreshDataInitializer
from .job import MigrationJob
from .legacy_source import (
    JsonFileLegacySource,
    LegacySource,
    S3JsonLegacySource,
)

log = logging.getLogger("migration_job")

ENV_USER_POOL_ID = "HELLODJ_USER_POOL_ID"
ENV_ADMIN_GROUP = "HELLODJ_ADMIN_GROUP"
ENV_EXPORT_FILE = "HELLODJ_LEGACY_EXPORT_FILE"
ENV_EXPORT_S3 = "HELLODJ_LEGACY_EXPORT_S3"
ENV_FRESH_INIT_VERIFY = "HELLODJ_FRESH_INIT_VERIFY"
ENV_REGION = "AWS_REGION"
ENV_REGION_FALLBACK = "AWS_DEFAULT_REGION"

_TRUTHY = {"1", "true", "yes", "on"}


def _resolve_region() -> str | None:
    """Return the configured AWS region, if any."""
    return os.environ.get(ENV_REGION) or os.environ.get(ENV_REGION_FALLBACK)


def _build_legacy_source(region: str | None) -> LegacySource:
    """Build the legacy export source from the environment.

    Raises:
        ValueError: If neither or both export locations are configured.
    """
    export_file = os.environ.get(ENV_EXPORT_FILE, "").strip()
    export_s3 = os.environ.get(ENV_EXPORT_S3, "").strip()

    if bool(export_file) == bool(export_s3):
        raise ValueError(
            f"exactly one of {ENV_EXPORT_FILE} or {ENV_EXPORT_S3} is required"
        )

    if export_file:
        return JsonFileLegacySource(export_file)

    parsed = urlparse(export_s3)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError(
            f"{ENV_EXPORT_S3} must be an s3://bucket/key URL, got {export_s3!r}"
        )
    return S3JsonLegacySource(
        parsed.netloc, parsed.path.lstrip("/"), region_name=region
    )


def _build_fresh_initializer(region: str | None) -> FreshDataInitializer:
    """Build the fresh-data initializer, wiring a DynamoDB probe if requested."""
    verify = os.environ.get(ENV_FRESH_INIT_VERIFY, "").strip().lower() in _TRUTHY
    if not verify:
        return FreshDataInitializer()

    from .fresh_init import build_dynamo_resource

    return FreshDataInitializer(resource=build_dynamo_resource(region))


def run() -> int:
    """Run the migration Job once; return a process exit code."""
    user_pool_id = os.environ.get(ENV_USER_POOL_ID, "").strip()
    if not user_pool_id:
        log.error("%s environment variable is required", ENV_USER_POOL_ID)
        return 1

    region = _resolve_region()
    admin_group = os.environ.get(ENV_ADMIN_GROUP, DEFAULT_ADMIN_GROUP).strip() or (
        DEFAULT_ADMIN_GROUP
    )

    try:
        legacy_source = _build_legacy_source(region)
    except ValueError as error:
        log.error("invalid legacy export configuration: %s", error)
        return 1

    seeder = CognitoAdminSeeder(
        user_pool_id, admin_group=admin_group, region_name=region
    )
    fresh_initializer = _build_fresh_initializer(region)

    job = MigrationJob(
        legacy_source, seeder, fresh_initializer=fresh_initializer
    )

    try:
        result = job.run()
    except Exception as error:  # noqa: BLE001 - surface as a clean Job failure
        log.error("migration failed: %s", error)
        return 1

    log.info(
        "migration complete: %d legacy record(s) read, %d admin credential(s) "
        "seeded, %d fresh table(s) verified",
        result.legacy_record_count,
        len(result.seeded_usernames),
        len(result.fresh_tables_verified),
    )
    return 0


def main() -> None:
    """Console-script / ``python -m`` entry point."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(run())


if __name__ == "__main__":
    main()
