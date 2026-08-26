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
_DEFAULT_BEDROCK_MODEL = "anthropic.claude-3-haiku-20240307-v1:0"
_DEFAULT_POLLY_VOICE = "Joanna"
_DEFAULT_POLLY_ENGINE = "neural"
_DEFAULT_TRANSCRIBE_LANGUAGE = "en-US"
_DEFAULT_SAMPLE_RATE = 16000
_DEFAULT_ORCHESTRATOR_URL = "http://playback-orchestrator:8080"


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
