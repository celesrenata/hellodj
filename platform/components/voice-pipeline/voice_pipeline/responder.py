"""Brief, spoken answers to general voice queries via Amazon Bedrock (Converse).

When the intent recognizer classifies an utterance as ``general`` (not a music
or admin command), the pipeline routes it here instead of the orchestrator. The
responder asks a small, cheap Bedrock model (Amazon Nova Micro by default) for a
one-to-two-sentence answer suitable for reading aloud, and gives the model a
single ``web_search`` tool backed by the in-cluster ``mcp-searxng-enhanced``
server for questions that need current information.

Everything is reached over the model-agnostic **Bedrock Converse API** so the
model is swappable via one env var. The Bedrock client is supplied by
:class:`~voice_pipeline.aws_clients.AwsClientFactory` (keyless IAM task role),
and the optional web-search client is injected. On any error the responder
degrades to a short spoken apology rather than raising into the pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

from .aws_clients import AwsClientFactory
from .config import VoicePipelineConfig
from .web_search import WebSearchClient

log = logging.getLogger(__name__)

__all__ = ["GeneralResponder"]

# Kept deliberately terse: answers are spoken via Polly, so brevity is the goal.
_SYSTEM_PROMPT = (
    "You are HelloDJ, a Discord music bot's voice assistant. Answer the user's "
    "question in one or two short sentences, in a natural spoken style, because "
    "your reply is read aloud. Do not use markdown, lists, or emoji. When the "
    "question needs current or factual information you are unsure of, use the "
    "web_search tool once, then answer briefly from the results. If you cannot "
    "answer, say so in one sentence."
)

# The single tool the model may call. The Converse toolSpec schema is a JSON
# schema object; we keep it minimal (a query + optional category).
_WEB_SEARCH_TOOL = {
    "toolSpec": {
        "name": "web_search",
        "description": (
            "Search the web for current or factual information and return a few "
            "brief result snippets with source URLs."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                    "category": {
                        "type": "string",
                        "description": (
                            "Optional SearXNG category: general, news, it, "
                            "science, social media, videos, images, files, map."
                        ),
                    },
                },
                "required": ["query"],
            }
        },
    }
}

_DECLINE_MESSAGE = "Sorry, I can't answer that right now."
# Bound the tool loop so a misbehaving model can never spin forever.
_MAX_TOOL_ROUNDS = 3


class GeneralResponder:
    """Answers general voice queries briefly via Bedrock Converse + web search."""

    def __init__(
        self,
        clients: AwsClientFactory,
        config: VoicePipelineConfig,
        *,
        web_search: WebSearchClient | None = None,
    ) -> None:
        """Initialise the responder.

        Args:
            clients: Factory supplying the (injected/created) Bedrock client.
            config: Runtime config (model id, token budget, region).
            web_search: Optional web-search client. When ``None`` (or config
                disables it) the model answers without the ``web_search`` tool.
        """
        self._clients = clients
        self._config = config
        self._web_search = web_search

    @property
    def _tool_enabled(self) -> bool:
        """True when the web-search tool should be offered to the model."""
        return self._web_search is not None and self._config.web_search_enabled

    async def answer(self, query: str) -> str:
        """Return a brief spoken answer to ``query`` (never raises).

        Args:
            query: The user's transcribed general question.

        Returns:
            A short answer string; a one-sentence apology on any failure.
        """
        text = query.strip()
        if not text:
            return ""
        try:
            return await self._converse_loop(text)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            log.warning("general responder failed: %s", exc)
            return _DECLINE_MESSAGE

    async def _converse_loop(self, query: str) -> str:
        """Drive the Bedrock Converse tool-use loop and return the final text."""
        client = self._clients.bedrock_runtime()
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": [{"text": query}]}
        ]
        request = self._base_request(messages)

        for _ in range(_MAX_TOOL_ROUNDS):
            response = client.converse(**request)
            output = _message_from_response(response)
            if output is None:
                return _DECLINE_MESSAGE
            messages.append(output)
            tool_uses = _tool_uses(output)
            if not tool_uses:
                return _text_from_message(output) or _DECLINE_MESSAGE
            # Resolve every requested tool call and feed the results back.
            tool_results = [await self._run_tool(tu) for tu in tool_uses]
            messages.append({"role": "user", "content": tool_results})
            request = self._base_request(messages)

        # Exhausted the tool budget without a final answer — make one more
        # tool-free pass so the model must answer from what it has.
        request = self._base_request(messages, allow_tools=False)
        response = client.converse(**request)
        output = _message_from_response(response)
        return (_text_from_message(output) if output else "") or _DECLINE_MESSAGE

    def _base_request(
        self, messages: list[dict[str, Any]], *, allow_tools: bool = True
    ) -> dict[str, Any]:
        """Build the Converse request kwargs for the current message list."""
        request: dict[str, Any] = {
            "modelId": self._config.bedrock_model_id,
            "messages": messages,
            "system": [{"text": _SYSTEM_PROMPT}],
            "inferenceConfig": {"maxTokens": self._config.max_response_tokens},
        }
        if allow_tools and self._tool_enabled:
            request["toolConfig"] = {"tools": [_WEB_SEARCH_TOOL]}
        return request

    async def _run_tool(self, tool_use: dict[str, Any]) -> dict[str, Any]:
        """Execute one requested tool call and build its Converse toolResult block."""
        tool_use_id = str(tool_use.get("toolUseId", ""))
        name = tool_use.get("name")
        args = tool_use.get("input") or {}
        if name == "web_search" and self._web_search is not None:
            query = str(args.get("query", "")).strip()
            category = str(args.get("category", "general")).strip() or "general"
            results = await self._web_search.search(query, category=category)
            body = results.to_tool_text()
        else:
            body = f"Tool '{name}' is not available."
        return {
            "toolResult": {
                "toolUseId": tool_use_id,
                "content": [{"text": body}],
            }
        }


def _message_from_response(response: Any) -> dict[str, Any] | None:
    """Extract the assistant message from a Converse response (or None)."""
    if not isinstance(response, dict):
        return None
    output = response.get("output")
    if not isinstance(output, dict):
        return None
    message = output.get("message")
    return message if isinstance(message, dict) else None


def _tool_uses(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every ``toolUse`` block in an assistant message."""
    content = message.get("content")
    if not isinstance(content, list):
        return []
    uses: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("toolUse"), dict):
            uses.append(block["toolUse"])
    return uses


def _text_from_message(message: dict[str, Any] | None) -> str:
    """Concatenate the text blocks of an assistant message."""
    if not message:
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    parts = [
        str(block["text"])
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return " ".join(p.strip() for p in parts if p.strip()).strip()
