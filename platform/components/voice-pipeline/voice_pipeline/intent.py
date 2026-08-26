"""Intent recognition via Amazon Bedrock (InvokeModel).

A transcribed voice command is classified into a structured intent by prompting
a Bedrock model. The model returns a compact JSON object describing the intent
category, an optional subcommand, and parsed arguments. Access is over the AWS
SDK using the pod's IAM task role (no static keys); the Bedrock runtime client
is injected via :class:`~voice_pipeline.aws_clients.AwsClientFactory`.

There is NO self-hosted LLM here — the legacy Ollama/Speaches/self-hosted intent
path is removed. On any Bedrock error or unparseable response, the recognizer
degrades gracefully to a ``general`` intent carrying the raw transcript.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .aws_clients import AwsClientFactory
from .config import VoicePipelineConfig

log = logging.getLogger(__name__)

__all__ = ["IntentCategory", "Intent", "IntentRecognizer"]


class IntentCategory(str, Enum):
    """Top-level intent categories the pipeline can act on."""

    MUSIC = "music"
    ADMIN = "admin"
    GENERAL = "general"


@dataclass(frozen=True)
class Intent:
    """A structured intent derived from a transcript.

    Attributes:
        category: The intent category (music/admin/general).
        query: The originating transcript text.
        subcommand: Specific action (e.g. "play", "skip") when applicable.
        args: Parsed arguments (e.g. ``{"song": "..."}``).
    """

    category: IntentCategory
    query: str
    subcommand: str | None = None
    args: dict[str, Any] = field(default_factory=dict)


# Instruction sent to the Bedrock model. It is deliberately strict about output
# shape so the response is machine-parseable.
_SYSTEM_PROMPT = (
    "You classify a Discord music bot voice command into JSON. "
    "Respond with ONLY a JSON object and no prose. Schema: "
    '{"category": "music|admin|general", "subcommand": string|null, '
    '"args": object}. '
    "Use 'music' for playback (play, skip, pause, resume, stop, queue, "
    "shuffle, repeat, volume, lyrics). Use 'admin' for moderation (mute, kick, "
    "ban, timeout, ticket, revoke, restart, shutdown). Use 'general' for "
    "everything else. For play/add put the song text in args.song."
)


class IntentRecognizer:
    """Classifies transcripts into structured intents using Amazon Bedrock."""

    def __init__(self, clients: AwsClientFactory, config: VoicePipelineConfig) -> None:
        """Initialise the recognizer.

        Args:
            clients: Factory supplying the injected/created Bedrock client.
            config: Runtime config (supplies the Bedrock model id).
        """
        self._clients = clients
        self._config = config

    def recognize(self, transcript: str) -> Intent:
        """Recognize the intent of a transcript.

        Args:
            transcript: The recognized speech text.

        Returns:
            A structured :class:`Intent`; a ``general`` fallback on any error.
        """
        text = transcript.strip()
        if not text:
            return Intent(category=IntentCategory.GENERAL, query="")
        try:
            body = self._invoke_model(text)
            return self._parse_response(body, text)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            log.warning("Intent recognition failed, defaulting to general: %s", exc)
            return Intent(category=IntentCategory.GENERAL, query=text)

    def _invoke_model(self, transcript: str) -> dict[str, Any]:
        """Invoke the Bedrock model and return the parsed response envelope."""
        client = self._clients.bedrock_runtime()
        request = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 256,
            "system": _SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": transcript}],
        }
        response = client.invoke_model(
            modelId=self._config.bedrock_model_id,
            contentType="application/json",
            accept="application/json",
            body=json.dumps(request),
        )
        return _read_bedrock_body(response)

    def _parse_response(self, envelope: dict[str, Any], transcript: str) -> Intent:
        """Extract the model's JSON intent from a Bedrock response envelope."""
        raw_text = _extract_model_text(envelope)
        parsed = _safe_json_object(raw_text)
        if parsed is None:
            return Intent(category=IntentCategory.GENERAL, query=transcript)
        category = _coerce_category(parsed.get("category"))
        subcommand = parsed.get("subcommand")
        subcommand = str(subcommand) if subcommand else None
        args = parsed.get("args")
        args = dict(args) if isinstance(args, dict) else {}
        return Intent(
            category=category,
            query=transcript,
            subcommand=subcommand,
            args=args,
        )


def _read_bedrock_body(response: Any) -> dict[str, Any]:
    """Read and JSON-decode a Bedrock ``invoke_model`` response body."""
    if not isinstance(response, dict):
        return {}
    body = response.get("body")
    if body is None:
        return {}
    # Real botocore returns a streaming body with ``.read()``; fakes may return
    # a dict, bytes, or str directly.
    if hasattr(body, "read"):
        body = body.read()
    if isinstance(body, bytes | bytearray):
        body = body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {}
    if isinstance(body, dict):
        return body
    return {}


def _extract_model_text(envelope: dict[str, Any]) -> str:
    """Extract the assistant text from an Anthropic-on-Bedrock envelope."""
    content = envelope.get("content")
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type", "text") == "text"
        ]
        return "".join(parts).strip()
    # Some models place the text directly under a top-level key.
    for key in ("completion", "output_text", "text"):
        value = envelope.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


def _safe_json_object(text: str) -> dict[str, Any] | None:
    """Parse the first JSON object found in ``text`` (tolerating surrounding prose)."""
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_category(value: Any) -> IntentCategory:
    """Map an arbitrary category value to a valid :class:`IntentCategory`."""
    try:
        return IntentCategory(str(value).strip().lower())
    except ValueError:
        return IntentCategory.GENERAL
