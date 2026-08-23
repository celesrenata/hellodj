"""HelloDJ — ffmpeg QSV transcode subprocess manager.

Manages a ffmpeg process that decodes video using Intel QSV hardware acceleration,
scales to a target resolution via VPP, and encodes to H.264 Annex-B output piped
to stdout. Supports software decode fallback when QSV decode is unavailable for
the source codec.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from video import Resolution

log = logging.getLogger(__name__)

# Codecs that Intel QSV can hardware-decode
QSV_DECODABLE_CODECS: frozenset[str] = frozenset(
    {"h264", "hevc", "vp9", "mpeg2video", "mpeg2", "vc1"}
)

# Reference: 8 Mbps at 1080p (1920×1080 = 2_073_600 pixels)
_REFERENCE_BITRATE_BPS = 8_000_000
_REFERENCE_PIXELS = 1920 * 1080

# Timeout: abort if no output within this many seconds
_WATCHDOG_TIMEOUT_SECONDS = 60.0

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


def _bitrate_for_resolution(resolution: Resolution) -> int:
    """Compute target bitrate proportional to pixel count.

    Returns bitrate in bits/sec, scaled linearly from the 8 Mbps @ 1080p reference.
    """
    pixels = resolution.width * resolution.height
    bitrate = int(_REFERENCE_BITRATE_BPS * pixels / _REFERENCE_PIXELS)
    # Floor at 1 Mbps, cap at 20 Mbps
    return max(1_000_000, min(bitrate, 20_000_000))


class TranscodePipelineError(Exception):
    """Raised when the transcode pipeline encounters an unrecoverable error."""


class TranscodePipeline:
    """ffmpeg QSV transcode subprocess manager.

    Spawns an ffmpeg process that:
    - Decodes the source using QSV hardware acceleration (when codec is supported)
    - Scales to the target resolution via QSV VPP
    - Encodes to H.264 (Baseline/Main) via h264_qsv at constrained VBR
    - Outputs raw H.264 Annex-B NAL units to stdout (pipe:1)

    If QSV decode fails, automatically retries with software decode while keeping
    h264_qsv for encoding.
    """

    def __init__(self, source_codec: str = "h264", source_fps: float = 30.0) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.output_resolution: Resolution = Resolution.RES_1080P
        self.source_fps: float = source_fps
        self.source_codec: str = source_codec.lower()
        self._timeout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._input_path: str | None = None
        self._use_hwaccel_decode: bool = self.source_codec in QSV_DECODABLE_CODECS
        self._stderr_buffer: list[str] = []
        self._last_output_time: float = 0.0
        self._running: bool = False
        self._read_buffer: bytes = b""

    def build_ffmpeg_args(
        self,
        input_path: str,
        resolution: Resolution,
        seek_seconds: float = 0.0,
        *,
        hwaccel_decode: bool | None = None,
    ) -> list[str]:
        """Construct ffmpeg command line with QSV acceleration.

        Args:
            input_path: Path to the source video file.
            resolution: Target output resolution.
            seek_seconds: Seek to this timestamp before decoding.
            hwaccel_decode: Override whether to use QSV decode. If None, uses
                the instance's _use_hwaccel_decode flag (based on source codec).

        Returns:
            Complete ffmpeg argument list suitable for asyncio subprocess.
        """
        if hwaccel_decode is None:
            hwaccel_decode = self._use_hwaccel_decode

        bitrate = _bitrate_for_resolution(resolution)
        maxrate = int(bitrate * 1.5)

        args: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "error"]

        # Seek before input for fast seeking
        if seek_seconds > 0.0:
            args.extend(["-ss", f"{seek_seconds:.3f}"])

        # Decode stage
        if hwaccel_decode:
            args.extend([
                "-hwaccel", "qsv",
                "-hwaccel_output_format", "qsv",
            ])

        # Input
        args.extend(["-i", input_path])

        # Video filter: VPP scale to target resolution
        # Width computed to preserve aspect ratio (use -2 for even number)
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

        # Encode stage: h264_qsv with ICQ (maximum compression, maintain quality)
        # This pipeline outputs raw H.264 NAL units — no container overhead concerns.
        # ICQ global_quality 23 ≈ visually transparent; look-ahead for optimal bit allocation.
        args.extend([
            "-c:v", "h264_qsv",
            "-profile:v", "high",
            "-preset", "veryslow",
            "-global_quality", "23",
            "-look_ahead", "1",
            "-look_ahead_depth", "40",
            "-extbrc", "1",
            "-maxrate", str(maxrate),
            "-bufsize", str(bitrate * 2),
            "-g", "60",  # keyframe interval
        ])

        # Output: raw H.264 Annex-B to stdout, no audio
        args.extend([
            "-an",  # no audio in video pipe
            "-f", "h264",
            "pipe:1",
        ])

        return args

    async def start(self, input_path: str, resolution: Resolution) -> None:
        """Launch ffmpeg with QSV decode + encode pipeline.

        Args:
            input_path: Path to the source video file.
            resolution: Target output resolution.

        Raises:
            TranscodePipelineError: If the process fails to start.
        """
        self._input_path = input_path
        self.output_resolution = resolution
        self._running = True
        self._read_buffer = b""

        args = self.build_ffmpeg_args(input_path, resolution)
        log.info("Transcode starting: %s", " ".join(args))

        try:
            self.process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            self._running = False
            raise TranscodePipelineError(f"Failed to start ffmpeg: {exc}") from exc

        # Start stderr monitoring for QSV errors
        self._stderr_buffer = []
        self._stderr_task = asyncio.ensure_future(self._monitor_stderr())

        # Start watchdog
        self._last_output_time = asyncio.get_event_loop().time()
        self._timeout_task = asyncio.ensure_future(self._watchdog())

    async def read_nal_unit(self) -> bytes | None:
        """Read the next H.264 NAL unit from ffmpeg stdout.

        NAL units are delimited by start codes: 0x00000001 (4-byte) or
        0x000001 (3-byte). Returns the NAL unit bytes WITHOUT the start code
        prefix. Returns None on EOF or if the pipeline is not running.

        Returns:
            NAL unit bytes (without start code), or None on EOF/stop.
        """
        if not self._running or self.process is None:
            return None

        stdout = self.process.stdout
        if stdout is None:
            return None

        # Read data in chunks until we find a complete NAL unit
        while self._running:
            # Try to find a NAL unit in the buffer
            nal = self._extract_nal_from_buffer()
            if nal is not None:
                # Reset watchdog timer
                self._last_output_time = asyncio.get_event_loop().time()
                return nal

            # Read more data from stdout
            try:
                chunk = await stdout.read(65536)
            except (asyncio.CancelledError, ConnectionResetError):
                return None

            if not chunk:
                # EOF — flush remaining buffer as final NAL
                if self._read_buffer:
                    remaining = self._strip_start_code(self._read_buffer)
                    self._read_buffer = b""
                    if remaining:
                        self._last_output_time = asyncio.get_event_loop().time()
                        return remaining
                return None

            self._read_buffer += chunk

        return None

    def _extract_nal_from_buffer(self) -> bytes | None:
        """Try to extract a complete NAL unit from the read buffer.

        Looks for two consecutive start codes; the bytes between them form
        one NAL unit. Returns None if no complete NAL is available yet.
        """
        buf = self._read_buffer

        # Find the first start code
        first_pos = self._find_start_code(buf, 0)
        if first_pos == -1:
            # No start code at all — if buffer is large, discard leading junk
            if len(buf) > 4:
                # Keep last 3 bytes (might be partial start code)
                self._read_buffer = buf[-3:]
            return None

        # Find the second start code after the first
        # Skip past the first start code
        after_first = first_pos + (4 if buf[first_pos:first_pos + 4] == b"\x00\x00\x00\x01" else 3)
        second_pos = self._find_start_code(buf, after_first)

        if second_pos == -1:
            # Only one start code found — need more data
            # But trim anything before the first start code
            if first_pos > 0:
                self._read_buffer = buf[first_pos:]
            return None

        # Extract the NAL unit between the two start codes
        nal_data = buf[after_first:second_pos]
        # Update buffer to start from the second start code
        self._read_buffer = buf[second_pos:]
        return nal_data

    @staticmethod
    def _find_start_code(data: bytes, offset: int) -> int:
        """Find the position of the next H.264 start code in data.

        Searches for both 4-byte (0x00000001) and 3-byte (0x000001) start codes.
        Returns -1 if not found.
        """
        pos = offset
        end = len(data) - 2  # Need at least 3 bytes for 0x000001

        while pos <= end:
            # Look for 0x00 as first byte (fast skip)
            if data[pos] != 0x00:
                pos += 1
                continue

            # Check for 4-byte start code: 00 00 00 01
            if pos + 3 <= len(data) - 1 and data[pos:pos + 4] == b"\x00\x00\x00\x01":
                return pos

            # Check for 3-byte start code: 00 00 01
            if data[pos:pos + 3] == b"\x00\x00\x01":
                return pos

            pos += 1

        return -1

    @staticmethod
    def _strip_start_code(data: bytes) -> bytes:
        """Remove a leading start code from data if present."""
        if data[:4] == b"\x00\x00\x00\x01":
            return data[4:]
        if data[:3] == b"\x00\x00\x01":
            return data[3:]
        return data

    async def restart_at(self, timestamp_seconds: float, resolution: Resolution) -> None:
        """Restart pipeline at a given timestamp with new resolution.

        Kills the current ffmpeg process and respawns with -ss seek to the
        given timestamp. Used for mid-stream resolution changes.

        Args:
            timestamp_seconds: Position to seek to in the source.
            resolution: New target output resolution.
        """
        if self._input_path is None:
            raise TranscodePipelineError("Cannot restart: no input path set")

        log.info(
            "Restarting transcode at %.1fs with resolution %s",
            timestamp_seconds,
            resolution.name,
        )

        # Kill the current process
        await self._kill_process()

        # Update state
        self.output_resolution = resolution
        self._running = True
        self._read_buffer = b""

        # Build new args with seek
        args = self.build_ffmpeg_args(self._input_path, resolution, seek_seconds=timestamp_seconds)
        log.info("Transcode restarting: %s", " ".join(args))

        try:
            self.process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            self._running = False
            raise TranscodePipelineError(f"Failed to restart ffmpeg: {exc}") from exc

        # Restart monitoring
        self._stderr_buffer = []
        self._stderr_task = asyncio.ensure_future(self._monitor_stderr())
        self._last_output_time = asyncio.get_event_loop().time()
        self._timeout_task = asyncio.ensure_future(self._watchdog())

    async def stop(self) -> None:
        """Terminate ffmpeg subprocess and clean up resources.

        Uses SIGKILL for immediate termination, then waits for the process
        to exit. Always cleans up, even on exceptions.
        """
        self._running = False

        try:
            await self._kill_process()
        finally:
            # Cancel watchdog and stderr tasks
            if self._timeout_task is not None:
                self._timeout_task.cancel()
                try:
                    await self._timeout_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._timeout_task = None

            if self._stderr_task is not None:
                self._stderr_task.cancel()
                try:
                    await self._stderr_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._stderr_task = None

            self._read_buffer = b""

    async def _kill_process(self) -> None:
        """SIGKILL the ffmpeg process and wait for it to exit."""
        if self.process is None:
            return

        # Cancel existing monitoring tasks
        if self._timeout_task is not None:
            self._timeout_task.cancel()
            try:
                await self._timeout_task
            except (asyncio.CancelledError, Exception):
                pass
            self._timeout_task = None

        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stderr_task = None

        proc = self.process
        self.process = None

        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            # Process already dead
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass

    async def _watchdog(self) -> None:
        """Abort the pipeline if no output frames within 60 seconds.

        Runs as a background asyncio task. Checks periodically whether
        output has been received; if the timeout is exceeded, kills ffmpeg
        and logs an error.
        """
        try:
            while self._running:
                await asyncio.sleep(5.0)
                if not self._running:
                    break

                elapsed = asyncio.get_event_loop().time() - self._last_output_time
                if elapsed >= _WATCHDOG_TIMEOUT_SECONDS:
                    log.error(
                        "Transcode watchdog: no output for %.0fs, aborting pipeline",
                        elapsed,
                    )
                    self._running = False
                    await self._kill_process()
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
                        "QSV decode error detected, falling back to software decode: %s",
                        line,
                    )
                    # Trigger fallback
                    asyncio.ensure_future(self._fallback_software_decode())
                    break

                # Log non-trivial errors
                if any(kw in line.lower() for kw in ("error", "fatal", "failed")):
                    log.error("ffmpeg stderr: %s", line)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.debug("stderr monitor exception: %s", exc)

    def _is_qsv_error(self, line: str) -> bool:
        """Check if a stderr line indicates a QSV decode failure."""
        line_lower = line.lower()
        return any(pattern.lower() in line_lower for pattern in _QSV_ERROR_PATTERNS)

    async def _fallback_software_decode(self) -> None:
        """Restart the pipeline using software decode but keeping QSV encode.

        Called when QSV decode fails (unsupported codec, driver issue, etc.).
        """
        if self._input_path is None or not self._running:
            return

        log.warning(
            "Falling back to software decode + QSV encode for %s",
            self._input_path,
        )

        # Kill current process
        await self._kill_process()

        # Disable hardware decode for this pipeline instance
        self._use_hwaccel_decode = False
        self._running = True
        self._read_buffer = b""

        # Rebuild args with software decode
        args = self.build_ffmpeg_args(
            self._input_path,
            self.output_resolution,
            hwaccel_decode=False,
        )
        log.info("Transcode fallback starting: %s", " ".join(args))

        try:
            self.process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            self._running = False
            log.error("Failed to start fallback transcode: %s", exc)
            return

        # Restart monitoring (no more QSV decode fallback since we're already in sw mode)
        self._stderr_buffer = []
        self._stderr_task = asyncio.ensure_future(self._monitor_stderr())
        self._last_output_time = asyncio.get_event_loop().time()
        self._timeout_task = asyncio.ensure_future(self._watchdog())
