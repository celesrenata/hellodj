"""Environment-driven runtime configuration for the voice-pipeline.

All AWS AI access (Bedrock, Transcribe, Polly) uses the pod's IAM task role via
the boto3 default credential chain — there are NO static access keys here and
none are read from the environment. Only non-secret operational knobs (region,
model identifiers, thresholds, endpoints) are configured here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = ["VoicePipelineConfig"]

# Defaults chosen to match the design: a small on-box wake word ONNX model,
# managed AWS STT/intent/TTS, and the orchestrator as the action sink.
_DEFAULT_REGION = "us-east-1"
_DEFAULT_WAKEWORD_PATH = "/app/models/Hello_DJ.onnx"
_DEFAULT_WAKEWORD_THRESHOLD = 0.5
# Amazon Nova Micro: the cheapest/fastest ON_DEMAND text model on Bedrock, ideal
# for the brief one-off Discord voice requests this pipeline serves (not deep
# reasoning). Reached via the model-agnostic Bedrock Converse API, so swapping
# to another Converse-capable model is a single env change (BEDROCK_MODEL_ID).
_DEFAULT_BEDROCK_MODEL = "amazon.nova-micro-v1:0"
# Max tokens for a spoken reply — kept small on purpose: answers are read aloud
# via Polly, so brevity is a feature, not a limitation.
_DEFAULT_MAX_RESPONSE_TOKENS = 256
_DEFAULT_POLLY_VOICE = "Joanna"
_DEFAULT_POLLY_ENGINE = "neural"
_DEFAULT_TRANSCRIBE_LANGUAGE = "en-US"
_DEFAULT_SAMPLE_RATE = 16000
_DEFAULT_ORCHESTRATOR_URL = "http://playback-orchestrator:8080"
# Number of SearXNG results folded into a web-search tool result. A handful is
# plenty for a one-to-two-sentence spoken answer.
_DEFAULT_WEB_SEARCH_MAX_RESULTS = 5


@dataclass(frozen=True)
class VoicePipelineConfig:
    """Non-secret runtime settings for the voice-pipeline component.

    Attributes:
        aws_region: AWS region for Bedrock/Transcribe/Polly clients.
        wakeword_model_path: Filesystem path to the local ONNX wake word model.
        wakeword_threshold: Detection probability threshold (>= is a detection).
        bedrock_model_id: Bedrock model identifier used for intent reasoning.
        polly_voice_id: Amazon Polly voice used for TTS responses.
        polly_engine: Amazon Polly engine ("neural" or "standard").
        transcribe_language: Default BCP-47 language code for STT.
        sample_rate_hz: PCM sample rate the pipeline operates at.
        orchestrator_base_url: Base URL of the playback-orchestrator.
        max_response_tokens: Max tokens for a Bedrock general-query reply.
        ai_task_role_arn: ARN of the keyless Bedrock/Transcribe/Polly task role
            the pod assumes for AI access ("" => use the default credential
            chain directly, e.g. in tests / local dev).
        searxng_mcp_url: Base URL of the in-cluster ``mcp-searxng-enhanced``
            server (FastMCP HTTP mode). "" => web search is disabled and the
            general responder answers from the model alone.
        web_search_enabled: Master toggle for the Bedrock web-search tool.
        web_search_max_results: Max search results folded into a tool result.
    """

    aws_region: str = _DEFAULT_REGION
    wakeword_model_path: str = _DEFAULT_WAKEWORD_PATH
    wakeword_threshold: float = _DEFAULT_WAKEWORD_THRESHOLD
    bedrock_model_id: str = _DEFAULT_BEDROCK_MODEL
    polly_voice_id: str = _DEFAULT_POLLY_VOICE
    polly_engine: str = _DEFAULT_POLLY_ENGINE
    transcribe_language: str = _DEFAULT_TRANSCRIBE_LANGUAGE
    sample_rate_hz: int = _DEFAULT_SAMPLE_RATE
    orchestrator_base_url: str = _DEFAULT_ORCHESTRATOR_URL
    max_response_tokens: int = _DEFAULT_MAX_RESPONSE_TOKENS
    ai_task_role_arn: str = ""
    searxng_mcp_url: str = ""
    web_search_enabled: bool = True
    web_search_max_results: int = _DEFAULT_WEB_SEARCH_MAX_RESULTS

    @property
    def web_search_available(self) -> bool:
        """True when web search is both enabled and has an MCP endpoint."""
        return self.web_search_enabled and bool(self.searxng_mcp_url)

    @classmethod
    def from_env(cls) -> VoicePipelineConfig:
        """Build a config from environment variables, falling back to defaults."""
        return cls(
            aws_region=os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", _DEFAULT_REGION)),
            wakeword_model_path=os.getenv("WAKE_WORD_MODEL_PATH", _DEFAULT_WAKEWORD_PATH),
            wakeword_threshold=_env_float("WAKE_WORD_THRESHOLD", _DEFAULT_WAKEWORD_THRESHOLD),
            bedrock_model_id=os.getenv("BEDROCK_MODEL_ID", _DEFAULT_BEDROCK_MODEL),
            polly_voice_id=os.getenv("POLLY_VOICE_ID", _DEFAULT_POLLY_VOICE),
            polly_engine=os.getenv("POLLY_ENGINE", _DEFAULT_POLLY_ENGINE),
            transcribe_language=os.getenv("TRANSCRIBE_LANGUAGE", _DEFAULT_TRANSCRIBE_LANGUAGE),
            sample_rate_hz=_env_int("VOICE_SAMPLE_RATE", _DEFAULT_SAMPLE_RATE),
            orchestrator_base_url=os.getenv("ORCHESTRATOR_URL", _DEFAULT_ORCHESTRATOR_URL),
            max_response_tokens=_env_int(
                "BEDROCK_MAX_RESPONSE_TOKENS", _DEFAULT_MAX_RESPONSE_TOKENS
            ),
            ai_task_role_arn=os.getenv("HELLODJ_AI_TASK_ROLE_ARN", ""),
            searxng_mcp_url=os.getenv("HELLODJ_SEARXNG_MCP_URL", ""),
            web_search_enabled=_env_bool("HELLODJ_WEB_SEARCH_ENABLED", True),
            web_search_max_results=_env_int(
                "HELLODJ_WEB_SEARCH_MAX_RESULTS", _DEFAULT_WEB_SEARCH_MAX_RESULTS
            ),
        )


def _env_float(name: str, default: float) -> float:
    """Read a float environment variable, falling back to a default."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    """Read an int environment variable, falling back to a default."""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean environment variable, falling back to a default.

    Truthy values (case-insensitive): ``1``, ``true``, ``yes``, ``on``.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
