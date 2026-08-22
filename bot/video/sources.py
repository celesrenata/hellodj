"""HelloDJ — Video source resolution: YouTube, URL download, and utilities.

Provides YouTubeResolver (yt-dlp wrapper) and URLDownloader (aiohttp-based)
for acquiring video files from various sources, plus shared utility functions
for file extension detection, size validation, and yt-dlp error classification.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import aiohttp

from video import FormatInfo, SourceQuality, VideoSource

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VIDEO_EXTENSIONS: frozenset[str] = frozenset({"mp4", "mkv", "webm", "avi", "mov", "m4v"})

_MAX_URL_DOWNLOAD_BYTES: int = 100 * 1024 * 1024  # 100 MB
_URL_CONNECT_TIMEOUT_SECONDS: float = 10.0
_YTDLP_DOWNLOAD_TIMEOUT_SECONDS: float = float(
    os.environ.get("YTDLP_DOWNLOAD_TIMEOUT", "600")
)  # 10 minutes default — long videos (50min+) can be 500MB+

# Default download directory for video files
_DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "hellodj_video"


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def is_video_extension(filename_or_url: str) -> bool:
    """Return True iff the file extension (case-insensitive, after last dot) is a supported video format.

    Supported extensions: mp4, mkv, webm, avi, mov, m4v.
    """
    # Strip query params if present (for URLs)
    path = urlparse(filename_or_url).path if "://" in filename_or_url else filename_or_url
    dot_pos = path.rfind(".")
    if dot_pos == -1:
        return False
    ext = path[dot_pos + 1:].lower()
    return ext in VIDEO_EXTENSIONS


def validate_file_size(size_bytes: int, max_bytes: int) -> tuple[bool, str]:
    """Validate that a file size does not exceed the maximum allowed.

    Returns:
        A tuple of (valid, message). If invalid, message contains both the
        actual and maximum sizes in a human-readable format.
    """
    if size_bytes <= max_bytes:
        return True, ""

    actual_mb = size_bytes / (1024 * 1024)
    max_mb = max_bytes / (1024 * 1024)
    return False, f"File size ({actual_mb:.1f}MB) exceeds maximum ({max_mb:.1f}MB)"


# ---------------------------------------------------------------------------
# yt-dlp error classification
# ---------------------------------------------------------------------------

# Error classification category type
YtdlpErrorCategory = Literal["unavailable", "age_restricted", "geo_restricted", "network", "unknown"]

# Patterns for classifying yt-dlp error output
_UNAVAILABLE_PATTERNS: tuple[str, ...] = (
    "video unavailable",
    "video is unavailable",
    "this video has been removed",
    "this video is no longer available",
    "this video is private",
    "video.*private",
    "video.*does not exist",
    "video.*been removed",
    "this video doesn't exist",
    "account.*terminated",
    "video has been deleted",
)

_AGE_RESTRICTED_PATTERNS: tuple[str, ...] = (
    "sign in to confirm your age",
    "age-restricted",
    "age restricted",
    "age_restricted",
    "confirm.*age",
    "age.gate",
    "age verification",
    "login required",
    "sign in.*verify",
)

_GEO_RESTRICTED_PATTERNS: tuple[str, ...] = (
    "not available in your country",
    "geo.restricted",
    "geo restricted",
    "geo_restricted",
    "blocked.*country",
    "blocked.*region",
    "available in your country",
    "content.*not available.*location",
    "uploader has not made this video available in your country",
)

_NETWORK_PATTERNS: tuple[str, ...] = (
    "unable to download",
    "connection.*refused",
    "connection.*reset",
    "connection.*timed out",
    "timed out",
    "timeout",
    "network.*unreachable",
    "name.*resolution.*failed",
    "dns",
    "getaddrinfo failed",
    "urlopen error",
    "connection error",
    "ssl.*error",
    "socket.*error",
)


def classify_ytdlp_error(error_output: str) -> YtdlpErrorCategory:
    """Classify a yt-dlp error output string into exactly one category.

    Categories:
        - "unavailable": video removed, private, or doesn't exist
        - "age_restricted": requires sign-in for age verification
        - "geo_restricted": blocked in the current region
        - "network": connection/DNS/timeout errors
        - "unknown": anything else

    The error output is matched case-insensitively against known patterns.
    """
    lower = error_output.lower()

    # Check age-restricted first (more specific than unavailable)
    for pattern in _AGE_RESTRICTED_PATTERNS:
        if re.search(pattern, lower):
            return "age_restricted"

    # Geo-restricted
    for pattern in _GEO_RESTRICTED_PATTERNS:
        if re.search(pattern, lower):
            return "geo_restricted"

    # Unavailable (broad patterns, check after more specific ones)
    for pattern in _UNAVAILABLE_PATTERNS:
        if re.search(pattern, lower):
            return "unavailable"

    # Network errors
    for pattern in _NETWORK_PATTERNS:
        if re.search(pattern, lower):
            return "network"

    return "unknown"


def error_category_message(category: YtdlpErrorCategory) -> str:
    """Return a user-facing message for a yt-dlp error category."""
    messages: dict[YtdlpErrorCategory, str] = {
        "unavailable": "Video is unavailable (removed, private, or does not exist)",
        "age_restricted": "Video is age-restricted and requires sign-in for verification",
        "geo_restricted": "Video is blocked in the current region",
        "network": "Network error while downloading video",
        "unknown": "Failed to download video (unknown error)",
    }
    return messages[category]


# ---------------------------------------------------------------------------
# YouTube quality selection helper
# ---------------------------------------------------------------------------


def select_quality(available_heights: list[int], requested: SourceQuality | None = None) -> int:
    """Select the best available video height based on the requested quality.

    Logic:
        - Default to 1080p when no quality is specified
        - Select the maximum available height <= the requested height
        - If no height <= requested exists, select the minimum available height
        - The result is always a member of the available heights set

    Args:
        available_heights: Non-empty list of available video heights.
        requested: Requested source quality, or None for default (1080p).

    Returns:
        The selected height from the available set.

    Raises:
        ValueError: If available_heights is empty.
    """
    if not available_heights:
        raise ValueError("available_heights must not be empty")

    target = requested.height if requested is not None else 1080

    # Find heights at or below the target
    at_or_below = [h for h in available_heights if h <= target]

    if at_or_below:
        return max(at_or_below)

    # Nothing at or below — select minimum available
    return min(available_heights)


# ---------------------------------------------------------------------------
# URL metadata extraction
# ---------------------------------------------------------------------------


def extract_url_metadata(url: str) -> tuple[str, str]:
    """Extract hostname and filename from a URL.

    Returns:
        A tuple of (hostname, filename) where:
        - hostname is the URL's domain without scheme or port
        - filename is the last path segment before any query parameters
    """
    parsed = urlparse(url)
    hostname = parsed.hostname or parsed.netloc.split(":")[0] if parsed.netloc else ""
    # Get last path segment
    path = parsed.path.rstrip("/")
    filename = path.rsplit("/", 1)[-1] if path else ""
    return hostname, filename


# ---------------------------------------------------------------------------
# YouTubeResolver
# ---------------------------------------------------------------------------


class YouTubeResolverError(Exception):
    """Raised when YouTube resolution fails."""

    def __init__(self, message: str, category: YtdlpErrorCategory = "unknown") -> None:
        super().__init__(message)
        self.category = category


class YouTubeResolver:
    """Resolve YouTube URLs/queries to streamable video sources via yt-dlp."""

    def __init__(self, download_dir: Path | None = None) -> None:
        self.download_dir = download_dir or _DOWNLOAD_DIR
        self.download_dir.mkdir(parents=True, exist_ok=True)

    async def resolve(
        self, query: str, quality: SourceQuality | None = None
    ) -> VideoSource:
        """Extract video stream URLs via yt-dlp (no download) and return a VideoSource.

        Uses yt-dlp in simulate mode to get the direct stream URLs for
        video and audio, which are then passed to ffmpeg for streaming
        transcode. No full file download is performed.

        Args:
            query: YouTube URL or search query.
            quality: Desired source quality. Defaults to best up to 720p.

        Returns:
            A VideoSource with stream_url set for streaming transcode.

        Raises:
            YouTubeResolverError: If yt-dlp fails.
        """
        # Cap at 720p for streaming — we're transcoding anyway and higher
        # resolutions waste bandwidth on the download side
        height = min(quality.height if quality is not None else 720, 720)
        format_sel = (
            f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
        )

        # Build yt-dlp command — simulate only, dump JSON with URLs
        args = [
            "yt-dlp",
            "--no-playlist",
            "-f", format_sel,
            "--dump-json",
            query,
        ]

        log.info("YouTubeResolver: extracting stream URLs for query=%r quality=%s", query, quality)

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=60.0,  # URL extraction is fast — 60s is generous
            )
        except asyncio.TimeoutError:
            try:
                process.kill()
                await process.wait()
            except (ProcessLookupError, OSError):
                pass
            raise YouTubeResolverError(
                "URL extraction timed out",
                category="network",
            )
        except (FileNotFoundError, OSError) as exc:
            raise YouTubeResolverError(
                f"Failed to run yt-dlp: {exc}",
                category="unknown",
            ) from exc

        if process.returncode != 0:
            error_text = stderr.decode(errors="replace") if stderr else ""
            category = classify_ytdlp_error(error_text)
            message = error_category_message(category)
            log.warning("yt-dlp failed (category=%s): %s", category, error_text[:200])
            raise YouTubeResolverError(message, category=category)

        # Parse JSON output
        import json

        json_output = stdout.decode(errors="replace").strip()
        json_lines = [line for line in json_output.splitlines() if line.strip().startswith("{")]
        if not json_lines:
            raise YouTubeResolverError(
                "yt-dlp produced no JSON output",
                category="unknown",
            )

        try:
            info = json.loads(json_lines[-1])
        except json.JSONDecodeError as exc:
            raise YouTubeResolverError(
                f"Failed to parse yt-dlp output: {exc}",
                category="unknown",
            ) from exc

        # Extract video and audio stream URLs from requested_formats
        video_url: str | None = None
        audio_url: str | None = None
        requested_formats = info.get("requested_formats", [])

        for fmt in requested_formats:
            has_video = fmt.get("vcodec", "none") != "none"
            has_audio = fmt.get("acodec", "none") != "none"
            url = fmt.get("url")

            if has_video and not video_url:
                video_url = url
            if has_audio and not has_video and not audio_url:
                audio_url = url
            # Combined format (has both video and audio)
            if has_video and has_audio and not video_url:
                video_url = url

        # Fallback: main URL (combined format)
        if not video_url:
            video_url = info.get("url")

        if not video_url:
            raise YouTubeResolverError(
                "yt-dlp returned no playable stream URL",
                category="unknown",
            )

        log.info(
            "YouTubeResolver: extracted stream URLs (video=%s, audio=%s) for '%s'",
            "yes" if video_url else "no",
            "yes" if audio_url else "no",
            info.get("title", "Unknown"),
        )

        return VideoSource(
            source_type="youtube",
            file_path="",  # No local file — streaming directly from URL
            title=info.get("title", "Unknown"),
            duration_seconds=float(info.get("duration", 0)),
            metadata={
                "uploader": info.get("uploader", "Unknown"),
                "channel": info.get("channel", info.get("uploader", "Unknown")),
                "video_id": info.get("id", ""),
                "webpage_url": info.get("webpage_url", ""),
                "height": info.get("height", 0),
                "width": info.get("width", 0),
            },
            audio_url=audio_url,
            stream_url=video_url,
            cleanup_on_finish=False,  # Nothing to clean up — no file downloaded
        )

    async def query_formats(self, url: str) -> list[FormatInfo]:
        """List available quality options for a YouTube video.

        Runs yt-dlp with format listing and parses the output into FormatInfo objects.

        Args:
            url: YouTube video URL.

        Returns:
            List of available video formats sorted by height descending.

        Raises:
            YouTubeResolverError: If yt-dlp fails to retrieve format info.
        """
        args = [
            "yt-dlp",
            "--no-playlist",
            "-j",  # dump single JSON (includes formats array)
            "--skip-download",
            url,
        ]

        log.info("YouTubeResolver: querying formats for %s", url)

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=_YTDLP_DOWNLOAD_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            try:
                process.kill()
                await process.wait()
            except (ProcessLookupError, OSError):
                pass
            raise YouTubeResolverError(
                "Format query timed out",
                category="network",
            )
        except (FileNotFoundError, OSError) as exc:
            raise YouTubeResolverError(
                f"Failed to run yt-dlp: {exc}",
                category="unknown",
            ) from exc

        if process.returncode != 0:
            error_text = stderr.decode(errors="replace") if stderr else ""
            category = classify_ytdlp_error(error_text)
            message = error_category_message(category)
            raise YouTubeResolverError(message, category=category)

        import json

        json_output = stdout.decode(errors="replace").strip()
        try:
            info = json.loads(json_output)
        except json.JSONDecodeError as exc:
            raise YouTubeResolverError(
                f"Failed to parse yt-dlp format output: {exc}",
                category="unknown",
            ) from exc

        formats: list[FormatInfo] = []
        seen_heights: set[int] = set()

        for fmt in info.get("formats", []):
            # Only video formats with a height
            height = fmt.get("height")
            vcodec = fmt.get("vcodec", "none")
            if height is None or height == 0 or vcodec == "none":
                continue

            # Deduplicate by height (keep best codec per height)
            if height in seen_heights:
                continue
            seen_heights.add(height)

            formats.append(FormatInfo(
                height=height,
                codec=vcodec,
                fps=float(fmt.get("fps", 0) or 0),
                filesize_approx=int(fmt.get("filesize_approx", 0) or fmt.get("filesize", 0) or 0),
                format_id=str(fmt.get("format_id", "")),
            ))

        # Sort by height descending
        formats.sort(key=lambda f: f.height, reverse=True)
        return formats


# ---------------------------------------------------------------------------
# URLDownloader
# ---------------------------------------------------------------------------


class URLDownloaderError(Exception):
    """Raised when URL download fails."""


class URLDownloader:
    """Download video from arbitrary URLs via aiohttp."""

    def __init__(
        self,
        download_dir: Path | None = None,
        max_bytes: int = _MAX_URL_DOWNLOAD_BYTES,
    ) -> None:
        self.download_dir = download_dir or _DOWNLOAD_DIR
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes

    async def download(self, url: str) -> VideoSource:
        """Validate, download, and return a VideoSource from a URL.

        Validation:
            - 10-second connection timeout
            - Content-Type must start with "video/"
            - HTTP 401/403 → "URL is not publicly accessible"
            - Max 100MB download limit

        Args:
            url: The video URL to download.

        Returns:
            A VideoSource with source_type="url".

        Raises:
            URLDownloaderError: If validation or download fails.
        """
        hostname, filename = extract_url_metadata(url)

        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=_URL_CONNECT_TIMEOUT_SECONDS,
            sock_connect=_URL_CONNECT_TIMEOUT_SECONDS,
        )

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    # Check HTTP status
                    if response.status in (401, 403):
                        raise URLDownloaderError("URL is not publicly accessible")

                    if response.status >= 400:
                        raise URLDownloaderError(
                            f"URL returned HTTP {response.status}"
                        )

                    # Check Content-Type
                    content_type = response.headers.get("Content-Type", "")
                    if not content_type.lower().startswith("video/"):
                        raise URLDownloaderError(
                            "URL does not contain video content "
                            f"(Content-Type: {content_type})"
                        )

                    # Check Content-Length if available
                    content_length = response.content_length
                    if content_length is not None:
                        valid, msg = validate_file_size(content_length, self.max_bytes)
                        if not valid:
                            raise URLDownloaderError(msg)

                    # Download with size limit
                    self.download_dir.mkdir(parents=True, exist_ok=True)
                    # Determine output filename
                    out_filename = filename if filename and is_video_extension(filename) else "video.mp4"
                    # Add a unique suffix to avoid collisions
                    import uuid
                    unique_name = f"{uuid.uuid4().hex[:8]}_{out_filename}"
                    output_path = self.download_dir / unique_name

                    downloaded = 0
                    with open(output_path, "wb") as f:
                        async for chunk in response.content.iter_chunked(65536):
                            downloaded += len(chunk)
                            if downloaded > self.max_bytes:
                                # Delete partial file
                                f.close()
                                output_path.unlink(missing_ok=True)
                                _, msg = validate_file_size(downloaded, self.max_bytes)
                                raise URLDownloaderError(msg)
                            f.write(chunk)

        except aiohttp.ClientError as exc:
            if "timeout" in str(exc).lower() or isinstance(exc, asyncio.TimeoutError):
                raise URLDownloaderError("URL unreachable: connection timed out") from exc
            raise URLDownloaderError(f"Failed to download: {exc}") from exc
        except asyncio.TimeoutError:
            raise URLDownloaderError("URL unreachable: connection timed out")

        log.info(
            "URLDownloader: downloaded %s (%d bytes) from %s",
            output_path.name,
            downloaded,
            hostname,
        )

        return VideoSource(
            source_type="url",
            file_path=str(output_path),
            title=filename or "Video",
            duration_seconds=0,  # Unknown until probed
            metadata={
                "hostname": hostname,
                "filename": filename,
                "url": url,
                "size_bytes": downloaded,
            },
            audio_url=None,
            cleanup_on_finish=True,
        )
