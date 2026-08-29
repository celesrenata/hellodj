"""Entry point wiring the voice-pipeline component together.

Builds runtime configuration from the environment and constructs a
:class:`~voice_pipeline.pipeline.VoicePipeline` with the standard collaborators.
AWS AI clients are created via the boto3 default credential chain (IAM task
role) — no static keys. The actual opus-frame source (``discord-bot-core``) and
the async HTTP transport for the orchestrator are supplied by the deployment
wiring; this module provides the construction seam and a small CLI probe.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import VoicePipelineConfig
from .pipeline import VoicePipeline

log = logging.getLogger(__name__)

__all__ = ["build_pipeline", "main"]


def build_pipeline(transport: Any, config: VoicePipelineConfig | None = None) -> VoicePipeline:
    """Construct the voice pipeline from environment configuration.

    Args:
        transport: Async HTTP transport for the orchestrator action client.
        config: Optional explicit config (defaults to :meth:`from_env`).

    Returns:
        A wired :class:`VoicePipeline`.
    """
    cfg = config or VoicePipelineConfig.from_env()
    return VoicePipeline.from_config(cfg, transport)


def main() -> int:
    """CLI probe that validates configuration and wake word model availability.

    Returns a process exit code. This does not start a long-running loop; the
    live opus stream is driven by ``discord-bot-core`` in the deployed wiring.
    """
    logging.basicConfig(level=logging.INFO)
    cfg = VoicePipelineConfig.from_env()
    log.info(
        "voice-pipeline configured: region=%s bedrock_model=%s polly_voice=%s "
        "orchestrator=%s wakeword=%s web_search=%s ai_task_role=%s",
        cfg.aws_region,
        cfg.bedrock_model_id,
        cfg.polly_voice_id,
        cfg.orchestrator_base_url,
        cfg.wakeword_model_path,
        cfg.web_search_available,
        bool(cfg.ai_task_role_arn),
    )
    from .wakeword import WakeWordModel  # noqa: PLC0415 - probe-only import

    model = WakeWordModel(cfg.wakeword_model_path, threshold=cfg.wakeword_threshold)
    log.info("wake word model available: %s", model.available)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
