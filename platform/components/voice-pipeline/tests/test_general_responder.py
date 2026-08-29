"""Tests for the Bedrock general-query responder + web-search MCP client (mocked).

These exercise the "answer a general/basic voice question briefly, optionally via
web search" path added to the voice-pipeline: the intent recognizer classifies an
utterance as ``general`` and the pipeline routes it to :class:`GeneralResponder`
(Bedrock Converse) with a ``web_search`` tool backed by ``mcp-searxng-enhanced``,
then speaks the answer via Polly instead of dispatching to the orchestrator.

No live AWS/HTTP: the Bedrock client is a fake exposing ``converse``, the
mcp-searxng-enhanced HTTP call is a fake :class:`SearchTransport`, and Polly is a
fake ``synthesize_speech``. Every collaborator degrades gracefully.
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
from voice_pipeline.responder import GeneralResponder
from voice_pipeline.stt import SpeechToText
from voice_pipeline.tts import TextToSpeech
from voice_pipeline.web_search import WebSearchClient

# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeConverseClient:
    """Bedrock fake exposing ``converse`` with a scripted sequence of responses.

    Each call pops the next scripted response so a tool-use round followed by a
    final-answer round can be simulated deterministically.
    """

    def __init__(self, responses: list[Any], *, raise_exc: Exception | None = None) -> None:
        self._responses = list(responses)
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        if not self._responses:
            return _text_response("")
        return self._responses.pop(0)


class FakeSearchTransport:
    """Fake MCP-over-HTTP transport recording the JSON-RPC envelopes it receives."""

    def __init__(self, response: Any = None, *, raise_exc: Exception | None = None) -> None:
        self._response = response
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    async def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"url": url, "payload": payload})
        if self._raise is not None:
            raise self._raise
        return self._response if self._response is not None else {}


class FakePollyClient:
    """Minimal Polly fake (synthesize_speech)."""

    def __init__(self, audio: bytes = b"") -> None:
        self._audio = audio
        self.calls: list[dict[str, Any]] = []

    def synthesize_speech(self, *, Text: str, VoiceId: str, Engine: str, OutputFormat: str) -> Any:  # noqa: N803
        self.calls.append({"Text": Text, "VoiceId": VoiceId})
        return {"AudioStream": self._audio, "ContentType": "audio/mpeg"}


class FakeTranscribeClient:
    """Minimal Transcribe fake returning a fixed transcript."""

    def __init__(self, transcript: str) -> None:
        self._transcript = transcript

    def transcribe_pcm(self, *, audio: bytes, sample_rate_hz: int, language_code: str) -> Any:
        return {"transcript": self._transcript, "confidence": 0.9}


class FakeBedrockIntentClient:
    """Bedrock intent fake (invoke_model) that always classifies as general."""

    def invoke_model(self, *, modelId: str, contentType: str, accept: str, body: str) -> Any:  # noqa: N803
        envelope = {
            "content": [
                {"type": "text", "text": json.dumps({"category": "general", "args": {}})}
            ]
        }
        return {"body": json.dumps(envelope).encode("utf-8")}


class FakeActionTransport:
    """Orchestrator transport that must NOT be called for general queries."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"url": url, "payload": payload})
        return {"ok": True, "message": "should not be used"}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _text_response(text: str) -> dict[str, Any]:
    """A Converse response whose assistant message is a single text block."""
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "stopReason": "end_turn",
    }


def _tool_use_response(query: str, *, tool_use_id: str = "tu-1") -> dict[str, Any]:
    """A Converse response requesting the web_search tool."""
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [
                    {"toolUse": {"toolUseId": tool_use_id, "name": "web_search",
                                 "input": {"query": query}}}
                ],
            }
        },
        "stopReason": "tool_use",
    }


def _mcp_search_response(results: list[dict[str, str]]) -> dict[str, Any]:
    """An MCP tools/call response whose text content is a JSON results list."""
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps(results)}]},
    }


def _config(**overrides: Any) -> VoicePipelineConfig:
    base = {"searxng_mcp_url": "http://mcp-searxng-enhanced:8000", "web_search_enabled": True}
    base.update(overrides)
    return VoicePipelineConfig(**base)


def _factory(bedrock: Any = None, polly: Any = None, transcribe: Any = None) -> AwsClientFactory:
    return AwsClientFactory(region="us-east-1", bedrock=bedrock, polly=polly, transcribe=transcribe)


# ── WebSearchClient tests ─────────────────────────────────────────────────────


async def test_web_search_parses_results_from_mcp_envelope() -> None:
    transport = FakeSearchTransport(
        _mcp_search_response(
            [
                {"title": "Paris weather", "url": "https://ex.com/a", "content": "18C sunny"},
                {"title": "Forecast", "url": "https://ex.com/b", "content": "rain later"},
            ]
        )
    )
    client = WebSearchClient("http://mcp:8000", transport, max_results=5)

    results = await client.search("weather in paris")

    assert not results.is_empty
    assert results.results[0].title == "Paris weather"
    assert results.results[0].url == "https://ex.com/a"
    # The JSON-RPC envelope targeted the /mcp endpoint and the search_web tool.
    call = transport.calls[0]
    assert call["url"] == "http://mcp:8000/mcp"
    assert call["payload"]["method"] == "tools/call"
    assert call["payload"]["params"]["name"] == "search_web"
    assert call["payload"]["params"]["arguments"]["query"] == "weather in paris"


