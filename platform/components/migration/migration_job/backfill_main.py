"""Entry point for the one-shot source-credential backfill.

Wires the legacy Secrets Manager reader, the ``hellodj-core`` :class:`CoreTable`,
and the source-credentials KMS client into a :class:`SourceCredentialBackfill`
and runs it once, then exits. Intended to run as a one-time Kubernetes Job /
ops step for Migration & Rollout step 3 of the unified-oauth-and-token-watchdog
spec (R2.6, R6.5). It is idempotent, so re-running is safe.

Usage::

    python -m migration_job.backfill_main

Environment:
    HELLODJ_STAGE                     Stage (``beta``/``staging``/``production``)
                                      used to build + validate the legacy secret
                                      name prefix ``hellodj/<stage>/guild/``
                                      (default ``beta``).
    HELLODJ_CORE_TABLE                ``hellodj-core`` DynamoDB table name
                                      (default ``hellodj-core``).
    HELLODJ_SOURCE_CREDS_KMS_KEY_ID   Source-credentials CMK id/ARN used to
                                      envelope-encrypt each token blob
                                      (required).
    AWS_REGION / AWS_DEFAULT_REGION   Region for the AWS clients (optional).

Requirements: 2.6, 6.5
"""

from __future__ import annotations

import logging
import os

from .source_credential_backfill import (
    SourceCredentialBackfill,
    build_secrets_client,
)

log = logging.getLogger("migration_job")

ENV_STAGE = "HELLODJ_STAGE"
ENV_CORE_TABLE = "HELLODJ_CORE_TABLE"
ENV_KMS_KEY_ID = "HELLODJ_SOURCE_CREDS_KMS_KEY_ID"
ENV_REGION = "AWS_REGION"
ENV_REGION_FALLBACK = "AWS_DEFAULT_REGION"

DEFAULT_STAGE = "beta"
DEFAULT_CORE_TABLE = "hellodj-core"


def _resolve_region() -> str | None:
    """Return the configured AWS region, if any."""
    return os.environ.get(ENV_REGION) or os.environ.get(ENV_REGION_FALLBACK)


def _build_core_table(table_name: str, region: str | None):
    """Build a real ``CoreTable`` over the DynamoDB resource (lazy boto3)."""
    import boto3
    from hellodj_platform_logic.data_access import CoreTable

    ddb = boto3.resource("dynamodb", region_name=region)
    return CoreTable(ddb.Table(table_name))


def _build_kms(region: str | None):
    """Create a real boto3 ``kms`` client (imported lazily)."""
    import boto3

    return boto3.client("kms", region_name=region)


def run() -> int:
    """Run the backfill once; return a process exit code."""
    kms_key_id = os.environ.get(ENV_KMS_KEY_ID, "").strip()
    if not kms_key_id:
        log.error("%s environment variable is required", ENV_KMS_KEY_ID)
        return 1

    stage = os.environ.get(ENV_STAGE, DEFAULT_STAGE).strip() or DEFAULT_STAGE
    table_name = (
        os.environ.get(ENV_CORE_TABLE, DEFAULT_CORE_TABLE).strip()
        or DEFAULT_CORE_TABLE
    )
    region = _resolve_region()

    try:
        secrets = build_secrets_client(region)
        core = _build_core_table(table_name, region)
        kms = _build_kms(region)
    except Exception as error:  # noqa: BLE001 - surface as a clean Job failure
        log.error("backfill setup failed: %s", error)
        return 1

    backfill = SourceCredentialBackfill(
        secrets, core, kms, kms_key_id, stage=stage
    )

    try:
        backfill.run()
    except Exception as error:  # noqa: BLE001 - surface as a clean Job failure
        log.error("backfill failed: %s", error)
        return 1

    return 0


def main() -> None:
    """Console-script / ``python -m`` entry point."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(run())


if __name__ == "__main__":
    main()
