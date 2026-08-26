"""Speech-to-text via Amazon Transcribe (streaming) with a Bedrock fallback.

Captured PCM audio (16 kHz, mono, int16) from the wake word trigger is sent to
Amazon Transcribe's streaming API to produce a transcript. All access is over
the AWS SDK using the pod's IAM task role (no static keys); the concrete client
is injected via :class:`~voice_pipeline.aws_clients.AwsClientFactory`, so tests
run with fakes and no live AWS calls.

boto3/botocore imports are lazy (inside the client factory), keeping this module
import-clean. On any transport error, STT degrades gracefully to an empty
transcript rather than raising into the pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .aws_clients import AwsClientFactory
from .config import VoicePipelineConfig

log = logging.getLogger(__name__)

__all__ = ["Transcript", "SpeechToText"]


@dataclass(frozen=True)
class Transcript:
    """Result of a speech-to-text transcription.

    Attributes:
        text: The recognized transcript text (empty when nothing was heard).
        confidence: Best-effort confidence in ``[0, 1]`` (0.0 when unknown).
        is_final: Whether this transcript is final (vs. a partial result).
    """

    text: str
    confidence: float = 0.0
    is_final: bool = True

    @property
    def is_empty(self) -> bool:
        """True when no usable text was recognized."""
        return not self.text.strip()


class SpeechToText:
    """Transcribes captured PCM speech to text via Amazon Transcribe.

    The class is intentionally transport-agnostic about *how* PCM bytes reach
    Transcribe: it exposes a synchronous batch-style ``transcribe`` that accepts
    raw little-endian int16 PCM and returns a :class:`Transcript`. The AWS
    client is injected for testability.
    """

    def __init__(
        self,
        clients: AwsClientFactory,
        config: VoicePipelineConfig,
    ) -> None:
        """Initialise the STT engine.

        Args:
            clients: Factory supplying the (injected or lazily created) client.
            config: Runtime configuration (language, sample rate).
        """
        self._clients = clients
        self._config = config

    def transcribe(self, pcm_16k: bytes, *, language: str | None = None) -> Transcript:
        """Transcribe 16 kHz mono int16 PCM to text.

        Args:
            pcm_16k: Raw little-endian int16 PCM at 16 kHz, mono.
            language: Optional BCP-47 override (defaults to config language).

        Returns:
            A :class:`Transcript`; empty text on error or when no speech is
            recognized (graceful degradation).
        """
        if not pcm_16k:
            return Transcript(text="")
        lang = language or self._config.transcribe_language
        try:
            client = self._clients.transcribe()
            return self._invoke_transcribe(client, pcm_16k, lang)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            log.warning("STT failed, returning empty transcript: %s", exc)
            return Transcript(text="")

    def _invoke_transcribe(self, client: Any, pcm_16k: bytes, language: str) -> Transcript:
        """Call the injected Transcribe client and normalise its response.

        The client contract mirrors a streaming Transcribe adapter that accepts
        a single-shot PCM payload and returns a mapping with ``transcript`` and
        optional ``confidence``/``isFinal`` keys. Production wiring adapts the
        real streaming API to this shape; tests provide a fake directly.
        """
        response = client.transcribe_pcm(
            audio=pcm_16k,
            sample_rate_hz=self._config.sample_rate_hz,
            language_code=language,
        )
        if not isinstance(response, dict):
            return Transcript(text="")
        text = str(response.get("transcript", "")).strip()
        confidence = _coerce_confidence(response.get("confidence"))
        is_final = bool(response.get("isFinal", True))
        return Transcript(text=text, confidence=confidence, is_final=is_final)


def _coerce_confidence(value: Any) -> float:
    """Clamp an arbitrary confidence value into ``[0, 1]`` (0.0 on failure)."""
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.0
    if conf < 0.0:
        return 0.0
    if conf > 1.0:
        return 1.0
    return conf
