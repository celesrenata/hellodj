"""Unit tests for the voice-pipeline's managed-AWS-AI integration (mocked).

These tests exercise the STT (Amazon Transcribe) -> intent (Amazon Bedrock) ->
action (playback-orchestrator) -> TTS (Amazon Polly) flow using in-process fakes
injected via :class:`AwsClientFactory` and a fake async :class:`Transport`. No
live AWS calls, credentials, boto3, or network are involved.

Covers Requirement 6.3: the voice command pipeline (wake word -> STT -> intent ->
action -> TTS response) is preserved on the Bedrock-backed re-platform, including
graceful degradation when a managed service or the orchestrator is unavailable.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from voice_pipeline.aws_clients import AwsClientFactory
from voice_pipeline.config import VoicePipelineConfig
from voice_pipeline.intent import IntentCategory, IntentRecognizer
from voice_pipeline.orchestrator_client import OrchestratorActionClient
from voice_pipeline.pipeline import VoiceContext, VoicePipeline
from voice_pipeline.stt import SpeechToText
from voice_pipeline.tts import TextToSpeech

# ── Fakes: managed AWS AI clients ──────────────────────────────────────────


class FakeTranscribeClient:
    """Stands in for the injected Transcribe adapter (``transcribe_pcm``)."""

    def __init__(self, response: Any = None, *, raise_exc: Exception | None = None) -> None:
        self._response = response
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    def transcribe_pcm(self, *, audio: bytes, sample_rate_hz: int, language_code: str) -> Any:
        self.calls.append(
            {"audio": audio, "sample_rate_hz": sample_rate_hz, "language_code": language_code}
        )
        if self._raise is not None:
            raise self._raise
        return self._response


class _BodyStream:
    """Mimics botocore's streaming body with a single ``.read()``."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class FakeBedrockClient:
    """Stands in for the injected Bedrock runtime client (``invoke_model``)."""

    def __init__(self, intent_json: Any = None, *, raise_exc: Exception | None = None) -> None:
        self._intent_json = intent_json
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    def invoke_model(self, *, modelId: str, contentType: str, accept: str, body: str) -> Any:  # noqa: N803 - boto3 kwarg names
        self.calls.append({"modelId": modelId, "body": body})
        if self._raise is not None:
            raise self._raise
        # Wrap the intent JSON in an Anthropic-on-Bedrock content envelope.
        envelope = {"content": [{"type": "text", "text": json.dumps(self._intent_json)}]}
        return {"body": _BodyStream(json.dumps(envelope).encode("utf-8"))}


class FakeBedrockRawTextClient:
    """Bedrock fake that returns raw (non-JSON) assistant text."""

    def __init__(self, text: str) -> None:
        self._text = text

    def invoke_model(self, *, modelId: str, contentType: str, accept: str, body: str) -> Any:  # noqa: N803
        envelope = {"content": [{"type": "text", "text": self._text}]}
        return {"body": json.dumps(envelope).encode("utf-8")}


class FakePollyClient:
    """Stands in for the injected Polly client (``synthesize_speech``)."""

    def __init__(self, audio: bytes = b"", *, raise_exc: Exception | None = None) -> None:
        self._audio = audio
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    def synthesize_speech(self, *, Text: str, VoiceId: str, Engine: str, OutputFormat: str) -> Any:  # noqa: N803 - boto3 kwarg names
        self.calls.append({"Text": Text, "VoiceId": VoiceId, "Engine": Engine})
        if self._raise is not None:
            raise self._raise
        return {"AudioStream": _BodyStream(self._audio), "ContentType": "audio/mpeg"}


class FakeTransport:
    """Fake async Transport recording orchestrator dispatches."""

    def __init__(self, response: Any = None, *, raise_exc: Exception | None = None) -> None:
        self._response = response if response is not None else {"ok": True, "message": ""}
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    async def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"url": url, "payload": payload})
        if self._raise is not None:
            raise self._raise
        return self._response


# ── Helpers ─────────────────────────────────────────────────────────────────


def _factory(**clients: Any) -> AwsClientFactory:
    return AwsClientFactory(region="us-east-1", **clients)


def _config() -> VoicePipelineConfig:
    return VoicePipelineConfig()


_PLAY_INTENT = {"category": "music", "subcommand": "play", "args": {"song": "x"}}


