"""Entry point for the config-renderer init container / Job.

Wires the Secrets Manager credential source and the DynamoDB config source into
the pure renderer, writes the rendered ``application.yml`` to the target path,
then exits. Intended to run once before the ``lavalink`` container starts.

Usage::

    python -m config_renderer [OUTPUT_PATH]

Environment:
    HELLODJ_LAVALINK_SECRET_ID   Secrets Manager secret id/ARN (required).
    HELLODJ_CORE_TABLE           DynamoDB core table (default hellodj-core).
    AWS_REGION / AWS_DEFAULT_REGION  Region for the AWS clients (optional).

Requirements: 6.1, 7.3, 15.1
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from hellodj_platform_logic.data_access import CORE_TABLE_NAME

from .config_source import DynamoConfigSource
from .renderer import render_yaml
from .secrets_source import SecretsManagerCredentialSource

log = logging.getLogger("config_renderer")

#: Default output path — an emptyDir the lavalink container mounts read-only.
DEFAULT_OUTPUT_PATH = "/out/application.yml"

#: Environment variable names.
ENV_SECRET_ID = "HELLODJ_LAVALINK_SECRET_ID"
ENV_CORE_TABLE = "HELLODJ_CORE_TABLE"
ENV_REGION = "AWS_REGION"
ENV_REGION_FALLBACK = "AWS_DEFAULT_REGION"


def _resolve_region() -> str | None:
    """Return the configured AWS region, if any."""
    return os.environ.get(ENV_REGION) or os.environ.get(ENV_REGION_FALLBACK)


def _write_output(output_path: str, contents: str) -> None:
    """Write the rendered config to ``output_path``, creating parent dirs."""
    path = Path(output_path)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def run(output_path: str) -> int:
    """Render the Lavalink config to ``output_path``; return an exit code."""
    secret_id = os.environ.get(ENV_SECRET_ID, "").strip()
    if not secret_id:
        log.error("%s environment variable is required", ENV_SECRET_ID)
        return 1

    region = _resolve_region()
    table_name = os.environ.get(ENV_CORE_TABLE, CORE_TABLE_NAME).strip() or (
        CORE_TABLE_NAME
    )

    try:
        credentials = SecretsManagerCredentialSource(
            secret_id, region_name=region
        ).load()
    except Exception as error:  # noqa: BLE001 - surface as a clean init failure
        log.error("failed to load credentials from Secrets Manager: %s", error)
        return 1

    try:
        from .config_source import build_core_table

        settings = DynamoConfigSource(
            build_core_table(table_name, region_name=region)
        ).load()
    except Exception as error:  # noqa: BLE001 - surface as a clean init failure
        log.error("failed to load config from DynamoDB: %s", error)
        return 1

    contents = render_yaml(credentials, settings)
    try:
        _write_output(output_path, contents)
    except OSError as error:
        log.error("failed to write %s: %s", output_path, error)
        return 1

    log.info("rendered Lavalink config to %s", output_path)
    return 0


def main() -> None:
    """Console-script / ``python -m`` entry point."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    output_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUTPUT_PATH
    raise SystemExit(run(output_path))


if __name__ == "__main__":
    main()
