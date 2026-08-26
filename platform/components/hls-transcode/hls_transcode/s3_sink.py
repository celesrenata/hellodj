"""S3 sink for produced HLS segments and playlists (CloudFront origin).

The transcode node writes HLS artifacts to a local tmpfs scratch directory, and
this module uploads them to Amazon S3, which is the CloudFront origin viewers
read from (R18.2, R18.4). The S3 client is *injected* so the sink is fully
unit-testable with a fake, and the real boto3 client is created lazily so this
module imports cleanly without boto3 installed (R15.1).

Correct content types matter for HLS playback: ``.m3u8`` playlists,
``.m4s``/``init.mp4`` fMP4 segments, and ``.ts`` MPEG-TS segments each get the
right MIME type so hls.js and CloudFront serve them correctly.

Requirements: 6.2, 15.1, 18.2, 18.4
"""

from __future__ import annotations

from typing import Any, Protocol

__all__ = ["S3Client", "S3Sink", "content_type_for", "create_s3_client"]

# MIME types for the artifact kinds the HLS muxer produces.
_CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".ts": "video/mp2t",
}
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


def content_type_for(file_name: str) -> str:
    """Return the HTTP content type for an HLS artifact file name.

    Args:
        file_name: The artifact file name (e.g. ``index.m3u8``, ``seg_0.m4s``).

    Returns:
        The matching MIME type, or ``application/octet-stream`` if unknown.
    """
    lower = file_name.lower()
    for suffix, content_type in _CONTENT_TYPES.items():
        if lower.endswith(suffix):
            return content_type
    return _DEFAULT_CONTENT_TYPE


class S3Client(Protocol):
    """Structural type for the subset of the boto3 S3 client used here."""

    def put_object(self, **kwargs: Any) -> Any:
        """Upload a single object to S3."""
        ...


def create_s3_client(region_name: str | None = None) -> S3Client:
    """Create a real boto3 S3 client (imported lazily).

    Args:
        region_name: Optional AWS region; ``None`` uses the boto3 default chain.

    Returns:
        A boto3 S3 client implementing the :class:`S3Client` protocol.
    """
    import boto3  # local import so the module imports without boto3 present

    return boto3.client("s3", region_name=region_name)


class S3Sink:
    """Uploads produced HLS artifacts to S3 with correct content types.

    The client is injected (real one built via :func:`create_s3_client`); the
    sink itself carries no AWS dependency at import time and is exercised in
    tests with a fake client that records ``put_object`` calls.
    """

    def __init__(self, bucket: str, client: S3Client) -> None:
        """Initialise with the destination bucket and an injected S3 client."""
        self._bucket = bucket
        self._client = client

    @property
    def bucket(self) -> str:
        """The destination S3 bucket name."""
        return self._bucket

    def put_bytes(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str | None = None,
        cache_control: str | None = None,
    ) -> None:
        """Upload raw bytes to ``key`` in the sink's bucket.

        Args:
            key: Destination S3 object key.
            body: Object payload.
            content_type: Optional explicit content type; inferred from the key
                suffix when omitted.
            cache_control: Optional ``Cache-Control`` header value. Playlists
                should be short-lived; segments can be cached longer.
        """
        params: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": body,
            "ContentType": content_type or content_type_for(key),
        }
        if cache_control is not None:
            params["CacheControl"] = cache_control
        self._client.put_object(**params)

    def put_file(
        self,
        key: str,
        local_path: str,
        *,
        cache_control: str | None = None,
    ) -> None:
        """Read ``local_path`` from the scratch dir and upload it to ``key``.

        Args:
            key: Destination S3 object key.
            local_path: Absolute local path of the produced artifact.
            cache_control: Optional ``Cache-Control`` header value.
        """
        with open(local_path, "rb") as handle:
            body = handle.read()
        self.put_bytes(
            key,
            body,
            content_type=content_type_for(local_path),
            cache_control=cache_control,
        )
