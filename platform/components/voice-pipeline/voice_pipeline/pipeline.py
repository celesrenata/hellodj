"""Voice pipeline orchestration: wakeword -> STT -> intent -> action -> TTS.

This module ties the component together. It consumes Discord voice **opus**
frames handed over by ``discord-bot-core`` (this component never touches
discord.py directly), runs the local ONNX wake word detector over a rolling
mel-spectrogram window, and — on detection — captures the following utterance,
transcribes it via Amazon Transcribe, recognizes the intent via Amazon Bedrock,
dispatches the resulting action to the ``playback-orchestrator``, and synthesizes
a spoken reply via Amazon Polly.

The local wake word ONNX model is the ONLY on-box AI. STT/intent/TTS are managed
AWS AI reached over the IAM task role. ``numpy`` is imported lazily so the module
imports without the wheel installed (lint/compile CI).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .aws_clients import AwsClientFactory
from .config import VoicePipelineConfig
from .intent import Intent, IntentRecognizer
from .orchestrator_client import (
    ActionRequest,
    ActionResult,
    OrchestratorActionClient,
)
from .stt import SpeechToText, Transcript
from .tts import SpeechAudio, TextToSpeech
from .wakeword import WakeWordModel

log = logging.getLogger(__name__)

__all__ = ["VoiceContext", "VoiceInteractionResult", "VoicePipeline"]

# Wake word model input geometry (matches the custom Hello_DJ.onnx model).
_MEL_BINS = 96
_MEL_HISTORY = 16  # time-steps -> input shape (1, 16, 96)
_WINDOW_SAMPLES = 1280  # 80 ms at 16 kHz per mel slice


@dataclass(frozen=True)
class VoiceContext:
    """Identifies who/where a voice interaction is happening.

    Attributes:
        guild_id: Discord guild id.
        channel_id: Voice channel id.
        user_id: Speaking user's Discord id.
    """

    guild_id: int
    channel_id: int
    user_id: int


@dataclass
class VoiceInteractionResult:
    """The end-to-end outcome of a single voice interaction.

    Attributes:
        transcript: The recognized speech.
        intent: The structured intent.
        action_result: The orchestrator's response (None if not dispatched).
        reply_audio: Synthesized spoken reply (None if not synthesized).
    """

    transcript: Transcript
    intent: Intent
    action_result: ActionResult | None = None
    reply_audio: SpeechAudio | None = None


@dataclass
class _SpeakerState:
    """Per-speaker rolling audio state used for wake word detection/capture."""

    mel_history: list[Any] = field(default_factory=list)
    capture: bytearray = field(default_factory=bytearray)
    capturing: bool = False


class VoicePipeline:
    """Orchestrates the full voice interaction across managed AWS AI services.

    Construct with either explicit collaborators (for tests) or via
    :meth:`from_config`, which wires the standard implementations around an
    :class:`AwsClientFactory`. All AWS access uses the pod's IAM task role.
    """

    def __init__(
        self,
        wakeword: WakeWordModel,
        stt: SpeechToText,
        intent: IntentRecognizer,
        tts: TextToSpeech,
        orchestrator: OrchestratorActionClient,
        config: VoicePipelineConfig,
    ) -> None:
        """Initialise the pipeline with its collaborators.

        Args:
            wakeword: Local ONNX wake word detector.
            stt: Amazon Transcribe speech-to-text engine.
            intent: Amazon Bedrock intent recognizer.
            tts: Amazon Polly text-to-speech engine.
            orchestrator: Typed action-dispatch client.
            config: Runtime configuration.
        """
        self._wakeword = wakeword
        self._stt = stt
        self._intent = intent
        self._tts = tts
        self._orchestrator = orchestrator
        self._config = config
        self._speakers: dict[int, _SpeakerState] = {}

    @classmethod
    def from_config(
        cls,
        config: VoicePipelineConfig,
        transport: Any,
        *,
        clients: AwsClientFactory | None = None,
    ) -> VoicePipeline:
        """Build a pipeline with the standard collaborators.

        Args:
            config: Runtime configuration.
            transport: Async HTTP transport for the orchestrator client.
            clients: Optional pre-built AWS client factory (injected in tests);
                defaults to one bound to ``config.aws_region`` using the IAM
                task role via the boto3 default credential chain.

        Returns:
            A fully wired :class:`VoicePipeline`.
        """
        factory = clients or AwsClientFactory(region=config.aws_region)
        return cls(
            wakeword=WakeWordModel(
                config.wakeword_model_path,
                threshold=config.wakeword_threshold,
            ),
            stt=SpeechToText(factory, config),
            intent=IntentRecognizer(factory, config),
            tts=TextToSpeech(factory, config),
            orchestrator=OrchestratorActionClient(config.orchestrator_base_url, transport),
            config=config,
        )

    # ── opus ingestion (from bot-core) ────────────────────────────────────

    def on_opus_frame(self, ssrc: int, pcm_16k: bytes) -> bool:
        """Feed one decoded 16 kHz mono int16 PCM frame for a speaker.

        ``discord-bot-core`` owns opus receipt and decode; it hands this
        component decoded PCM per speaker (SSRC). This method updates the rolling
        wake word window and, once triggered, accumulates the utterance for STT.

        Args:
            ssrc: Discord voice SSRC identifying the speaker.
            pcm_16k: One frame of little-endian int16 PCM at 16 kHz, mono.

        Returns:
            ``True`` if this frame completed a wake word detection (the caller
            should then finalize the utterance via :meth:`process_utterance`).
        """
        state = self._speakers.setdefault(ssrc, _SpeakerState())
        if state.capturing:
            state.capture.extend(pcm_16k)
            return False
        detected = self._update_wakeword(state, pcm_16k)
        if detected:
            state.capturing = True
            state.capture = bytearray()
            log.info("Wake word detected for ssrc=%s", ssrc)
        return detected

    def _update_wakeword(self, state: _SpeakerState, pcm_16k: bytes) -> bool:
        """Update the rolling mel window and run wake word inference."""
        if not self._wakeword.available:
            return False
        try:
            import numpy as np  # noqa: PLC0415 - intentional lazy import
        except ImportError:
            return False
        samples = np.frombuffer(pcm_16k, dtype=np.int16)
        if samples.size == 0:
            return False
        window = samples[-_WINDOW_SAMPLES:]
        mel_slice = _mel_slice(window, np)
        state.mel_history.append(mel_slice)
        if len(state.mel_history) > _MEL_HISTORY:
            state.mel_history.pop(0)
        if len(state.mel_history) < _MEL_HISTORY:
            return False
        mel_input = np.stack(state.mel_history[-_MEL_HISTORY:], axis=0)[np.newaxis, :, :]
        return self._wakeword.predict(mel_input)

    def take_capture(self, ssrc: int) -> bytes:
        """Return and clear the captured utterance PCM for a speaker."""
        state = self._speakers.get(ssrc)
        if state is None:
            return b""
        captured = bytes(state.capture)
        state.capture = bytearray()
        state.capturing = False
        state.mel_history.clear()
        return captured

    # ── end-to-end processing ─────────────────────────────────────────────

    async def process_utterance(
        self,
        pcm_16k: bytes,
        context: VoiceContext,
    ) -> VoiceInteractionResult:
        """Run the full STT -> intent -> action -> TTS flow for an utterance.

        Args:
            pcm_16k: Captured utterance PCM (16 kHz mono int16).
            context: Guild/channel/user context for the interaction.

        Returns:
            A :class:`VoiceInteractionResult` capturing every stage's output.
            Each stage degrades gracefully, so a partial result is returned
            rather than raising when a managed service is unavailable.
        """
        transcript = self._stt.transcribe(pcm_16k)
        if transcript.is_empty:
            return VoiceInteractionResult(
                transcript=transcript,
                intent=self._intent.recognize(""),
            )
        intent = self._intent.recognize(transcript.text)
        action_result = await self._dispatch(intent, context)
        reply_audio = self._speak(action_result)
        return VoiceInteractionResult(
            transcript=transcript,
            intent=intent,
            action_result=action_result,
            reply_audio=reply_audio,
        )

    async def _dispatch(self, intent: Intent, context: VoiceContext) -> ActionResult | None:
        """Dispatch a recognized intent to the orchestrator, degrading on error."""
        request = ActionRequest(
            category=intent.category.value,
            guild_id=context.guild_id,
            channel_id=context.channel_id,
            requested_by=context.user_id,
            subcommand=intent.subcommand,
            query=intent.query,
            args=intent.args,
        )
        try:
            return await self._orchestrator.dispatch(request)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            log.warning("Action dispatch failed: %s", exc)
            return None

    def _speak(self, action_result: ActionResult | None) -> SpeechAudio | None:
        """Synthesize a spoken reply from the orchestrator's message."""
        if action_result is None or not action_result.message:
            return None
        return self._tts.synthesize(action_result.message)


