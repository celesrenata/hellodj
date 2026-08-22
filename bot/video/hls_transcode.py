"""HelloDJ — HLS transcode pipeline: ffmpeg QSV → HLS segment output.

Outputs HLS segments to disk instead of piping raw H.264 to stdout.
Based on the existing TranscodePipeline but adapted for Activity-based
video delivery with audio included and 720p resolution cap.
"""

from __future__ import annotations

import asyncio
import json
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

        # Subtitle tracks discovered via ffprobe
        self.subtitle_tracks: list[dict] = []

        # Audio tracks discovered via ffprobe
        self.audio_tracks: list[dict] = []

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

        When multiple audio tracks are detected (len(self.audio_tracks) > 1),
        produces a multi-variant HLS output with:
        - A master playlist (playlist.m3u8) with #EXT-X-MEDIA audio group entries
        - A video-only variant: video.m3u8 + seg%05d_video.ts
        - Per-language audio variants: audio_{lang}.m3u8 + seg%05d_audio_{lang}.ts

        When only one (or zero) audio tracks exist, produces a single muxed
        A/V output as before (playlist.m3u8 + seg%05d.ts).

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

        multi_audio = len(self.audio_tracks) > 1

        args: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]

        # Decode stage
        if hwaccel_decode:
            args.extend([
                "-hwaccel", "qsv",
                "-hwaccel_output_format", "qsv",
            ])

        # Input
        args.extend(["-i", input_path])

        # Stream mapping for multi-audio: explicitly map video + each audio
        if multi_audio:
            args.extend(["-map", "0:v:0"])
            for idx in range(len(self.audio_tracks)):
                args.extend(["-map", f"0:a:{idx}"])

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
        # force_key_frames ensures keyframes align with HLS segment boundaries
        args.extend([
            "-c:v", "h264_qsv",
            "-profile:v", "main",
            "-preset", "fast",
            "-b:v", str(bitrate),
            "-maxrate", str(maxrate),
            "-bufsize", str(bitrate * 2),
            "-g", "96",
            "-force_key_frames", "expr:gte(t,n_forced*4)",
        ])

        # Audio encode: AAC at 128 kbps
        args.extend([
            "-c:a", "aac",
            "-b:a", "128k",
        ])

        # HLS output format
        # -max_interleave_delta 0: force muxer to wait for all streams before
        # writing, preventing audio starvation when QSV video runs ahead
        args.extend([
            "-max_interleave_delta", "0",
            "-f", "hls",
            "-hls_time", str(_HLS_SEGMENT_DURATION),
            "-hls_list_size", "0",
            "-hls_playlist_type", "event",
            "-hls_flags", "independent_segments",
        ])

        if multi_audio:
            # Build -var_stream_map: video variant + one audio variant per track
            # Each variant gets a name used as the %v substitution in output paths
            var_parts: list[str] = ["v:0,name:video,agroup:audio"]
            for idx, track in enumerate(self.audio_tracks):
                lang = track.get("lang", f"aud{idx}")
                var_parts.append(
                    f"a:{idx},name:audio_{lang},agroup:audio,language:{lang}"
                )
            var_stream_map = " ".join(var_parts)

            segment_pattern = str(self.output_dir / "seg%05d_%v.ts")
            args.extend([
                "-var_stream_map", var_stream_map,
                "-master_pl_name", "playlist.m3u8",
                "-hls_segment_filename", segment_pattern,
                str(self.output_dir / "%v.m3u8"),
            ])
        else:
            # Single muxed A/V output (original behavior)
            segment_pattern = str(self.output_dir / "seg%05d.ts")
            args.extend([
                "-hls_segment_filename", segment_pattern,
                str(self.playlist_path),
            ])

        return args

    @staticmethod
    async def probe_subtitles(input_path: str) -> list[dict]:
        """Probe the source file for embedded subtitle tracks.

        Runs ffprobe to detect subtitle streams and extracts language,
        label, and stream index metadata for each.

        Args:
            input_path: Path to the source video file.

        Returns:
            List of subtitle track dicts with keys: lang, label, stream_index.
            Returns an empty list if no subtitles are found or ffprobe fails.
        """
        args = [
            "ffprobe",
            "-hide_banner",
            "-loglevel", "error",
            "-select_streams", "s",
            "-show_streams",
            "-print_format", "json",
            input_path,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except (FileNotFoundError, OSError) as exc:
            log.warning("ffprobe not available for subtitle probe: %s", exc)
            return []
        except asyncio.TimeoutError:
            log.warning("ffprobe subtitle probe timed out for %s", input_path)
            return []

        if proc.returncode != 0:
            log.debug(
                "ffprobe subtitle probe returned non-zero for %s: %s",
                input_path,
                stderr.decode(errors="replace").strip(),
            )
            return []

        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            log.warning("ffprobe subtitle probe returned invalid JSON for %s", input_path)
            return []

        streams = data.get("streams", [])
        tracks: list[dict] = []
        subtitle_idx = 0

        for stream in streams:
            if stream.get("codec_type") != "subtitle":
                continue

            tags = stream.get("tags", {})
            # Language: try tags.language, fall back to "und" or indexed name
            lang = tags.get("language", "").strip().lower()
            if not lang or lang == "und":
                lang = f"sub{subtitle_idx}"

            # Label: try tags.title, fall back to language code
            label = tags.get("title", "").strip()
            if not label:
                label = lang.upper() if len(lang) <= 3 else lang.capitalize()

            # Stream index from ffprobe output
            stream_index = stream.get("index", subtitle_idx)

            tracks.append({
                "lang": lang,
                "label": label,
                "stream_index": stream_index,
            })
            subtitle_idx += 1

        return tracks

    @staticmethod
    async def probe_audio_tracks(input_path: str) -> list[dict]:
        """Probe the source file for embedded audio tracks.

        Runs ffprobe to detect audio streams and extracts language,
        label, and stream index metadata for each.

        Args:
            input_path: Path to the source video file.

        Returns:
            List of audio track dicts with keys: lang, label, stream_index.
            Returns an empty list if no audio tracks are found or ffprobe fails.
        """
        args = [
            "ffprobe",
            "-hide_banner",
            "-loglevel", "error",
            "-select_streams", "a",
            "-show_streams",
            "-print_format", "json",
            input_path,
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        except (FileNotFoundError, OSError) as exc:
            log.warning("ffprobe not available for audio probe: %s", exc)
            return []
        except asyncio.TimeoutError:
            log.warning("ffprobe audio probe timed out for %s", input_path)
            return []

        if proc.returncode != 0:
            log.debug(
                "ffprobe audio probe returned non-zero for %s: %s",
                input_path,
                stderr.decode(errors="replace").strip(),
            )
            return []

        try:
            data = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            log.warning("ffprobe audio probe returned invalid JSON for %s", input_path)
            return []

        streams = data.get("streams", [])
        tracks: list[dict] = []
        audio_idx = 0

        for stream in streams:
            if stream.get("codec_type") != "audio":
                continue

            tags = stream.get("tags", {})
            # Language: try tags.language, fall back to indexed name
            lang = tags.get("language", "").strip().lower()
            if not lang or lang == "und":
                lang = f"aud{audio_idx}"

            # Label: try tags.title, fall back to language code
            label = tags.get("title", "").strip()
            if not label:
                label = lang.upper() if len(lang) <= 3 else lang.capitalize()

            # Stream index from ffprobe output
            stream_index = stream.get("index", audio_idx)

            tracks.append({
                "lang": lang,
                "label": label,
                "stream_index": stream_index,
            })
            audio_idx += 1

        return tracks

    async def extract_subtitles(
        self, input_path: str, subtitle_tracks: list[dict]
    ) -> None:
        """Extract subtitle tracks from the source file as WebVTT sidecar files.

        For each discovered subtitle track, runs ffmpeg to convert it to WebVTT
        format and writes it to `output_dir/subtitles/{lang}.vtt`.

        Args:
            input_path: Path to the source video file.
            subtitle_tracks: List of subtitle track dicts from probe_subtitles().
        """
        if not subtitle_tracks:
            return

        subtitles_dir = self.output_dir / "subtitles"
        subtitles_dir.mkdir(parents=True, exist_ok=True)

        for idx, track in enumerate(subtitle_tracks):
            lang = track["lang"]
            output_path = subtitles_dir / f"{lang}.vtt"

            args = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-y",
                "-i", input_path,
                "-map", f"0:s:{idx}",
                "-f", "webvtt",
                str(output_path),
            ]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=60.0)
            except (FileNotFoundError, OSError) as exc:
                log.warning(
                    "Failed to extract subtitle track %s (%s): %s",
                    lang, track.get("label", ""), exc,
                )
                continue
            except asyncio.TimeoutError:
                log.warning(
                    "Subtitle extraction timed out for track %s (%s)",
                    lang, track.get("label", ""),
                )
                continue

            if proc.returncode != 0:
                log.warning(
                    "ffmpeg subtitle extraction failed for track %s (%s): %s",
                    lang,
                    track.get("label", ""),
                    stderr.decode(errors="replace").strip(),
                )
            else:
                log.info("Extracted subtitle track: %s → %s", lang, output_path)

    async def start_streaming(self, source_url: str, resolution: Resolution) -> None:
        """Launch ffmpeg HLS transcode pipeline reading directly from a URL.

        Unlike start() which reads from a local file, this reads from an HTTP/
        HTTPS URL (e.g., an HLS manifest or direct video URL) and transcodes
        on-the-fly. Uses -re to throttle input to native playback rate, reducing
        bandwidth pressure on media providers.

        Args:
            source_url: HTTP(S) URL to the source video/HLS manifest.
            resolution: Target output resolution (capped at 720p).

        Raises:
            HLSTranscodePipelineError: If the process fails to start.
        """
        self._input_path = source_url
        self._running = True
        self.ready.clear()
        self._complete_event.clear()

        # Ensure output directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # No subtitle/audio probing for streaming URLs (not seekable)
        self.subtitle_tracks = []
        self.audio_tracks = [{"lang": "und", "label": ""}]

        args = self._build_streaming_ffmpeg_args(source_url, resolution)
        log.info("HLS streaming transcode starting: %s", " ".join(args))

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

        # Start watchdog (longer timeout for streaming — source may buffer)
        self._last_segment_time = asyncio.get_event_loop().time()
        self._timeout_task = asyncio.ensure_future(self._watchdog())

    def _build_streaming_ffmpeg_args(
        self, source_url: str, resolution: Resolution
    ) -> list[str]:
        """Build ffmpeg args for streaming URL input → HLS output.

        Uses -re to read at native rate (throttled) and -reconnect flags
        for resilient HTTP streaming.
        """
        resolution = self._cap_resolution(resolution)
        bitrate = _bitrate_for_resolution(resolution)
        maxrate = int(bitrate * 1.5)

        args: list[str] = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]

        # Throttle input to native playback rate — friendly to providers
        args.append("-re")

        # HTTP reconnect options for resilient streaming
        args.extend([
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
        ])

        # Decode stage: prefer QSV hardware decode
        if self._use_hwaccel_decode:
            args.extend([
                "-hwaccel", "qsv",
                "-hwaccel_output_format", "qsv",
            ])

        # Input: URL directly
        args.extend(["-i", source_url])

        # Video filter: VPP scale to target resolution
        if self._use_hwaccel_decode:
            args.extend(["-vf", f"scale_qsv=w=-1:h={resolution.height}"])
        else:
            args.extend([
                "-vf", f"hwupload=extra_hw_frames=64,scale_qsv=w=-1:h={resolution.height}",
                "-init_hw_device", "qsv=qsv:hw",
                "-filter_hw_device", "qsv",
            ])

        # Video encode: h264_qsv
        args.extend([
            "-c:v", "h264_qsv",
            "-profile:v", "main",
            "-preset", "fast",
            "-b:v", str(bitrate),
            "-maxrate", str(maxrate),
            "-bufsize", str(bitrate * 2),
            "-g", "96",
            "-force_key_frames", "expr:gte(t,n_forced*4)",
        ])

        # Audio encode: AAC at 128 kbps
        args.extend(["-c:a", "aac", "-b:a", "128k"])

        # HLS output (event type — segments accumulate for VOD-like seeking)
        segment_pattern = str(self.output_dir / "seg%05d.ts")
        args.extend([
            "-max_interleave_delta", "0",
            "-f", "hls",
            "-hls_time", str(_HLS_SEGMENT_DURATION),
            "-hls_list_size", "0",
            "-hls_playlist_type", "event",
            "-hls_flags", "independent_segments",
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

        # Probe and extract subtitles before starting the main transcode
        self.subtitle_tracks = await self.probe_subtitles(input_path)
        if self.subtitle_tracks:
            log.info(
                "Found %d subtitle track(s) for guild=%s session=%s: %s",
                len(self.subtitle_tracks),
                self.guild_id,
                self.session_id,
                [t["lang"] for t in self.subtitle_tracks],
            )
            await self.extract_subtitles(input_path, self.subtitle_tracks)

        # Probe audio tracks
        self.audio_tracks = await self.probe_audio_tracks(input_path)
        if self.audio_tracks:
            log.info(
                "Found %d audio track(s) for guild=%s session=%s: %s",
                len(self.audio_tracks),
                self.guild_id,
                self.session_id,
                [t["lang"] for t in self.audio_tracks],
            )

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

    # ------------------------------------------------------------------
    # Visualizer raw-frame input pipeline
    # ------------------------------------------------------------------

    def build_visualizer_ffmpeg_args(
        self,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
    ) -> list[str]:
        """Build ffmpeg args for raw frame input → QSV HLS output.

        Constructs an ffmpeg command that reads raw RGBA frames from stdin,
        uploads to QSV for hardware-accelerated H.264 encoding, and outputs
        a live-like HLS stream with short segments and a rolling window.

        The output directory is guild-level:
            /tmp/hellodj_hls/{guild_id}/viz/

        Args:
            width: Frame width in pixels (default 1280).
            height: Frame height in pixels (default 720).
            fps: Frames per second (default 30).

        Returns:
            Complete ffmpeg argument list suitable for asyncio subprocess.
        """
        viz_output_dir = _HLS_BASE_DIR / str(self.guild_id) / "viz"
        return [
            "ffmpeg", "-hide_banner", "-y",
            # Input: raw RGBA from stdin
            "-f", "rawvideo",
            "-pixel_format", "rgba",
            "-video_size", f"{width}x{height}",
            "-framerate", str(fps),
            "-i", "pipe:0",
            # Hardware upload + QSV encode
            "-vf", "format=nv12,hwupload=extra_hw_frames=64",
            "-c:v", "h264_qsv",
            "-preset", "veryfast",
            "-b:v", "2500k",
            "-maxrate", "3000k",
            "-bufsize", "6000k",
            # HLS output
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "5",
            "-hls_flags", "delete_segments+append_list",
            "-hls_segment_filename", str(viz_output_dir / "seg%05d.ts"),
            str(viz_output_dir / "playlist.m3u8"),
        ]

    @property
    def stdin_pipe(self) -> asyncio.StreamWriter | None:
        """Access the ffmpeg process stdin for writing raw frames.

        Returns None if the process has not been started with
        start_visualizer() or the process has exited.
        """
        if self.process is not None and self.process.stdin is not None:
            return self.process.stdin
        return None

    async def start_visualizer(
        self,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
    ) -> asyncio.StreamWriter:
        """Launch ffmpeg visualizer pipeline reading raw frames from stdin.

        Creates the visualizer output directory, builds the ffmpeg command
        for rawvideo stdin input with QSV HLS encoding, and spawns the
        process with stdin=PIPE.

        The segment watcher and watchdog are started as with the normal
        pipeline, so `self.ready` is set when the first .ts segment appears.

        Args:
            width: Frame width in pixels (default 1280).
            height: Frame height in pixels (default 720).
            fps: Frames per second (default 30).

        Returns:
            The process stdin pipe (asyncio.StreamWriter) for writing
            raw RGBA frame data.

        Raises:
            HLSTranscodePipelineError: If the process fails to start.
        """
        self._running = True
        self.ready.clear()
        self._complete_event.clear()

        # Visualizer output goes to guild-level "viz" subdirectory
        viz_output_dir = _HLS_BASE_DIR / str(self.guild_id) / "viz"
        self.output_dir = viz_output_dir
        self.playlist_path = viz_output_dir / "playlist.m3u8"

        # Ensure output directory exists
        viz_output_dir.mkdir(parents=True, exist_ok=True)

        args = self.build_visualizer_ffmpeg_args(width, height, fps)
        log.info("HLS visualizer pipeline starting: %s", " ".join(args))

        try:
            self.process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, OSError) as exc:
            self._running = False
            raise HLSTranscodePipelineError(
                f"Failed to start ffmpeg visualizer pipeline: {exc}"
            ) from exc

        # Start stderr monitoring
        self._stderr_buffer = []
        self._stderr_task = asyncio.ensure_future(self._monitor_stderr())

        # Start segment watcher (sets ready event on first .ts file)
        self._segment_watcher_task = asyncio.ensure_future(self._watch_segments())

        # Start watchdog
        self._last_segment_time = asyncio.get_event_loop().time()
        self._timeout_task = asyncio.ensure_future(self._watchdog())

        assert self.process.stdin is not None
        return self.process.stdin

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
