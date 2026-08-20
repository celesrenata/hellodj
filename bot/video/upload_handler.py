"""HelloDJ — Discord attachment upload handler for video sources.

Validates, downloads, and probes Discord file attachments to produce
VideoSource objects compatible with the Activity streaming pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import uuid
from pathlib import Path

import discord

from video import VideoSource

log = logging.getLogger(__name__)

# Default download directory (shared with sources.py)
_DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "hellodj_video"


class UploadHandlerError(Exception):
    """Raised when upload processing fails."""


class UploadHandler:
    """Process Discord file attachments into playable VideoSource objects."""

    _MAX_UPLOAD_BYTES: int = 500 * 1024 * 1024  # 500 MB
    _FFPROBE_TIMEOUT: float = 10.0
    _SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({"mp4", "mkv", "webm", "avi", "mov", "m4v"})
    _VIDEO_MIME_PREFIXES: tuple[str, ...] = ("video/",)

    def __init__(self, download_dir: Path | None = None) -> None:
        self.download_dir = download_dir or _DOWNLOAD_DIR
        self.download_dir.mkdir(parents=True, exist_ok=True)

    async def process(self, attachment: discord.Attachment, uploader_name: str) -> VideoSource:
        """Download, validate, and produce a VideoSource from a Discord attachment.

        Validation pipeline:
            1. Check file size from attachment metadata (≤ 500 MB)
            2. Check content type / extension against supported formats
            3. Download the file
            4. Run ffprobe to confirm at least one video stream exists
            5. Extract duration from ffprobe output

        Args:
            attachment: The Discord message attachment object.
            uploader_name: The uploader's Discord display name.

        Returns:
            VideoSource with source_type="upload", cleanup_on_finish=True

        Raises:
            UploadHandlerError: On validation failure, download error, or ffprobe rejection.
        """
        # 1. Validate size (pre-download)
        size_error = self.validate_size(attachment)
        if size_error is not None:
            raise UploadHandlerError(size_error)

        # 2. Validate type (pre-download)
        type_error = self.validate_type(attachment)
        if type_error is not None:
            raise UploadHandlerError(type_error)

        # 3. Download attachment
        unique_name = f"{uuid.uuid4().hex[:8]}_{attachment.filename}"
        file_path = self.download_dir / unique_name

        try:
            await attachment.save(file_path)
        except Exception as exc:
            # Clean up partial file if it exists
            file_path.unlink(missing_ok=True)
            raise UploadHandlerError("Failed to download attachment") from exc

        # 4. ffprobe validation (post-download — clean up on failure)
        try:
            is_valid, duration = await self.ffprobe_validate(file_path)
        except UploadHandlerError:
            file_path.unlink(missing_ok=True)
            raise

        if not is_valid:
            file_path.unlink(missing_ok=True)
            raise UploadHandlerError("File is not a playable video")

        # 5. Produce VideoSource
        title = Path(attachment.filename).stem

        return VideoSource(
            source_type="upload",
            file_path=str(file_path),
            title=title,
            duration_seconds=duration,
            metadata={
                "uploader": uploader_name,
                "original_filename": attachment.filename,
                "size_bytes": attachment.size,
            },
            audio_url=None,
            cleanup_on_finish=True,
        )

    def validate_type(self, attachment: discord.Attachment) -> str | None:
        """Validate attachment type via content_type and filename extension.

        Returns None if valid, or an error message string if rejected.

        Logic:
            1. If content_type starts with "video/" → accept
            2. If content_type starts with "audio/" or "image/" → reject
            3. Fallback to extension check from filename
        """
        content_type = (attachment.content_type or "").lower()

        # Check MIME type first
        if content_type:
            if content_type.startswith("video/"):
                return None  # Valid
            if content_type.startswith("audio/") or content_type.startswith("image/"):
                return "Only video files are accepted"

        # Fallback: check file extension
        filename = attachment.filename or ""
        dot_pos = filename.rfind(".")
        if dot_pos != -1:
            ext = filename[dot_pos + 1:].lower()
            if ext in self._SUPPORTED_EXTENSIONS:
                return None  # Valid

        return "Unsupported format — accepted: mp4, mkv, webm, avi, mov, m4v"

    def validate_size(self, attachment: discord.Attachment) -> str | None:
        """Validate attachment file size from metadata.

        Returns None if valid, or an error message if too large or unknown size.
        """
        if attachment.size is None:
            return "Cannot validate file — size unknown"

        if attachment.size > self._MAX_UPLOAD_BYTES:
            size_mb = attachment.size / (1024 * 1024)
            return f"File too large ({size_mb:.1f} MB) — maximum is 500 MB"

        return None

    async def ffprobe_validate(self, file_path: Path) -> tuple[bool, float]:
        """Run ffprobe on the downloaded file to validate it contains a video stream.

        Args:
            file_path: Path to the downloaded file.

        Returns:
            Tuple of (is_valid, duration_seconds). duration_seconds is 0.0 if unknown.

        Raises:
            UploadHandlerError: On ffprobe timeout or process error.
        """
        args = [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            str(file_path),
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=self._FFPROBE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            # Kill the process on timeout
            try:
                process.kill()
                await process.wait()
            except (ProcessLookupError, OSError):
                pass
            raise UploadHandlerError("File validation timed out")
        except (FileNotFoundError, OSError) as exc:
            raise UploadHandlerError("File validation failed") from exc

        if process.returncode != 0:
            raise UploadHandlerError("File validation failed")

        # Parse JSON output
        try:
            data = json.loads(stdout.decode(errors="replace"))
        except (json.JSONDecodeError, ValueError):
            raise UploadHandlerError("File validation failed")

        # Check for at least one video stream
        streams = data.get("streams", [])
        has_video = any(
            stream.get("codec_type") == "video"
            for stream in streams
        )

        if not has_video:
            return False, 0.0

        # Extract duration from format (more reliable) or streams
        duration = 0.0
        fmt = data.get("format", {})
        duration_str = fmt.get("duration")
        if duration_str:
            try:
                duration = float(duration_str)
            except (ValueError, TypeError):
                pass

        # Fallback: try video stream duration
        if duration == 0.0:
            for stream in streams:
                if stream.get("codec_type") == "video":
                    stream_dur = stream.get("duration")
                    if stream_dur:
                        try:
                            duration = float(stream_dur)
                            break
                        except (ValueError, TypeError):
                            pass

        return True, duration