# ── STT tests (Amazon Transcribe) ────────────────────────────────────────────


def test_stt_returns_transcript_from_fake_transcribe() -> None:
    fake = FakeTranscribeClient(
        {"transcript": "play some jazz", "confidence": 0.92, "isFinal": True}
    )
    stt = SpeechToText(_factory(transcribe=fake), _config())

    result = stt.transcribe(b"\x01\x02" * 100)

    assert result.text == "play some jazz"
    assert result.confidence == pytest.approx(0.92)
    assert result.is_final is True
    assert not result.is_empty
    # PCM and config were forwarded to the managed client.
    assert fake.calls[0]["sample_rate_hz"] == 16000
    assert fake.calls[0]["language_code"] == "en-US"


def test_stt_empty_pcm_yields_empty_transcript_without_calling_client() -> None:
    fake = FakeTranscribeClient({"transcript": "should not be used"})
    stt = SpeechToText(_factory(transcribe=fake), _config())

    result = stt.transcribe(b"")

    assert result.is_empty
    assert result.text == ""
    assert fake.calls == []  # empty input short-circuits before the AWS call


def test_stt_client_error_degrades_to_empty_transcript() -> None:
    fake = FakeTranscribeClient(raise_exc=RuntimeError("transcribe unavailable"))
    stt = SpeechToText(_factory(transcribe=fake), _config())

    result = stt.transcribe(b"\x10\x20" * 50)

    assert result.is_empty  # graceful degradation, no exception propagated


# ── Intent tests (Amazon Bedrock) ────────────────────────────────────────────


def test_intent_parses_structured_intent_from_bedrock_envelope() -> None:
    fake = FakeBedrockClient(_PLAY_INTENT)
    recognizer = IntentRecognizer(_factory(bedrock=fake), _config())

    intent = recognizer.recognize("play x")

    assert intent.category is IntentCategory.MUSIC
    assert intent.subcommand == "play"
    assert intent.args == {"song": "x"}
    assert intent.query == "play x"
    # The configured Bedrock model id was used for InvokeModel.
    assert fake.calls[0]["modelId"] == _config().bedrock_model_id


def test_intent_empty_transcript_is_general_without_calling_bedrock() -> None:
    fake = FakeBedrockClient(_PLAY_INTENT)
    recognizer = IntentRecognizer(_factory(bedrock=fake), _config())

    intent = recognizer.recognize("   ")

    assert intent.category is IntentCategory.GENERAL
    assert intent.query == ""
    assert fake.calls == []


def test_intent_malformed_response_falls_back_to_general() -> None:
    fake = FakeBedrockRawTextClient("this is not json at all")
    recognizer = IntentRecognizer(_factory(bedrock=fake), _config())

    intent = recognizer.recognize("what is the weather")

    assert intent.category is IntentCategory.GENERAL
    assert intent.query == "what is the weather"


def test_intent_bedrock_error_falls_back_to_general() -> None:
    fake = FakeBedrockClient(raise_exc=RuntimeError("bedrock throttled"))
    recognizer = IntentRecognizer(_factory(bedrock=fake), _config())

    intent = recognizer.recognize("play x")

    assert intent.category is IntentCategory.GENERAL
    assert intent.query == "play x"


# ── TTS tests (Amazon Polly) ─────────────────────────────────────────────────


def test_tts_returns_audio_from_fake_polly() -> None:
    fake = FakePollyClient(audio=b"ID3-mp3-bytes")
    tts = TextToSpeech(_factory(polly=fake), _config())

    speech = tts.synthesize("Now playing jazz")

    assert speech.audio == b"ID3-mp3-bytes"
    assert not speech.is_empty
    assert speech.content_type == "audio/mpeg"
    assert fake.calls[0]["VoiceId"] == _config().polly_voice_id
    assert fake.calls[0]["Engine"] == _config().polly_engine


def test_tts_empty_text_yields_empty_audio_without_calling_polly() -> None:
    fake = FakePollyClient(audio=b"unused")
    tts = TextToSpeech(_factory(polly=fake), _config())

    speech = tts.synthesize("   ")

    assert speech.is_empty
    assert fake.calls == []


