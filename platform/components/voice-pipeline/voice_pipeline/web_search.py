"""Web search via the in-cluster ``mcp-searxng-enhanced`` server (MCP over HTTP).

The general-query responder gives the Bedrock model a single ``web_search`` tool.
When the model elects to use it, this client calls the ``mcp-searxng-enhanced``
component (FastMCP HTTP mode, ``POST <base>/mcp``) using the Model Context
Protocol JSON-RPC ``tools/call`` method to invoke the server's ``search_web``
tool, which fronts a SearXNG metasearch instance and scrapes result text via
Trafilatura.

Design constraints honoured here:

* **Transport-agnostic + testable.** The HTTP call is delegated to an injected
  :class:`SearchTransport` (an aiohttp adapter in production, a fake in tests),
  so this module imports no networking dependency and needs no live server.
* **Graceful degradation.** Any transport/protocol error, or an unconfigured
  endpoint, yields an empty result set — the responder then answers from the
  model alone rather than failing the voice interaction.
* **Brevity.** Only the top results' title/url/snippet are surfaced; the answer
  is read aloud, so we keep the tool payload compact.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

__all__ = [
    "SearchResult",
    "SearchResults",
    "SearchTransport",
    "WebSearchClient",
]


@dataclass(frozen=True)
class SearchResult:
    """A single web-search hit distilled to what a spoken answer needs.

    Attributes:
        title: The result title.
        url: The source URL (used for citation).
        snippet: A short content excerpt.
    """

    title: str
    url: str
    snippet: str

    def to_line(self) -> str:
        """Render a compact one-line summary for the model's tool result."""
        parts = [p for p in (self.title.strip(), self.snippet.strip()) if p]
        head = " — ".join(parts) if parts else self.url
        return f"{head} ({self.url})" if self.url else head


@dataclass(frozen=True)
class SearchResults:
    """The outcome of a web search.

    Attributes:
        query: The query that was searched.
        results: The (possibly empty) list of hits.
    """

    query: str
    results: list[SearchResult] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when no usable results were returned."""
        return not self.results

    def to_tool_text(self) -> str:
        """Render results as compact text to feed back to the model as a tool result."""
        if self.is_empty:
            return "No web results found."
        return "\n".join(f"- {r.to_line()}" for r in self.results)


class SearchTransport(Protocol):
    """Structural type for the async HTTP transport the client depends on.

    An aiohttp-backed adapter satisfies this in production; tests provide a fake
    that records calls and returns canned MCP responses.
    """

    async def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST ``payload`` as JSON to ``url`` and return the parsed response."""
        ...


class WebSearchClient:
    """Calls ``mcp-searxng-enhanced``'s ``search_web`` tool over MCP-HTTP.

    The client is a thin, typed façade: it builds the JSON-RPC ``tools/call``
    envelope, delegates the HTTP call to the injected :class:`SearchTransport`,
    and normalises the (text) tool result into :class:`SearchResults`. Every
    failure mode degrades to empty results rather than raising into the
    responder.
    """

    def __init__(
        self,
        base_url: str,
        transport: SearchTransport,
        *,
        max_results: int = 5,
    ) -> None:
        """Initialise the client.

        Args:
            base_url: Base URL of the mcp-searxng-enhanced server (the ``/mcp``
                endpoint is appended). A trailing slash is tolerated.
            transport: Injected async HTTP transport (mockable in tests).
            max_results: Max number of hits to surface from a search.
        """
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._max_results = max(1, max_results)
        self._request_id = 0

    @property
    def endpoint(self) -> str:
        """The MCP JSON-RPC endpoint this client posts to."""
        return f"{self._base_url}/mcp"

    async def search(self, query: str, *, category: str = "general") -> SearchResults:
        """Run a web search, returning normalised results (empty on any failure).

        Args:
            query: The search query.
            category: SearXNG category (``general``/``news``/``it``/…).

        Returns:
            A :class:`SearchResults`; empty when the query is blank, the
            endpoint is unreachable, or the response is unusable.
        """
        text = query.strip()
        if not text:
            return SearchResults(query=query)
        self._request_id += 1
        envelope = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {
                "name": "search_web",
                "arguments": {"query": text, "category": category},
            },
        }
        try:
            raw = await self._transport.post_json(self.endpoint, envelope)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            log.warning("web search transport failed: %s", exc)
            return SearchResults(query=query)
        return SearchResults(query=query, results=self._parse(raw))

    def _parse(self, raw: Any) -> list[SearchResult]:
        """Extract search hits from an MCP ``tools/call`` response envelope."""
        if not isinstance(raw, dict):
            return []
        result = raw.get("result")
        if not isinstance(result, dict):
            return []
        content = result.get("content")
        if not isinstance(content, list):
            return []
        hits: list[SearchResult] = []
        for block in content:
            hits.extend(self._parse_block(block))
            if len(hits) >= self._max_results:
                break
        return hits[: self._max_results]

    def _parse_block(self, block: Any) -> list[SearchResult]:
        """Parse a single MCP content block into zero or more results.

        The MCP text content of ``search_web`` is JSON (a list of result
        objects, or an object wrapping a ``results`` list). Non-JSON text is
        surfaced as a single snippet-only result so the model still gets signal.
        """
        if not isinstance(block, dict):
            return []
        text = block.get("text")
        if not isinstance(text, str) or not text.strip():
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return [SearchResult(title="", url="", snippet=text.strip())]
        return _results_from_payload(payload)


def _results_from_payload(payload: Any) -> list[SearchResult]:
    """Coerce a parsed JSON payload into a list of :class:`SearchResult`."""
    if isinstance(payload, dict):
        payload = payload.get("results", payload.get("data", []))
    if not isinstance(payload, list):
        return []
    out: list[SearchResult] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        out.append(
            SearchResult(
                title=str(item.get("title", "")).strip(),
                url=str(item.get("url", item.get("link", ""))).strip(),
                snippet=str(
                    item.get("content", item.get("snippet", item.get("description", "")))
                ).strip(),
            )
        )
    return out
