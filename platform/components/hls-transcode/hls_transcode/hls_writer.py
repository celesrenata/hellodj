"""HLS artifact model: local scratch layout and S3 key derivation.

HLS segments and playlists are produced by ffmpeg into a RAM-backed tmpfs
scratch directory on the transcode node, then uploaded to S3 (the CloudFront
origin) by :mod:`hls_transcode.s3_sink`. This module is the single source of
truth for how a guild stream maps to a local scratch layout and to S3 object
keys and viewer-facing CloudFront URLs.

Everything here is pure string/path derivation — no ffmpeg, no boto3, no
network — so it is testable in isolation and importable without any runtime
dependency installed. The S3 key shape mirrors the activity-backend's
``HlsCatalog`` so producer and consumer agree on object layout.

Requirements: 6.2, 18.2, 18.4
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["HlsArtifacts", "HlsWriter"]

# Standard artifact names produced by the encoder's HLS muxer.
_PLAYLIST_NAME = "index.m3u8"
_FMP4_INIT_NAME = "init.mp4"


def _sanitize(component: str, *, fallback: str) -> str:
    """Return a safe path/key component (alphanumerics, ``-`` and ``_``)."""
    cleaned = "".join(c for c in str(component) if c.isalnum() or c in "-_")
    return cleaned or fallback


@dataclass(frozen=True)
class HlsArtifacts:
    """Resolved local scratch and S3 locations for one HLS stream.

    Attributes:
        output_dir: Local (tmpfs) directory ffmpeg writes segments/playlist to.
        playlist_name: The media playlist file name within ``output_dir``.
        s3_bucket: Destination S3 bucket (CloudFront origin).
        s3_key_prefix: Destination key prefix for this stream's objects.
        playlist_key: S3 key of the playlist object.
        playlist_url: Viewer-facing CloudFront URL (empty when no CDN domain is
            configured).
    """

    output_dir: str
    playlist_name: str
    s3_bucket: str
    s3_key_prefix: str
    playlist_key: str
    playlist_url: str

    @property
    def local_playlist_path(self) -> str:
        """Absolute local path of the playlist within the scratch dir."""
        return f"{self.output_dir.rstrip('/')}/{self.playlist_name}"

    def s3_key_for(self, file_name: str) -> str:
        """Return the S3 key for a produced artifact file name."""
        return f"{self.s3_key_prefix}/{file_name.strip('/')}"


class HlsWriter:
    """Derives local scratch layout + S3 keys for guild HLS streams.

    A stream is identified by ``(guild_id, kind, stream_id)`` where ``kind`` is
    ``"video"`` or ``"visualizer"`` and ``stream_id`` is a per-session token, so
    a new session never collides with a stale one. The layout mirrors the
    activity-backend catalog: ``<prefix>/guild=<id>/<kind>/<stream_id>/...``.
    """

    def __init__(
        self,
        *,
        scratch_root: str,
        s3_bucket: str,
        s3_prefix: str,
        cloudfront_domain: str = "",
    ) -> None:
        """Initialise from the runtime storage settings.

        Args:
            scratch_root: Root tmpfs directory for per-stream scratch dirs.
            s3_bucket: Destination S3 bucket (CloudFront origin).
            s3_prefix: Key prefix within the bucket for HLS objects.
            cloudfront_domain: Optional CloudFront domain for viewer URLs.
        """
        self._scratch_root = scratch_root.rstrip("/")
        self._bucket = s3_bucket
        self._prefix = s3_prefix.strip("/")
        self._cloudfront_domain = cloudfront_domain.strip().rstrip("/")

    def _relative_path(self, guild_id: int, kind: str, stream_id: str) -> str:
        """Return the shared ``guild=<id>/<kind>/<stream_id>`` path fragment."""
        safe_kind = _sanitize(kind, fallback="media").lower()
        safe_stream = _sanitize(stream_id, fallback="stream")
        return f"guild={int(guild_id)}/{safe_kind}/{safe_stream}"

    def playlist_url_for_key(self, playlist_key: str) -> str:
        """Return the CloudFront URL for an S3 playlist key (empty if no CDN)."""
        if not self._cloudfront_domain:
            return ""
        return f"https://{self._cloudfront_domain}/{playlist_key.lstrip('/')}"

    def plan(self, guild_id: int, kind: str, stream_id: str) -> HlsArtifacts:
        """Resolve the full :class:`HlsArtifacts` for a stream.

        Args:
            guild_id: The guild the stream belongs to.
            kind: ``"video"`` or ``"visualizer"``.
            stream_id: Per-session identifier.

        Returns:
            A populated :class:`HlsArtifacts` with local + S3 + CDN locations.
        """
        relative = self._relative_path(guild_id, kind, stream_id)
        output_dir = f"{self._scratch_root}/{relative}"
        key_prefix = "/".join(p for p in (self._prefix, relative) if p)
        playlist_key = f"{key_prefix}/{_PLAYLIST_NAME}"
        return HlsArtifacts(
            output_dir=output_dir,
            playlist_name=_PLAYLIST_NAME,
            s3_bucket=self._bucket,
            s3_key_prefix=key_prefix,
            playlist_key=playlist_key,
            playlist_url=self.playlist_url_for_key(playlist_key),
        )

    @staticmethod
    def init_segment_name() -> str:
        """Return the fMP4 init-segment file name produced by the muxer."""
        return _FMP4_INIT_NAME
