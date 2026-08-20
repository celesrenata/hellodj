"""HelloDJ — HLS transcode pipeline: ffmpeg QSV → HLS segment output.

Outputs HLS segments to disk instead of piping raw H.264 to stdout.
Based on the existing TranscodePipeline but adapted for Activity-based
video delivery with audio included and 720p resolution cap.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from video import Resolution
from video.transcode import QSV_DECODABLE_CODECS, _bitrate_for_resolution

log = logging.getLogger(__name__)

# Maximum output resolution for HLS Activity streams
_MAX_HLS_HEIGHT = 720

# HLS segment duration in seconds
_HLS_SEGMENT_DURATION = 4

# Watchdog timeout: kill if no new segments appear within this window
_WATCHDOG_TIMEOUT_SECONDS = 60.0

# Base directory for HLS output
_HLS_BASE_DIR = Path("/tmp/hellodj_hls")

# QSV error patterns in ffmpeg stderr that indicate decode failure
_QSV_ERROR_PATTERNS: tuple[str, ...] = (
    "Error initializing the MFX video decoder",
    "Error during decoding",
    "qsv",
    "MFXVideoDECODE",
    "mfx session",
    "hwaccel initialisation",
    "Failed to initialise VAAPI",
    "Decode_MFXAV1D",
)


class HLSTranscodePipelineError(Exception):
    """Raised when the HLS transcode pipeline encounters an unrecoverable error."""


class HLSTranscodePipeline:
    """ffmpeg QSV transcode → HLS segment output.

    Spawns an ffmpeg process that:
    - Decodes the source using QSV hardware acceleration (when codec is supported)
    - Scales to the target resolution via QSV VPP (capped at 720p)
    - Encodes video to H.264 via h264_qsv at constrained VBR
    - Encodes audio to AAC at 128 kbps
    - Outputs HLS segments (4s) and an m3u8 playlist to disk

    If QSV decode fails, automatically retries with software decode while
    keeping h264_qsv for encoding.
    """

    def __init__(
        self,
        guild_id: int,
        session_id: str,
        source_codec: str = "h264",
        source_fps: float = 30.0,
    ) -> None:
        self.guild_id = guild_id
        self.session_id = session_id
        self.source_codec: str = source_codec.lower()
        self.source_fps: float = source_fps

        # Output paths
        self.output_dir: Path = _HLS_BASE_DIR / str(guild_id) / session_id
        self.playlist_path: Path = self.output_dir / "playlist.m3u8"

        # Readiness signal: set when the first .ts segment appears on disk
        self.ready: asyncio.Event = asyncio.Event()

        # Internal state
        self.process: asyncio.subprocess.Process | None = None
        self._use_hwaccel_decode: bool = self.source_codec in QSV_DECODABLE_CODECS
        self._input_path: str | None = None
        self._running: bool = False
        self._timeout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._segment_watcher_task: asyncio.Task | None = None
        self._stderr_buffer: list[str] = []
        self._last_segment_time: float = 0.0
        self._complete_event: asyncio.Event = asyncio.Event()

    def _cap_resolution(self, resolution: Resolution) -> Resolution:
        """Cap the output resolution at 720p maximum."""
        if resolution.height > _MAX_HLS_HEIGHT:
            return Resolution.from_height(_MAX_HLS_HEIGHT)
        return resolution

    def build_ffmpeg_args(
        self,
        input_path: str,
        resolution: Resolution,
        *,
        hwaccel_decode: bool | None = None,
    ) -> list[str]:
        """Construct ffmpeg command line for HLS output with QSV acceleration.

        Args:
            input_path: Path to the source video file.
            resolution: Target output resolution (will be capped at 720p).
            hwaccel_decode: Override whether to use QSV decode. If None, uses
                the instance's _use_hwaccel_decode flag (based on source codec).

        Returns:
            Complete ffmpeg argument list suitable for asyncio subprocess.
        """
        if hwaccel_decode is None:
            hwaccel_decode = self._use_hwaccel_decode

        # Cap resolution at 720p
        resolution = self._cap_resolution(resolution)

        bitrate = _bitrate_for_resolution(resolution)
        maxrate = int(bitrate * 1.5)

        args: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]

        # Decode stage
        if hwaccel_decode:
            args.extend([
                "-hwaccel", "qsv",
                "-hwaccel_output_format", "qsv",
            ])

        # Input
        args.extend(["-i", input_path])

        # Video filter: VPP scale to target resolution
        if hwaccel_decode:
            # QSV VPP scale filter (hardware surfaces)
            args.extend([
                "-vf", f"scale_qsv=w=-1:h={resolution.height}",
            ])
        else:
            # Software decode → upload to QSV surface → scale
            args.extend([
                "-vf", f"hwupload=extra_hw_frames=64,scale_qsv=w=-1:h={resolution.height}",
                "-init_hw_device", "qsv=qsv:hw",
                "-filter_hw_device", "qsv",
            ])

        # Video encode: h264_qsv with constrained VBR
        args.extend([
            "-c:v", "h264_qsv",
            "-profile:v", "main",
            "-preset", "fast",
            "-b:v", str(bitrate),
            "-maxrate", str(maxrate),
            "-bufsize", str(bitrate * 2),
            "-g", "60",
        ])

        # Audio encode: AAC at 128 kbps
        args.extend([
            "-c:a", "aac",
            "-b:a", "128k",
        ])

        # HLS output format
        segment_pattern = str(self.output_dir / "seg%05d.ts")
        args.extend([
            "-f", "hls",
            "-hls_time", str(_HLS_SEGMENT_DURATION),
            "-hls_list_size", "0",
            "-hls_segment_filename", segment_pattern,
            str(self.playlist_path),
        ])

        return args

    async def start(self, input_path: str, resolution: Resolution) -> None:
        """Launch ffmpeg HLS transcode pipeline.

        Creates the output directory and starts ffmpeg. Launches background
        tasks for stderr monitoring, segment watching, and watchdog timeout.

        Args:
            input_path: Path to the source video file.
            resolution: Target output resolution (capped at 720p).

        Raises:
            HLSTranscodePipelineError: If the process fails to start.
        """
        self._input_path = input_path
        self._running = True
        self.ready.clear()
        self._complete_event.clear()

        # Ensure output directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)

        args = self.build_ffmpeg_args(input_path, resolution)
        log.info("HLS transcode starting: %s", " ".join(args))

        try:
            self.process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            self._running = False
            raise HLSTranscodePipelineError(f"Failed to start ffmpeg: {exc}") from exc

        # Start stderr monitoring for QSV errors
        self._stderr_buffer = []
        self._stderr_task = asyncio.ensure_future(self._monitor_stderr())

        # Start segment watcher (sets ready event on first .ts file)
        self._segment_watcher_task = asyncio.ensure_future(self._watch_segments())

        # Start watchdog
        self._last_segment_time = asyncio.get_event_loop().time()
        self._timeout_task = asyncio.ensure_future(self._watchdog())

    async def wait_ready(self, timeout: float = 30.0) -> bool:
        """Wait until the first HLS segment is written to disk.

        Args:
            timeout: Maximum seconds to wait for readiness.

        Returns:
            True if ready within timeout, False otherwise.
        """
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_complete(self) -> None:
        """Wait until the ffmpeg process finishes (transcode complete).

        Blocks until ffmpeg exits normally or the pipeline is stopped.
        """
        await self._complete_event.wait()

    async def stop(self) -> None:
        """Terminate ffmpeg subprocess and clean up background tasks.

        Uses SIGKILL for immediate termination, then waits for exit.
        """
        self._running = False

        try:
            await self._kill_process()
        finally:
            # Cancel all background tasks
            for task in (self._timeout_task, self._stderr_task, self._segment_watcher_task):
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

            self._timeout_task = None
            self._stderr_task = None
            self._segment_watcher_task = None

            # Signal completion so any waiters unblock
            self._complete_event.set()

    async def _kill_process(self) -> None:
        """SIGKILL the ffmpeg process and wait for it to exit."""
        if self.process is None:
            return

        proc = self.process
        self.process = None

        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass

    async def _watch_segments(self) -> None:
        """Monitor output directory for new .ts segment files.

        Sets the `ready` event when the first segment appears. Updates
        the last-segment timestamp for watchdog purposes.
        """
        try:
            first_seen = False
            while self._running:
                await asyncio.sleep(0.5)
                if not self._running:
                    break

                # Check for .ts files in the output directory
                try:
                    segments = list(self.output_dir.glob("*.ts"))
                except OSError:
                    continue

                if segments and not first_seen:
                    first_seen = True
                    self.ready.set()
                    self._last_segment_time = asyncio.get_event_loop().time()
                    log.info(
                        "HLS transcode ready: first segment written for guild=%s session=%s",
                        self.guild_id,
                        self.session_id,
                    )

                if segments:
                    # Update watchdog timestamp based on newest segment mtime
                    try:
                        newest_mtime = max(s.stat().st_mtime for s in segments)
                        current_time = asyncio.get_event_loop().time()
                        # Only update if a newer segment appeared
                        if newest_mtime > self._last_segment_time:
                            self._last_segment_time = current_time
                    except OSError:
                        pass

                # Check if ffmpeg has exited
                if self.process is not None and self.process.returncode is not None:
                    log.info(
                        "HLS transcode complete: ffmpeg exited with code %d for guild=%s session=%s",
                        self.process.returncode,
                        self.guild_id,
                        self.session_id,
                    )
                    self._running = False
                    self._complete_event.set()
                    break

        except asyncio.CancelledError:
            pass

    async def _watchdog(self) -> None:
        """Abort the pipeline if no new segments appear within 60 seconds.

        Runs as a background asyncio task. Checks periodically whether
        new segments have been written; if the timeout is exceeded, kills
        ffmpeg and logs an error.
        """
        try:
            while self._running:
                await asyncio.sleep(5.0)
                if not self._running:
                    break

                elapsed = asyncio.get_event_loop().time() - self._last_segment_time
                if elapsed >= _WATCHDOG_TIMEOUT_SECONDS:
                    log.error(
                        "HLS transcode watchdog: no new segments for %.0fs, aborting pipeline "
                        "(guild=%s session=%s)",
                        elapsed,
                        self.guild_id,
                        self.session_id,
                    )
                    self._running = False
                    await self._kill_process()
                    self._complete_event.set()
                    break
        except asyncio.CancelledError:
            pass

    async def _monitor_stderr(self) -> None:
        """Monitor ffmpeg stderr for QSV decode errors.

        If a QSV-related error is detected, triggers a fallback restart
        with software decode but QSV encode retained.
        """
        if self.process is None or self.process.stderr is None:
            return

        try:
            while self._running:
                line_bytes = await self.process.stderr.readline()
                if not line_bytes:
                    break

                line = line_bytes.decode(errors="replace").strip()
                if not line:
                    continue

                self._stderr_buffer.append(line)

                # Check for QSV decode errors
                if self._use_hwaccel_decode and self._is_qsv_error(line):
                    log.warning(
                        "HLS QSV decode error detected, falling back to software decode: %s",
                        line,
                    )
                    asyncio.ensure_future(self._fallback_software_decode())
                    break

                # Log non-trivial errors
                if any(kw in line.lower() for kw in ("error", "fatal", "failed")):
                    log.error("HLS ffmpeg stderr: %s", line)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.debug("HLS stderr monitor exception: %s", exc)

    def _is_qsv_error(self, line: str) -> bool:
        """Check if a stderr line indicates a QSV decode failure."""
        line_lower = line.lower()
        return any(pattern.lower() in line_lower for pattern in _QSV_ERROR_PATTERNS)

    async def _fallback_software_decode(self) -> None:
        """Restart the pipeline using software decode but keeping QSV encode.

        Called when QSV decode fails for the source codec.
        """
        if self._input_path is None or not self._running:
            return

        log.warning(
            "HLS falling back to software decode + QSV encode for %s",
            self._input_path,
        )

        # Kill current process
        await self._kill_process()

        # Cancel existing background tasks
        for task in (self._timeout_task, self._stderr_task, self._segment_watcher_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        # Disable hardware decode for this pipeline instance
        self._use_hwaccel_decode = False
        self._running = True

        # Rebuild args with software decode
        # Use RES_720P as the resolution (already capped)
        args = self.build_ffmpeg_args(
            self._input_path,
            Resolution.RES_720P,
            hwaccel_decode=False,
        )
        log.info("HLS transcode fallback starting: %s", " ".join(args))

        try:
            self.process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            self._running = False
            log.error("Failed to start HLS fallback transcode: %s", exc)
            self._complete_event.set()
            return

        # Restart monitoring
        self._stderr_buffer = []
        self._stderr_task = asyncio.ensure_future(self._monitor_stderr())
        self._segment_watcher_task = asyncio.ensure_future(self._watch_segments())
        self._last_segment_time = asyncio.get_event_loop().time()
        self._timeout_task = asyncio.ensure_future(self._watchdog())