def test_tts_polly_error_degrades_to_empty_audio() -> None:
    fake = FakePollyClient(raise_exc=RuntimeError("polly unavailable"))
    tts = TextToSpeech(_factory(polly=fake), _config())

    speech = tts.synthesize("Now playing jazz")

    assert speech.is_empty  # graceful degradation


# ── Full pipeline flow (STT -> intent -> action -> TTS) ──────────────────────


@pytest.fixture
def context() -> VoiceContext:
    return VoiceContext(guild_id=111, channel_id=222, user_id=333)


def _build_pipeline(
    *,
    transcribe: Any,
    bedrock: Any,
    polly: Any,
    transport: FakeTransport,
) -> VoicePipeline:
    config = _config()
    factory = _factory(transcribe=transcribe, bedrock=bedrock, polly=polly)
    return VoicePipeline(
        wakeword=_DummyWakeWord(),
        stt=SpeechToText(factory, config),
        intent=IntentRecognizer(factory, config),
        tts=TextToSpeech(factory, config),
        orchestrator=OrchestratorActionClient(config.orchestrator_base_url, transport),
        config=config,
    )


class _DummyWakeWord:
    """Minimal wake word stand-in; unused by process_utterance directly."""

    available = False

    def predict(self, _mel_input: Any) -> bool:
        return False


async def test_full_flow_stt_intent_action_tts(context: VoiceContext) -> None:
    transcribe = FakeTranscribeClient({"transcript": "play x", "confidence": 0.9})
    bedrock = FakeBedrockClient(_PLAY_INTENT)
    polly = FakePollyClient(audio=b"reply-audio")
    transport = FakeTransport({"ok": True, "message": "Now playing x", "data": {}})
    pipeline = _build_pipeline(
        transcribe=transcribe, bedrock=bedrock, polly=polly, transport=transport
    )

    result = await pipeline.process_utterance(b"\x01\x02" * 200, context)

    # STT stage
    assert result.transcript.text == "play x"
    # Intent stage
    assert result.intent.category is IntentCategory.MUSIC
    assert result.intent.subcommand == "play"
    # Action stage: dispatched to the orchestrator with voice context.
    assert result.action_result is not None
    assert result.action_result.ok is True
    assert result.action_result.message == "Now playing x"
    assert transport.calls, "orchestrator should have been dispatched"
    payload = transport.calls[0]["payload"]
    assert payload["source"] == "voice"
    assert payload["category"] == "music"
    assert payload["guildId"] == "111"
    assert payload["requestedBy"] == "333"
    # TTS stage: reply synthesized from the orchestrator message.
    assert result.reply_audio is not None
    assert result.reply_audio.audio == b"reply-audio"
    assert polly.calls[0]["Text"] == "Now playing x"


async def test_full_flow_empty_transcript_skips_dispatch_and_tts(context: VoiceContext) -> None:
    transcribe = FakeTranscribeClient({"transcript": ""})
    bedrock = FakeBedrockClient(_PLAY_INTENT)
    polly = FakePollyClient(audio=b"unused")
    transport = FakeTransport({"ok": True, "message": "unused"})
    pipeline = _build_pipeline(
        transcribe=transcribe, bedrock=bedrock, polly=polly, transport=transport
    )

    result = await pipeline.process_utterance(b"\x01\x02" * 10, context)

    assert result.transcript.is_empty
    assert result.action_result is None
    assert result.reply_audio is None
    assert transport.calls == []  # nothing dispatched
    assert polly.calls == []  # nothing synthesized


async def test_full_flow_orchestrator_failure_degrades_gracefully(context: VoiceContext) -> None:
    transcribe = FakeTranscribeClient({"transcript": "play x", "confidence": 0.9})
    bedrock = FakeBedrockClient(_PLAY_INTENT)
    polly = FakePollyClient(audio=b"unused")
    transport = FakeTransport(raise_exc=ConnectionError("orchestrator down"))
    pipeline = _build_pipeline(
        transcribe=transcribe, bedrock=bedrock, polly=polly, transport=transport
    )

    # Must not raise despite the transport failing.
    result = await pipeline.process_utterance(b"\x01\x02" * 200, context)

    assert result.transcript.text == "play x"
    assert result.intent.category is IntentCategory.MUSIC
    assert result.action_result is None  # dispatch failed, degraded to None
    assert result.reply_audio is None  # no message -> no TTS
    assert transport.calls, "dispatch was attempted before failing"
