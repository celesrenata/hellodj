"""Typed HTTP/JSON client that dispatches recognized actions to the orchestrator.

Once the pipeline has turned speech into a structured intent, the resulting
action is dispatched to the ``playback-orchestrator`` — the single owner of
routing, classification, filtering, bans, and session/queue persistence. This
component contains no playback logic; it only forwards typed action requests.

The transport is abstracted behind :class:`Transport` so the client is testable
without a live orchestrator or a specific HTTP library. Nothing here imports a
networking dependency, keeping the module import-clean and unit-testable. This
mirrors the ``discord-bot-core`` playback client contract so both components
speak the same orchestrator protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "ActionRequest",
    "ActionResult",
    "ActionDispatchError",
    "Transport",
    "OrchestratorActionClient",
]


@dataclass(frozen=True)
class ActionRequest:
    """A typed action derived from a recognized voice intent.

    Attributes:
        category: Intent category ("music"/"admin"/"general").
        guild_id: Discord guild the request originates from.
        channel_id: Voice/text channel context for the request.
        requested_by: Discord user id whose speech triggered the action.
        subcommand: Specific action (e.g. "play", "skip") when applicable.
        query: The transcript / free-text query for the action.
        args: Parsed arguments (e.g. ``{"song": "..."}``).
    """

    category: str
    guild_id: int
    channel_id: int
    requested_by: int
    subcommand: str | None = None
    query: str | None = None
    args: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """Serialize to the JSON body sent to the orchestrator."""
        payload: dict[str, Any] = {
            "source": "voice",
            "category": self.category,
            "guildId": str(self.guild_id),
            "channelId": str(self.channel_id),
            "requestedBy": str(self.requested_by),
            "args": dict(self.args),
        }
        if self.subcommand is not None:
            payload["subcommand"] = self.subcommand
        if self.query is not None:
            payload["query"] = self.query
        return payload


@dataclass(frozen=True)
class ActionResult:
    """Outcome of an action request as reported by the orchestrator.

    Attributes:
        ok: Whether the orchestrator accepted and acted on the request.
        message: Human-readable status suitable for a spoken TTS reply.
        data: Additional structured payload (e.g. queue snapshot).
    """

    ok: bool
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ActionResult:
        """Build a result from the orchestrator's JSON response body."""
        return cls(
            ok=bool(payload.get("ok", False)),
            message=str(payload.get("message", "")),
            data=dict(payload.get("data", {})),
        )


class ActionDispatchError(RuntimeError):
    """Raised when the orchestrator cannot be reached or returns an error."""


class Transport(Protocol):
    """Structural type for the async HTTP transport the client depends on.

    An aiohttp-backed adapter satisfies this in production; tests provide a fake
    that records calls and returns canned payloads.
    """

    async def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST ``payload`` as JSON to ``url`` and return the parsed response."""
        ...


class OrchestratorActionClient:
    """Dispatches recognized voice actions to the playback-orchestrator.

    The client is a thin, typed façade: it builds the request URL, delegates the
    HTTP call to the injected :class:`Transport`, and maps the response into an
    :class:`ActionResult`.
    """

    def __init__(self, base_url: str, transport: Transport) -> None:
        """Initialise the client.

        Args:
            base_url: Base URL of the orchestrator (no trailing slash required).
            transport: Injected async HTTP transport (mockable in tests).
        """
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    @property
    def base_url(self) -> str:
        """The orchestrator base URL this client targets."""
        return self._base_url

    async def dispatch(self, request: ActionRequest) -> ActionResult:
        """Dispatch an action request to the orchestrator.

        Args:
            request: The typed action to forward.

        Returns:
            The orchestrator's :class:`ActionResult`.

        Raises:
            ActionDispatchError: If the transport fails or the response is
                unusable.
        """
        url = f"{self._base_url}/v1/voice/action"
        try:
            raw = await self._transport.post_json(url, request.to_payload())
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed error
            raise ActionDispatchError(
                f"failed to reach playback-orchestrator at {url}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ActionDispatchError(
                "playback-orchestrator returned a non-object response"
            )
        return ActionResult.from_payload(raw)
