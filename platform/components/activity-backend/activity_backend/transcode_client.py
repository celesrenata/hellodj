"""Typed client that emits transcode requests to the hls-transcode component.

The activity-backend does not transcode media. When a video or visualizer
session needs an HLS stream, it sends a typed HTTP/JSON request to the
``hls-transcode`` component, which performs the libx264/NVENC encode and writes
HLS to S3 (the CloudFront origin). This module defines the request/response
contract and a small client that delegates transport to an injected object, so
the client is fully unit-testable without aiohttp (R18.4, R15.1).

The concrete aiohttp transport lives in :mod:`activity_backend.server` and is
imported lazily, so this module imports cleanly without aiohttp installed.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Protocol

__all__ = [
    "TranscodeKind",
    "TranscodeRequest",
    "TranscodeResult",
    "TranscodeError",
    "Transport",
    "TranscodeClient",
]


class TranscodeKind(enum.Enum):
    """The kind of stream the transcode component should produce."""

    VIDEO = "video"
    VISUALIZER = "visualizer"


class TranscodeError(RuntimeError):
    """Raised when a transcode request fails at the transport or protocol layer."""


class Transport(Protocol):
    """Structural type for the JSON transport used by :class:`TranscodeClient`.

    An aiohttp-backed implementation is provided by the server; tests supply a
    fake exposing a compatible ``post_json``.
    """

    async def post_json(
        self, url: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """POST ``payload`` as JSON to ``url`` and return the decoded body."""
        ...


@dataclass(frozen=True)
class TranscodeRequest:
    """A request to start/refresh an HLS transcode for a guild stream.

    Attributes:
        guild_id: The guild the stream belongs to.
        kind: Whether this is a video or visualizer stream.
        stream_id: Per-session identifier (dedupes stale sessions).
        source_uri: Media source the transcoder should read (e.g. a resolved
            media URL or an intra-node loopback endpoint). Optional for
            visualizer streams driven purely by the audio feature bus.
        s3_bucket: Destination S3 bucket for HLS output (CloudFront origin).
        s3_key_prefix: Destination key prefix for HLS objects.
        engine: Visualizer engine name (visualizer streams only).
        options: Free-form encoder/visualizer options passed through.
    """

    guild_id: int
    kind: TranscodeKind
    stream_id: str
    s3_bucket: str
    s3_key_prefix: str
    source_uri: str | None = None
    engine: str | None = None
    options: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        """Serialize to the JSON body the hls-transcode component expects."""
        payload: dict[str, Any] = {
            "guildId": str(self.guild_id),
            "kind": self.kind.value,
            "streamId": self.stream_id,
            "s3Bucket": self.s3_bucket,
            "s3KeyPrefix": self.s3_key_prefix,
        }
        if self.source_uri is not None:
            payload["sourceUri"] = self.source_uri
        if self.engine is not None:
            payload["engine"] = self.engine
        if self.options:
            payload["options"] = dict(self.options)
        return payload


@dataclass(frozen=True)
class TranscodeResult:
    """The transcode component's response to a start/refresh request.

    Attributes:
        accepted: Whether the transcode job was accepted/started.
        playlist_key: S3 key of the produced/expected playlist, if known.
        message: Human-readable status detail.
    """

    accepted: bool
    playlist_key: str | None = None
    message: str = ""

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> TranscodeResult:
        """Build a result from a decoded JSON response body."""
        return cls(
            accepted=bool(body.get("accepted", body.get("ok", False))),
            playlist_key=body.get("playlistKey"),
            message=str(body.get("message", "")),
        )


class TranscodeClient:
    """Client for the hls-transcode component's start/stop endpoints."""

    def __init__(self, base_url: str, transport: Transport) -> None:
        """Initialise with the transcode base URL and an injected transport."""
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    async def request_transcode(
        self, request: TranscodeRequest
    ) -> TranscodeResult:
        """Ask the transcode component to start/refresh a stream (R18.4)."""
        url = f"{self._base_url}/v1/transcode"
        try:
            body = await self._transport.post_json(url, request.to_payload())
        except Exception as exc:  # normalize transport failures
            raise TranscodeError(
                f"transcode request failed for guild {request.guild_id}: {exc}"
            ) from exc
        return TranscodeResult.from_body(body)

    async def stop_transcode(self, guild_id: int, stream_id: str) -> None:
        """Ask the transcode component to stop a stream (best-effort)."""
        url = f"{self._base_url}/v1/transcode/stop"
        payload = {"guildId": str(guild_id), "streamId": stream_id}
        try:
            await self._transport.post_json(url, payload)
        except Exception as exc:
            raise TranscodeError(
                f"transcode stop failed for guild {guild_id}: {exc}"
            ) from exc
