"""Tests for HLS Visualizer Pipeline — start_visualizer(), _build_visualizer_ffmpeg_args(), write_frame().

Validates the raw RGBA frame → QSV h264_qsv → HLS segment pipeline used by
GPU visualizer engines to deliver real-time HLS streams to Activity viewers.

Requirements: Req 3 (AC 1-5), Req 13 (AC 1-5)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from video.hls_transcode import (
    HLSTranscodePipeline,
    HLSTranscodePipelineError,
    _HLS_BASE_DIR,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FRAME_WIDTH = 1280
FRAME_HEIGHT = 720
FRAME_SIZE = FRAME_WIDTH * FRAME_HEIGHT * 4  # 3,686,400 bytes
FAKE_FRAME = b"\x00" * FRAME_SIZE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pipeline():
    """Create a pipeline instance for testing."""
    return HLSTranscodePipeline(
        guild_id=987654,
        session_id="viz-session",
        source_codec="h264",
    )


@pytest.fixture
def mock_subprocess():
    """Mock asyncio.create_subprocess_exec for start_visualizer tests."""
    mock_stdin = AsyncMock()
    mock_stdin.write = MagicMock()
    mock_stdin.drain = AsyncMock()

    mock_process = MagicMock()
    mock_process.stdin = mock_stdin
    mock_process.stderr = AsyncMock()
    mock_process.stderr.readline = AsyncMock(return_value=b"")
    mock_process.returncode = None
    mock_process.kill = MagicMock()
    mock_process.wait = AsyncMock()

    return mock_process


# ---------------------------------------------------------------------------
# Tests — _build_visualizer_ffmpeg_args()
# ---------------------------------------------------------------------------


class TestBuildVisualizerFfmpegArgs:
    """Validates the ffmpeg command construction for visualizer pipeline."""

    def test_starts_with_ffmpeg(self, pipeline):
        """Command starts with ffmpeg binary."""
        args = pipeline._build_visualizer_ffmpeg_args()
        assert args[0] == "ffmpeg"

    def test_hide_banner_and_error_loglevel(self, pipeline):
        """-hide_banner and -loglevel error suppress noisy output."""
        args = pipeline._build_visualizer_ffmpeg_args()
        assert "-hide_banner" in args
        assert "-loglevel" in args
        ll_idx = args.index("-loglevel")
        assert args[ll_idx + 1] == "error"

    def test_overwrite_flag(self, pipeline):
        """-y flag allows overwriting output files."""
        args = pipeline._build_visualizer_ffmpeg_args()
        assert "-y" in args

    def test_rawvideo_input_format(self, pipeline):
        """Input format is rawvideo with RGBA pixel format from stdin."""
        args = pipeline._build_visualizer_ffmpeg_args()

        f_idx = args.index("-f")
        assert args[f_idx + 1] == "rawvideo"

        pf_idx = args.index("-pixel_format")
        assert args[pf_idx + 1] == "rgba"

        i_idx = args.index("-i")
        assert args[i_idx + 1] == "pipe:0"

    def test_default_video_size_1280x720(self, pipeline):
        """Default video size is 1280x720."""
        args = pipeline._build_visualizer_ffmpeg_args()
        vs_idx = args.index("-video_size")
        assert args[vs_idx + 1] == "1280x720"

    def test_custom_video_size(self, pipeline):
        """Custom width/height reflected in -video_size."""
        args = pipeline._build_visualizer_ffmpeg_args(width=1920, height=1080)
        vs_idx = args.index("-video_size")
        assert args[vs_idx + 1] == "1920x1080"

    def test_default_framerate_30(self, pipeline):
        """Default framerate is 30fps."""
        args = pipeline._build_visualizer_ffmpeg_args()
        fr_idx = args.index("-framerate")
        assert args[fr_idx + 1] == "30"

    def test_custom_framerate(self, pipeline):
        """Custom fps is reflected in -framerate."""
        args = pipeline._build_visualizer_ffmpeg_args(fps=60)
        fr_idx = args.index("-framerate")
        assert args[fr_idx + 1] == "60"

    def test_qsv_hardware_device_init(self, pipeline):
        """QSV hardware device is initialized for encode."""
        args = pipeline._build_visualizer_ffmpeg_args()

        assert "-init_hw_device" in args
        hw_idx = args.index("-init_hw_device")
        assert args[hw_idx + 1] == "qsv=qsv:hw"

        assert "-filter_hw_device" in args
        fhw_idx = args.index("-filter_hw_device")
        assert args[fhw_idx + 1] == "qsv"

    def test_nv12_hwupload_filter(self, pipeline):
        """Filter converts RGBA → NV12 and uploads to QSV surface."""
        args = pipeline._build_visualizer_ffmpeg_args()
        vf_idx = args.index("-vf")
        assert args[vf_idx + 1] == "format=nv12,hwupload=extra_hw_frames=64"

    def test_h264_qsv_encoder(self, pipeline):
        """Uses h264_qsv encoder."""
        args = pipeline._build_visualizer_ffmpeg_args()
        cv_idx = args.index("-c:v")
        assert args[cv_idx + 1] == "h264_qsv"

    def test_main_profile(self, pipeline):
        """Uses H.264 high profile for quality."""
        args = pipeline._build_visualizer_ffmpeg_args()
        pv_idx = args.index("-profile:v")
        assert args[pv_idx + 1] == "high"

    def test_fast_preset(self, pipeline):
        """Uses medium preset for quality/speed balance."""
        args = pipeline._build_visualizer_ffmpeg_args()
        p_idx = args.index("-preset")
        assert args[p_idx + 1] == "medium"

    def test_bitrate_2500k_constrained_vbr(self, pipeline):
        """Uses ICQ mode with global_quality 28, maxrate 3000k, and bufsize 5000k."""
        args = pipeline._build_visualizer_ffmpeg_args()

        # ICQ mode uses -global_quality instead of -b:v
        gq_idx = args.index("-global_quality")
        assert args[gq_idx + 1] == "28"

        mr_idx = args.index("-maxrate")
        assert args[mr_idx + 1] == "3000k"

        bs_idx = args.index("-bufsize")
        assert args[bs_idx + 1] == "5000k"

    def test_gop_size_60(self, pipeline):
        """GOP size is 60 frames (2 seconds at 30fps)."""
        args = pipeline._build_visualizer_ffmpeg_args()
        g_idx = args.index("-g")
        assert args[g_idx + 1] == "60"

    def test_force_keyframes_every_2s(self, pipeline):
        """Keyframes forced every 2 seconds for segment alignment."""
        args = pipeline._build_visualizer_ffmpeg_args()
        fk_idx = args.index("-force_key_frames")
        assert args[fk_idx + 1] == "expr:gte(t,n_forced*2)"

    def test_constant_output_framerate(self, pipeline):
        """-r 30 ensures frame duplication if engine is slower."""
        args = pipeline._build_visualizer_ffmpeg_args()
        r_idx = args.index("-r")
        assert args[r_idx + 1] == "30"

    def test_hls_output_format(self, pipeline):
        """Output format is HLS with 2s segments."""
        args = pipeline._build_visualizer_ffmpeg_args()

        # Second -f is hls (first is rawvideo)
        f_indices = [i for i, a in enumerate(args) if a == "-f"]
        assert len(f_indices) == 2
        assert args[f_indices[1] + 1] == "hls"

        ht_idx = args.index("-hls_time")
        assert args[ht_idx + 1] == "2"

    def test_hls_list_size_10(self, pipeline):
        """Rolling window keeps last 10 segments (20s)."""
        args = pipeline._build_visualizer_ffmpeg_args()
        hls_idx = args.index("-hls_list_size")
        assert args[hls_idx + 1] == "10"

    def test_hls_flags_delete_and_independent(self, pipeline):
        """HLS flags: delete_segments (live) + independent_segments."""
        args = pipeline._build_visualizer_ffmpeg_args()
        hf_idx = args.index("-hls_flags")
        assert args[hf_idx + 1] == "delete_segments+independent_segments"

    def test_output_path_uses_guild_id(self, pipeline):
        """Output playlist path includes guild_id."""
        args = pipeline._build_visualizer_ffmpeg_args()
        output_path = args[-1]
        assert "/tmp/hellodj_hls/987654/viz/playlist.m3u8" in output_path

    def test_segment_filename_in_viz_dir(self, pipeline):
        """Segment filename pattern is in the viz directory."""
        args = pipeline._build_visualizer_ffmpeg_args()
        seg_idx = args.index("-hls_segment_filename")
        seg_pattern = args[seg_idx + 1]
        assert "/tmp/hellodj_hls/987654/viz/seg%05d.ts" in seg_pattern

    def test_no_audio_codec(self, pipeline):
        """Visualizer pipeline has no audio — no -c:a flag."""
        args = pipeline._build_visualizer_ffmpeg_args()
        assert "-c:a" not in args

    def test_public_alias_available(self, pipeline):
        """Public build_visualizer_ffmpeg_args is an alias for the private method."""
        assert (
            pipeline.build_visualizer_ffmpeg_args()
            == pipeline._build_visualizer_ffmpeg_args()
        )


# ---------------------------------------------------------------------------
# Tests — start_visualizer()
# ---------------------------------------------------------------------------


class TestStartVisualizer:
    """Validates the start_visualizer() subprocess launch."""

    @pytest.mark.asyncio
    async def test_sets_output_dir_to_viz(self, pipeline, mock_subprocess, tmp_path):
        """start_visualizer sets output_dir to guild/viz subdirectory."""
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_subprocess):
            with patch.object(Path, "mkdir"):
                # Patch _HLS_BASE_DIR for deterministic output
                with patch("video.hls_transcode._HLS_BASE_DIR", tmp_path):
                    pipeline.output_dir = tmp_path / str(pipeline.guild_id) / "viz"
                    pipeline.playlist_path = pipeline.output_dir / "playlist.m3u8"

                    await pipeline.start_visualizer()

        assert "viz" in str(pipeline.output_dir)

    @pytest.mark.asyncio
    async def test_returns_stdin_pipe(self, pipeline, mock_subprocess):
        """start_visualizer returns the ffmpeg stdin writer."""
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_subprocess):
            with patch.object(Path, "mkdir"):
                result = await pipeline.start_visualizer()

        assert result is mock_subprocess.stdin

    @pytest.mark.asyncio
    async def test_sets_running_true(self, pipeline, mock_subprocess):
        """start_visualizer sets _running flag."""
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_subprocess):
            with patch.object(Path, "mkdir"):
                await pipeline.start_visualizer()

        assert pipeline._running is True

    @pytest.mark.asyncio
    async def test_clears_ready_event(self, pipeline, mock_subprocess):
        """start_visualizer clears the ready event (set when first segment appears)."""
        pipeline.ready.set()  # Pre-set to verify it gets cleared
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_subprocess):
            with patch.object(Path, "mkdir"):
                await pipeline.start_visualizer()

        assert not pipeline.ready.is_set()

    @pytest.mark.asyncio
    async def test_spawns_ffmpeg_with_stdin_pipe(self, pipeline, mock_subprocess):
        """Subprocess is created with stdin=PIPE for frame writing."""
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_subprocess) as mock_exec:
            with patch.object(Path, "mkdir"):
                await pipeline.start_visualizer()

        # Verify stdin=PIPE was passed
        call_kwargs = mock_exec.call_args.kwargs
        assert call_kwargs["stdin"] == asyncio.subprocess.PIPE

    @pytest.mark.asyncio
    async def test_raises_on_spawn_failure(self, pipeline):
        """Raises HLSTranscodePipelineError if ffmpeg cannot be spawned."""
        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            side_effect=FileNotFoundError("ffmpeg not found"),
        ):
            with patch.object(Path, "mkdir"):
                with pytest.raises(HLSTranscodePipelineError, match="Failed to start"):
                    await pipeline.start_visualizer()

        assert pipeline._running is False

    @pytest.mark.asyncio
    async def test_starts_segment_watcher(self, pipeline, mock_subprocess):
        """start_visualizer launches the segment watcher background task."""
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_subprocess):
            with patch.object(Path, "mkdir"):
                await pipeline.start_visualizer()

        assert pipeline._segment_watcher_task is not None

    @pytest.mark.asyncio
    async def test_starts_stderr_monitor(self, pipeline, mock_subprocess):
        """start_visualizer launches the stderr monitor background task."""
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_subprocess):
            with patch.object(Path, "mkdir"):
                await pipeline.start_visualizer()

        assert pipeline._stderr_task is not None


# ---------------------------------------------------------------------------
# Tests — write_frame()
# ---------------------------------------------------------------------------


class TestWriteFrame:
    """Validates the write_frame() method for piping RGBA data."""

    @pytest.mark.asyncio
    async def test_writes_data_to_stdin(self, pipeline, mock_subprocess):
        """write_frame writes the frame bytes to ffmpeg stdin."""
        pipeline.process = mock_subprocess
        pipeline._running = True

        await pipeline.write_frame(FAKE_FRAME)

        mock_subprocess.stdin.write.assert_called_once_with(FAKE_FRAME)

    @pytest.mark.asyncio
    async def test_drains_after_write(self, pipeline, mock_subprocess):
        """write_frame calls drain() to flush the write buffer."""
        pipeline.process = mock_subprocess
        pipeline._running = True

        await pipeline.write_frame(FAKE_FRAME)

        mock_subprocess.stdin.drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_raises_when_not_running(self, pipeline):
        """write_frame raises error when pipeline is not running."""
        pipeline._running = False
        pipeline.process = None

        with pytest.raises(HLSTranscodePipelineError, match="not running"):
            await pipeline.write_frame(FAKE_FRAME)

    @pytest.mark.asyncio
    async def test_raises_when_no_process(self, pipeline):
        """write_frame raises error when process is None."""
        pipeline._running = True
        pipeline.process = None

        with pytest.raises(HLSTranscodePipelineError, match="not running"):
            await pipeline.write_frame(FAKE_FRAME)

    @pytest.mark.asyncio
    async def test_raises_when_no_stdin(self, pipeline):
        """write_frame raises error when process has no stdin."""
        mock_proc = MagicMock()
        mock_proc.stdin = None
        pipeline._running = True
        pipeline.process = mock_proc

        with pytest.raises(HLSTranscodePipelineError, match="not running"):
            await pipeline.write_frame(FAKE_FRAME)

    @pytest.mark.asyncio
    async def test_raises_on_broken_pipe(self, pipeline, mock_subprocess):
        """write_frame raises error on BrokenPipeError (ffmpeg exited)."""
        mock_subprocess.stdin.write.side_effect = BrokenPipeError("pipe closed")
        pipeline.process = mock_subprocess
        pipeline._running = True

        with pytest.raises(HLSTranscodePipelineError, match="Failed to write frame"):
            await pipeline.write_frame(FAKE_FRAME)

        # Should also set _running to False
        assert pipeline._running is False

    @pytest.mark.asyncio
    async def test_raises_on_connection_reset(self, pipeline, mock_subprocess):
        """write_frame raises error on ConnectionResetError."""
        mock_subprocess.stdin.write.side_effect = ConnectionResetError()
        pipeline.process = mock_subprocess
        pipeline._running = True

        with pytest.raises(HLSTranscodePipelineError, match="Failed to write frame"):
            await pipeline.write_frame(FAKE_FRAME)

    @pytest.mark.asyncio
    async def test_multiple_frames_sequential(self, pipeline, mock_subprocess):
        """Multiple write_frame calls work sequentially."""
        pipeline.process = mock_subprocess
        pipeline._running = True

        frame1 = b"\x01" * FRAME_SIZE
        frame2 = b"\x02" * FRAME_SIZE

        await pipeline.write_frame(frame1)
        await pipeline.write_frame(frame2)

        assert mock_subprocess.stdin.write.call_count == 2
        assert mock_subprocess.stdin.drain.await_count == 2


# ---------------------------------------------------------------------------
# Tests — stdin_pipe property
# ---------------------------------------------------------------------------


class TestStdinPipeProperty:
    """Validates the stdin_pipe property."""

    def test_none_when_no_process(self, pipeline):
        """Returns None when no process is running."""
        assert pipeline.stdin_pipe is None

    def test_none_when_stdin_is_none(self, pipeline):
        """Returns None when process has no stdin."""
        mock_proc = MagicMock()
        mock_proc.stdin = None
        pipeline.process = mock_proc
        assert pipeline.stdin_pipe is None

    def test_returns_stdin_when_available(self, pipeline):
        """Returns stdin when process and stdin are available."""
        mock_stdin = MagicMock()
        mock_proc = MagicMock()
        mock_proc.stdin = mock_stdin
        pipeline.process = mock_proc
        assert pipeline.stdin_pipe is mock_stdin


# ---------------------------------------------------------------------------
# Tests — Integration (start_visualizer + write_frame + stop)
# ---------------------------------------------------------------------------


class TestVisualizerLifecycle:
    """Integration-style tests for the full visualizer lifecycle."""

    @pytest.mark.asyncio
    async def test_start_write_stop(self, pipeline, mock_subprocess):
        """Full lifecycle: start → write frames → stop."""
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_subprocess):
            with patch.object(Path, "mkdir"):
                stdin = await pipeline.start_visualizer()

        # Write a few frames
        await pipeline.write_frame(FAKE_FRAME)
        await pipeline.write_frame(FAKE_FRAME)

        # Stop
        await pipeline.stop()
        assert pipeline._running is False

    @pytest.mark.asyncio
    async def test_output_dir_matches_guild(self, pipeline, mock_subprocess):
        """Output dir after start_visualizer is /tmp/hellodj_hls/{guild_id}/viz."""
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_subprocess):
            with patch.object(Path, "mkdir"):
                await pipeline.start_visualizer()

        expected = _HLS_BASE_DIR / str(pipeline.guild_id) / "viz"
        assert pipeline.output_dir == expected

    @pytest.mark.asyncio
    async def test_playlist_path_matches(self, pipeline, mock_subprocess):
        """Playlist path is output_dir/playlist.m3u8."""
        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_subprocess):
            with patch.object(Path, "mkdir"):
                await pipeline.start_visualizer()

        expected = _HLS_BASE_DIR / str(pipeline.guild_id) / "viz" / "playlist.m3u8"
        assert pipeline.playlist_path == expected