def _mel_slice(window: Any, np: Any) -> Any:
    """Compute a 96-bin log-mel slice for an ~80 ms window at 16 kHz.

    A compact, dependency-light STFT + mel filterbank (no librosa) matching the
    model's expected feature layout. Returns a ``float32`` array of length 96.
    """
    win_length = 640
    padded = window.astype(np.float32)
    if padded.size < win_length:
        padded = np.pad(padded, (0, win_length - padded.size))
    else:
        padded = padded[:win_length]
    windowed = padded * np.hanning(win_length).astype(np.float32)
    spectrum = np.fft.rfft(windowed, n=512)
    power = np.abs(spectrum) ** 2
    freqs = np.linspace(0, 8000, power.size)
    bank = _mel_filterbank(freqs, np)
    mel = np.dot(power, bank.T)
    mel = np.log(np.maximum(mel, 1e-10))
    mel = (mel - np.mean(mel)) / (np.std(mel) + 1e-6)
    return mel.astype(np.float32)


def _mel_filterbank(freqs: Any, np: Any) -> Any:
    """Build a triangular mel filterbank of shape ``(96, len(freqs))``."""

    def hz_to_mel(hz: Any) -> Any:
        return 2595.0 * np.log10(1.0 + hz / 700.0)

    def mel_to_hz(mel: Any) -> Any:
        return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)

    points = mel_to_hz(np.linspace(hz_to_mel(0.0), hz_to_mel(8000.0), _MEL_BINS + 2))
    bank = np.zeros((_MEL_BINS, freqs.size), dtype=np.float32)
    for i in range(_MEL_BINS):
        low, center, high = points[i], points[i + 1], points[i + 2]
        left = (freqs - low) / max(center - low, 1e-6)
        right = (high - freqs) / max(high - center, 1e-6)
        bank[i] = np.clip(np.minimum(left, right), 0.0, None)
    return bank
