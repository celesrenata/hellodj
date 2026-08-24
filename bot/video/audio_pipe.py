"""HelloDJ — Audio pipe session management for the Lavalink-to-FFmpeg FIFO bridge.

Creates and manages named FIFOs (mkfifo) that Lavalink writes filtered PCM to,
and FFmpeg reads as audio input during HLS transcoding.
"""

from __future__ import annotations

import glob
import logging
import os
import stat
import uuid
from pathlib import Path

from video.hls_transcode import _HLS_BASE_DIR

log = logging.getLogger(__name__)


class AudioPipeSession:
    """Manages a single FIFO pipe between Lavalink and FFmpeg.

    The pipe lives at /tmp/hellodj_hls/{guild_id}/{session_id}/audio.pipe
    and carries raw PCM audio (s16le, 48kHz, stereo) from Lavalink's filter
    chain into FFmpeg's muxing pipeline.

    Args:
        guild_id: The guild's Discord snowflake ID.
        session_id: Optional UUID for the session. Generated if not provided.
    """

    def __init__(self, guild_id: int, session_id: str | None = None) -> None:
        self.guild_id = guild_id
        self.session_id = session_id or str(uuid.uuid4())
        self._pipe_path = _HLS_BASE_DIR / str(guild_id) / self.session_id / "audio.pipe"
        self._active = False
        self._primer_fd: int | None = None  # Keeps FIFO "primed" so readers don't block

    @property
    def pipe_path(self) -> Path:
        """Absolute path to the named FIFO."""
        return self._pipe_path

    @property
    def ffmpeg_input_path(self) -> str:
        """Path string suitable for FFmpeg ``-i`` argument."""
        return str(self._pipe_path)

    @property
    def active(self) -> bool:
        """Whether the pipe has been created and not yet stopped."""
        return self._active

    async def start(self) -> bool:
        """Create the named FIFO and prime it for non-blocking reads.

        Ensures the parent directory exists, removes any stale pipe at the
        same path, creates a fresh FIFO, and opens it with O_RDWR to prevent
        FFmpeg from blocking on open(). The primer fd acts as both a reader
        and writer, satisfying the FIFO open semantics so that subsequent
        open() calls from FFmpeg (reader) and Lavalink (writer) don't block.

        Returns:
            True on success, False if the FIFO could not be created.
        """
        try:
            self._pipe_path.parent.mkdir(parents=True, exist_ok=True)

            # Remove stale pipe if it exists
            if self._pipe_path.exists():
                self._pipe_path.unlink()

            os.mkfifo(self._pipe_path)

            # Open O_RDWR | O_NONBLOCK to "prime" the FIFO. This never blocks
            # because O_RDWR on a FIFO satisfies both reader/writer requirements.
            # Lavalink can then open for writing without blocking.
            # IMPORTANT: must be closed after Lavalink connects, otherwise the
            # primer fd competes as a reader and absorbs data meant for the pipe
            # reader task.
            self._primer_fd = os.open(str(self._pipe_path), os.O_RDWR | os.O_NONBLOCK)

            self._active = True
            log.info("Audio pipe created: %s", self._pipe_path)
            return True
        except OSError as exc:
            log.error("Failed to create audio pipe at %s: %s", self._pipe_path, exc)
            self._active = False
            return False

    def close_primer(self) -> None:
        """Close the O_RDWR primer fd after a writer has connected.

        Must be called after Lavalink's enable_pipe succeeds, so the primer
        fd no longer competes as a reader on the FIFO. The actual reader
        (pipe reader task) will then receive all data from Lavalink's writer.
        """
        if self._primer_fd is not None:
            try:
                os.close(self._primer_fd)
            except OSError:
                pass
            self._primer_fd = None

    async def stop(self) -> None:
        """Remove the FIFO and clean up the session directory if empty."""
        self._active = False

        # Close primer fd first
        self.close_primer()

        try:
            if self._pipe_path.exists():
                self._pipe_path.unlink()
                log.info("Audio pipe removed: %s", self._pipe_path)

            # Clean up empty session directory
            session_dir = self._pipe_path.parent
            if session_dir.exists() and not any(session_dir.iterdir()):
                session_dir.rmdir()
        except OSError as exc:
            log.warning("Error cleaning up audio pipe %s: %s", self._pipe_path, exc)


def cleanup_orphaned_pipes() -> int:
    """Remove any orphaned audio.pipe files from previous sessions.

    Called at bot startup to ensure clean state. Only removes paths that
    are actual FIFOs (not regular files that happen to share the name).

    Returns:
        The number of pipes cleaned up.
    """
    count = 0
    pattern = str(_HLS_BASE_DIR / "*" / "*" / "audio.pipe")

    for pipe_path in glob.glob(pattern):
        try:
            path = Path(pipe_path)
            # Verify it's actually a FIFO (not a regular file)
            if stat.S_ISFIFO(path.stat().st_mode):
                path.unlink()
                count += 1
                log.info("Cleaned up orphaned audio pipe: %s", pipe_path)
        except OSError as exc:
            log.warning("Failed to clean up orphaned pipe %s: %s", pipe_path, exc)

    if count > 0:
        log.info("Cleaned up %d orphaned audio pipe(s)", count)
    return count
