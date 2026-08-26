"""Text-to-speech via Amazon Polly.

Spoken responses are synthesized with Amazon Polly's neural voices over the AWS
SDK using the pod's IAM task role (no static keys). The Polly client is injected
via :class:`~voice_pipeline.aws_clients.AwsClientFactory`, so tests run with a
fake and never touch AWS.

This replaces all legacy self-hosted TTS (Kokoro / Speaches). On any error,
synthesis degrades gracefully to empty audio rather than raising.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .aws_clients import AwsClientFactory
from .config import VoicePipelineConfig

log = logging.getLogger(__name__)

__all__ = ["SpeechAudio", "TextToSpeech"]


@dataclass(frozen=True)
class SpeechAudio:
    """Synthesized speech audio.

    Attributes:
        audio: The synthesized audio bytes (empty on failure).
        content_type: MIME type of the audio (e.g. ``audio/mpeg``).
        voice_id: The Polly voice used.
    """

    audio: bytes
    content_type: str = "audio/mpeg"
    voice_id: str = ""

    @property
    def is_empty(self) -> bool:
        """True when no audio was produced."""
        return not self.audio


class TextToSpeech:
    """Synthesizes speech from text using Amazon Polly."""

    def __init__(self, clients: AwsClientFactory, config: VoicePipelineConfig) -> None:
        """Initialise the TTS engine.

        Args:
            clients: Factory supplying the injected/created Polly client.
            config: Runtime config (voice id and engine).
        """
        self._clients = clients
        self._config = config

    def synthesize(
        self,
        text: str,
        *,
        voice_id: str | None = None,
        output_format: str = "mp3",
    ) -> SpeechAudio:
        """Synthesize ``text`` into speech audio.

        Args:
            text: The text to speak.
            voice_id: Optional Polly voice override (defaults to config).
            output_format: Polly output format (``mp3``/``ogg_vorbis``/``pcm``).

        Returns:
            A :class:`SpeechAudio`; empty audio on error or empty input.
        """
        if not text.strip():
            return SpeechAudio(audio=b"")
        voice = voice_id or self._config.polly_voice_id
        try:
            client = self._clients.polly()
            response = client.synthesize_speech(
                Text=text,
                VoiceId=voice,
                Engine=self._config.polly_engine,
                OutputFormat=output_format,
            )
            return self._read_stream(response, voice)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            log.warning("TTS failed, returning empty audio: %s", exc)
            return SpeechAudio(audio=b"", voice_id=voice)

    def _read_stream(self, response: Any, voice: str) -> SpeechAudio:
        """Normalise a Polly ``synthesize_speech`` response into audio bytes."""
        if not isinstance(response, dict):
            return SpeechAudio(audio=b"", voice_id=voice)
        stream = response.get("AudioStream")
        audio = _read_audio_bytes(stream)
        content_type = str(response.get("ContentType", "audio/mpeg"))
        return SpeechAudio(audio=audio, content_type=content_type, voice_id=voice)


def _read_audio_bytes(stream: Any) -> bytes:
    """Read audio bytes from a Polly stream (or passthrough bytes in tests)."""
    if stream is None:
        return b""
    if isinstance(stream, bytes | bytearray):
        return bytes(stream)
    if hasattr(stream, "read"):
        data = stream.read()
        return bytes(data) if data else b""
    return b""