async def test_web_search_empty_query_skips_transport() -> None:
    transport = FakeSearchTransport(_mcp_search_response([]))
    client = WebSearchClient("http://mcp:8000", transport)

    results = await client.search("   ")

    assert results.is_empty
    assert transport.calls == []


async def test_web_search_transport_error_degrades_to_empty() -> None:
    transport = FakeSearchTransport(raise_exc=ConnectionError("mcp down"))
    client = WebSearchClient("http://mcp:8000", transport)

    results = await client.search("anything")

    assert results.is_empty  # no exception propagated


async def test_web_search_respects_max_results() -> None:
    many = [{"title": f"t{i}", "url": f"https://ex.com/{i}", "content": "c"} for i in range(10)]
    transport = FakeSearchTransport(_mcp_search_response(many))
    client = WebSearchClient("http://mcp:8000", transport, max_results=3)

    results = await client.search("q")

    assert len(results.results) == 3


# ── GeneralResponder tests ────────────────────────────────────────────────────


async def test_responder_answers_directly_without_tool() -> None:
    bedrock = FakeConverseClient([_text_response("It is Tuesday.")])
    responder = GeneralResponder(_factory(bedrock=bedrock), _config(searxng_mcp_url=""))

    answer = await responder.answer("what day is it")

    assert answer == "It is Tuesday."
    assert len(bedrock.calls) == 1
    # No web-search endpoint configured -> no toolConfig offered.
    assert "toolConfig" not in bedrock.calls[0]


async def test_responder_runs_web_search_tool_then_answers() -> None:
    bedrock = FakeConverseClient(
        [_tool_use_response("current weather paris"), _text_response("It's 18 degrees and sunny.")]
    )
    transport = FakeSearchTransport(
        _mcp_search_response([{"title": "Paris", "url": "https://ex.com", "content": "18C sunny"}])
    )
    web = WebSearchClient("http://mcp:8000", transport)
    responder = GeneralResponder(_factory(bedrock=bedrock), _config(), web_search=web)

    answer = await responder.answer("what's the weather in paris")

    assert answer == "It's 18 degrees and sunny."
    # First call offered the tool; the search ran; a second call produced the answer.
    assert "toolConfig" in bedrock.calls[0]
    assert transport.calls, "web_search tool should have been invoked"
    # The second converse call carried the tool result back to the model.
    second_messages = bedrock.calls[1]["messages"]
    assert any(
        isinstance(b, dict) and "toolResult" in b
        for m in second_messages
        for b in (m.get("content") or [])
    )


async def test_responder_bedrock_error_degrades_to_apology() -> None:
    bedrock = FakeConverseClient([], raise_exc=RuntimeError("bedrock throttled"))
    responder = GeneralResponder(_factory(bedrock=bedrock), _config(searxng_mcp_url=""))

    answer = await responder.answer("anything")

    assert answer == "Sorry, I can't answer that right now."


async def test_responder_empty_query_returns_empty() -> None:
    bedrock = FakeConverseClient([_text_response("unused")])
    responder = GeneralResponder(_factory(bedrock=bedrock), _config(searxng_mcp_url=""))

    answer = await responder.answer("   ")

    assert answer == ""
    assert bedrock.calls == []


# ── Pipeline general-branch tests ─────────────────────────────────────────────


@pytest.fixture
def context() -> VoiceContext:
    return VoiceContext(guild_id=1, channel_id=2, user_id=3)


def _pipeline(
    *, bedrock_converse: Any, polly: Any, action_transport: Any, web: Any
) -> VoicePipeline:
    config = _config()
    # Intent factory always classifies general; converse factory answers.
    intent_factory = _factory(bedrock=FakeBedrockIntentClient())
    converse_factory = _factory(bedrock=bedrock_converse, polly=polly,
                                transcribe=FakeTranscribeClient("what is the capital of france"))
    responder = GeneralResponder(converse_factory, config, web_search=web)
    return VoicePipeline(
        wakeword=_DummyWake(),
        stt=SpeechToText(converse_factory, config),
        intent=IntentRecognizer(intent_factory, config),
        tts=TextToSpeech(converse_factory, config),
        orchestrator=OrchestratorActionClient(config.orchestrator_base_url, action_transport),
        config=config,
        responder=responder,
    )


class _DummyWake:
    available = False

    def predict(self, _mel: Any) -> bool:
        return False


async def test_pipeline_general_query_answered_not_dispatched(context: VoiceContext) -> None:
    bedrock = FakeConverseClient([_text_response("Paris is the capital of France.")])
    polly = FakePollyClient(audio=b"spoken-answer")
    action_transport = FakeActionTransport()
    pipeline = _pipeline(bedrock_converse=bedrock, polly=polly,
                         action_transport=action_transport, web=None)

    result = await pipeline.process_utterance(b"\x01\x02" * 100, context)

    assert result.intent.category is IntentCategory.GENERAL
    assert result.answer_text == "Paris is the capital of France."
    assert result.action_result is None
    assert result.reply_audio is not None
    assert result.reply_audio.audio == b"spoken-answer"
    # The orchestrator was NOT called for a general query.
    assert action_transport.calls == []
    assert polly.calls[0]["Text"] == "Paris is the capital of France."
