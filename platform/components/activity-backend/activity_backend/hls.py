"""HLS location model: S3 keys and CloudFront URLs (R18.2, R18.4).

The activity-backend does not transcode media itself and does not assume local
disk. HLS playlists and segments are written by the ``hls-transcode`` component
to an S3 bucket, and served to viewers through CloudFront (the managed edge
cache). This module is the single source of truth for how a guild's media maps
to S3 object keys and to viewer-facing CloudFront URLs.

Everything here is pure string derivation — no boto3, no network — so it is
testable in isolation and importable without any AWS dependency installed.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import ActivityConfig

__all__ = ["HlsLocation", "HlsCatalog"]

# Standard HLS artifact names produced by the transcode component.
_PLAYLIST_NAME = "index.m3u8"


def _sanitize_kind(kind: str) -> str:
    """Return a safe path component for the media ``kind`` (e.g. ``video``)."""
    cleaned = "".join(c for c in kind.strip().lower() if c.isalnum() or c in "-_")
    return cleaned or "media"


@dataclass(frozen=True)
class HlsLocation:
    """Resolved S3 and CloudFront locations for one HLS stream.

    Attributes:
        bucket: The S3 bucket holding the HLS objects.
        key_prefix: The S3 key prefix (folder) for this stream's objects.
        playlist_key: The S3 key of the ``index.m3u8`` playlist.
        playlist_url: The CloudFront URL viewers load (empty if no CDN domain
            is configured).
    """

    bucket: str
    key_prefix: str
    playlist_key: str
    playlist_url: str

    def segment_key(self, segment_name: str) -> str:
        """Return the S3 key for a named segment within this stream."""
        return f"{self.key_prefix}/{segment_name.strip('/')}"


class HlsCatalog:
    """Derives S3 keys and CloudFront URLs for guild media streams.

    A stream is identified by ``(guild_id, kind, stream_id)`` where ``kind`` is
    ``"video"`` or ``"visualizer"`` and ``stream_id`` is a per-session token
    (so a new video/visualizer session never collides with a stale one).
    """

    def __init__(self, config: ActivityConfig) -> None:
        """Initialise from the component config (bucket, CDN domain, prefix)."""
        self._bucket = config.hls_s3_bucket
        self._prefix = config.hls_s3_prefix.strip("/")
        self._cloudfront_domain = config.cloudfront_domain.strip().rstrip("/")

    @property
    def bucket(self) -> str:
        """The configured S3 bucket name (may be empty in local/dev)."""
        return self._bucket

    def key_prefix(self, guild_id: int, kind: str, stream_id: str) -> str:
        """Return the S3 key prefix for a stream.

        Shape: ``<prefix>/guild=<id>/<kind>/<stream_id>``. The ``guild=`` segment
        keeps objects grouped per guild for lifecycle rules and analytics.
        """
        safe_kind = _sanitize_kind(kind)
        safe_stream = "".join(
            c for c in str(stream_id) if c.isalnum() or c in "-_"
        )
        parts = [self._prefix, f"guild={int(guild_id)}", safe_kind, safe_stream]
        return "/".join(p for p in parts if p)

    def playlist_url_for_key(self, playlist_key: str) -> str:
        """Return the CloudFront URL for an S3 playlist key.

        Returns an empty string when no CloudFront domain is configured, so
        callers can detect an unconfigured edge layer rather than emit a broken
        URL.
        """
        if not self._cloudfront_domain:
            return ""
        return f"https://{self._cloudfront_domain}/{playlist_key.lstrip('/')}"

    def locate(
        self, guild_id: int, kind: str, stream_id: str
    ) -> HlsLocation:
        """Resolve the full :class:`HlsLocation` for a stream."""
        prefix = self.key_prefix(guild_id, kind, stream_id)
        playlist_key = f"{prefix}/{_PLAYLIST_NAME}"
        return HlsLocation(
            bucket=self._bucket,
            key_prefix=prefix,
            playlist_key=playlist_key,
            playlist_url=self.playlist_url_for_key(playlist_key),
        )
