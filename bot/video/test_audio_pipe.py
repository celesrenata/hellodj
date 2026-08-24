"""End-to-end integration tests for the Lavalink Audio Pipe feature.

Unit tests for locally-testable components:
- AudioPipeSession (FIFO lifecycle)
- _build_atempo_chain (FFmpeg filter chain generation)
- _build_streaming_ffmpeg_args (pipe input mode args)

Integration test checklist for live-service scenarios is documented
in the TestIntegrationChecklist class (marked skip — requires live services).
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from video.audio_pipe import AudioPipeSession, cleanup_orphaned_pipes
from video.hls_transcode import HLSTranscodePipeline


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pipeline():
    """Create a pipeline instance for testing."""
    return HLSTranscodePipeline(
        guild_id=999888,
        session_id="pipe-test-session",
        source_codec="h264",
    )


@pytest.fixture
def tmp_hls_dir(tmp_path):
    """Patch _HLS_BASE_DIR to a temp directory for safe FIFO creation."""
    with patch("video.audio_pipe._HLS_BASE_DIR", tmp_path):
        yield tmp_path


# ---------------------------------------------------------------------------
# AudioPipeSession Unit Tests
# ---------------------------------------------------------------------------


class TestAudioPipeSession:
    """Unit tests for AudioPipeSession FIFO lifecycle management."""

    def test_start_creates_fifo(self, tmp_hls_dir):
        """start() creates a named FIFO at the expected path."""
        import asyncio

        session = AudioPipeSession(guild_id=12345, session_id="sess-001")
        result = asyncio.run(session.start())

        assert result is True
        assert session.active is True
        assert session.pipe_path.exists()
        assert stat.S_ISFIFO(session.pipe_path.stat().st_mode)

    def test_stop_removes_fifo(self, tmp_hls_dir):
        """stop() removes the FIFO and marks session inactive."""
        import asyncio

        session = AudioPipeSession(guild_id=12345, session_id="sess-002")

        async def _run():
            await session.start()
            assert session.pipe_path.exists()
            await session.stop()

        asyncio.run(_run())
        assert session.active is False
        assert not session.pipe_path.exists()

    def test_stop_cleans_empty_session_dir(self, tmp_hls_dir):
        """stop() removes the empty session directory after FIFO removal."""
        import asyncio

        session = AudioPipeSession(guild_id=12345, session_id="sess-003")

        async def _run():
            await session.start()
            session_dir = session.pipe_path.parent
            assert session_dir.exists()
            await session.stop()
            return session_dir

        session_dir = asyncio.run(_run())
        assert not session_dir.exists()

    def test_ffmpeg_input_path_returns_string(self, tmp_hls_dir):
        """ffmpeg_input_path returns the pipe path as a string."""
        session = AudioPipeSession(guild_id=67890, session_id="sess-004")

        path_str = session.ffmpeg_input_path
        assert isinstance(path_str, str)
        assert "67890" in path_str
        assert "sess-004" in path_str
        assert path_str.endswith("audio.pipe")

    def test_pipe_path_contains_guild_and_session(self, tmp_hls_dir):
        """pipe_path includes guild_id and session_id in the path."""
        session = AudioPipeSession(guild_id=11111, session_id="my-session")

        assert "11111" in str(session.pipe_path)
        assert "my-session" in str(session.pipe_path)

    def test_start_removes_stale_pipe(self, tmp_hls_dir):
        """start() removes a stale pipe at the same path before creating a new one."""
        import asyncio

        session = AudioPipeSession(guild_id=12345, session_id="sess-005")

        # Manually create a stale FIFO
        session.pipe_path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(session.pipe_path)

        result = asyncio.run(session.start())

        assert result is True
        assert session.active is True
        assert stat.S_ISFIFO(session.pipe_path.stat().st_mode)

    def test_cleanup_orphaned_pipes(self, tmp_hls_dir):
        """cleanup_orphaned_pipes() finds and removes stale FIFOs."""
        # Create some orphaned pipes
        for guild_id in (111, 222):
            pipe_dir = tmp_hls_dir / str(guild_id) / "old-session"
            pipe_dir.mkdir(parents=True)
            os.mkfifo(pipe_dir / "audio.pipe")

        # Also create a regular file (should NOT be cleaned)
        regular_dir = tmp_hls_dir / "333" / "some-session"
        regular_dir.mkdir(parents=True)
        (regular_dir / "audio.pipe").write_text("not a fifo")

        count = cleanup_orphaned_pipes()

        assert count == 2
        # Regular file should still exist
        assert (regular_dir / "audio.pipe").exists()


# ---------------------------------------------------------------------------
# _build_atempo_chain Unit Tests
# ---------------------------------------------------------------------------


class TestBuildAtemoChain:
    """Unit tests for _build_atempo_chain() speed decomposition."""

    def test_speed_1_0_identity(self, pipeline):
        """speed=1.0 → 'atempo=1.0' (identity)."""
        result = pipeline._build_atempo_chain(1.0)
        assert result == "atempo=1.0"

    def test_speed_1_25(self, pipeline):
        """speed=1.25 → 'atempo=1.25' (within single filter range)."""
        result = pipeline._build_atempo_chain(1.25)
        assert result == "atempo=1.25"

    def test_speed_4_0_chained(self, pipeline):
        """speed=4.0 → chained atempo=2.0,atempo=2 (two doublings)."""
        result = pipeline._build_atempo_chain(4.0)
        # :.6g format drops trailing zeros: 2.0 → "2", 4.0/2.0=2.0 → "2"
        assert result == "atempo=2.0,atempo=2"

    def test_speed_0_25_chained(self, pipeline):
        """speed=0.25 → 'atempo=0.5,atempo=0.5' (two halvings)."""
        result = pipeline._build_atempo_chain(0.25)
        assert result == "atempo=0.5,atempo=0.5"

    def test_speed_3_0_chained(self, pipeline):
        """speed=3.0 → 'atempo=2.0,atempo=1.5' (factor then remainder)."""
        result = pipeline._build_atempo_chain(3.0)
        assert result == "atempo=2.0,atempo=1.5"

    def test_speed_0_0_raises_valueerror(self, pipeline):
        """speed=0.0 raises ValueError."""
        with pytest.raises(ValueError, match="positive"):
            pipeline._build_atempo_chain(0.0)

    def test_negative_speed_raises_valueerror(self, pipeline):
        """Negative speed raises ValueError."""
        with pytest.raises(ValueError, match="positive"):
            pipeline._build_atempo_chain(-1.0)

    def test_speed_2_0_single_filter(self, pipeline):
        """speed=2.0 → 'atempo=2' (boundary, single filter)."""
        result = pipeline._build_atempo_chain(2.0)
        # :.6g format: 2.0 → "2"
        assert result == "atempo=2"

    def test_speed_0_5_single_filter(self, pipeline):
        """speed=0.5 → 'atempo=0.5' (boundary, single filter)."""
        result = pipeline._build_atempo_chain(0.5)
        assert result == "atempo=0.5"


# ---------------------------------------------------------------------------
# FFmpeg Streaming Args with Pipe Input Tests
# ---------------------------------------------------------------------------


class TestBuildStreamingFfmpegArgsWithPipe:
    """Tests for _build_streaming_ffmpeg_args() when audio_pipe_path is set."""

    def _res_720p(self):
        from video import Resolution
        return Resolution.RES_720P

    def test_pipe_input_format_specifiers(self, pipeline):
        """When pipe path set, args contain -f s16le -ar 48000 -ac 2 -i {path}."""
        pipe_path = "/tmp/hellodj_hls/999/sess/audio.pipe"
        args = pipeline._build_streaming_ffmpeg_args(
            "http://example.com/video.m3u8",
            self._res_720p(),
            audio_pipe_path=pipe_path,
        )

        # Find the pipe input section
        assert "-f" in args
        assert "s16le" in args
        assert "-ar" in args
        assert "48000" in args
        assert "-ac" in args
        assert "2" in args
        assert pipe_path in args

    def test_pipe_input_has_thread_queue_size(self, pipeline):
        """Pipe input has -thread_queue_size for buffering."""
        pipe_path = "/tmp/hellodj_hls/999/sess/audio.pipe"
        args = pipeline._build_streaming_ffmpeg_args(
            "http://example.com/video.m3u8",
            self._res_720p(),
            audio_pipe_path=pipe_path,
        )

        # Find the thread_queue_size before the pipe input
        pipe_idx = args.index(pipe_path)
        # Look backwards for -thread_queue_size before the pipe -i
        preceding_args = args[:pipe_idx]
        tqs_indices = [i for i, a in enumerate(preceding_args) if a == "-thread_queue_size"]
        # Should have at least one thread_queue_size before the pipe input
        # (the video input also has one, so we want at least 2 total)
        all_tqs = [i for i, a in enumerate(args) if a == "-thread_queue_size"]
        assert len(all_tqs) >= 2, f"Expected at least 2 -thread_queue_size, got {len(all_tqs)}"

    def test_pipe_maps_video_and_pipe_audio(self, pipeline):
        """With pipe, maps -map 0:v:0 -map 1:a:0."""
        pipe_path = "/tmp/hellodj_hls/999/sess/audio.pipe"
        args = pipeline._build_streaming_ffmpeg_args(
            "http://example.com/video.m3u8",
            self._res_720p(),
            audio_pipe_path=pipe_path,
        )

        assert "-map" in args
        map_indices = [i for i, a in enumerate(args) if a == "-map"]
        mapped_values = [args[i + 1] for i in map_indices]
        assert "0:v:0" in mapped_values
        assert "1:a:0" in mapped_values

    def test_timescale_adds_setpts_and_atempo(self, pipeline):
        """When timescale_speed != 1.0, setpts and atempo are in the args."""
        pipe_path = "/tmp/hellodj_hls/999/sess/audio.pipe"
        args = pipeline._build_streaming_ffmpeg_args(
            "http://example.com/video.m3u8",
            self._res_720p(),
            audio_pipe_path=pipe_path,
            timescale_speed=1.25,
        )

        # Check setpts in video filter
        vf_idx = args.index("-vf")
        vf_value = args[vf_idx + 1]
        assert "setpts=PTS/1.25" in vf_value

        # Check atempo in audio filter
        af_idx = args.index("-af")
        af_value = args[af_idx + 1]
        assert "atempo=1.25" in af_value

    def test_no_timescale_no_af_flag(self, pipeline):
        """When timescale_speed=1.0, no -af flag is added."""
        pipe_path = "/tmp/hellodj_hls/999/sess/audio.pipe"
        args = pipeline._build_streaming_ffmpeg_args(
            "http://example.com/video.m3u8",
            self._res_720p(),
            audio_pipe_path=pipe_path,
            timescale_speed=1.0,
        )

        assert "-af" not in args

    def test_no_readrate_with_pipe(self, pipeline):
        """Pipe mode disables readrate throttling (FIFO provides natural throttle)."""
        pipe_path = "/tmp/hellodj_hls/999/sess/audio.pipe"
        args = pipeline._build_streaming_ffmpeg_args(
            "http://example.com/video.m3u8",
            self._res_720p(),
            audio_pipe_path=pipe_path,
        )

        assert "-readrate" not in args

    def test_no_reconnect_with_pipe(self, pipeline):
        """Pipe mode disables HTTP reconnect flags (local FIFO)."""
        pipe_path = "/tmp/hellodj_hls/999/sess/audio.pipe"
        args = pipeline._build_streaming_ffmpeg_args(
            "http://example.com/video.m3u8",
            self._res_720p(),
            audio_pipe_path=pipe_path,
        )

        assert "-reconnect" not in args

    def test_without_pipe_uses_source_audio(self, pipeline):
        """Without pipe path, no s16le/48000/stereo input is added."""
        args = pipeline._build_streaming_ffmpeg_args(
            "http://example.com/video.m3u8",
            self._res_720p(),
        )

        assert "s16le" not in args
        # Should not have dual-input mapping
        assert "-map" not in args


# ---------------------------------------------------------------------------
# Integration Test Checklist (requires live services — marked skip)
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Requires live Lavalink, Discord bot, and audio playback")
class TestIntegrationChecklist:
    """Integration test scenarios — manual validation against live services.

    These tests document the E2E scenarios that must be verified before
    the audio pipe feature is considered production-ready.

    Run manually with: pytest -k TestIntegrationChecklist --no-header -rN
    (remove the skip mark and configure live services)
    """

    def test_video_with_eq_has_filtered_audio(self):
        """Scenario 1: Video with EQ → HLS segments contain filtered audio.

        Steps:
        1. Play a music video with the /play command
        2. Apply an EQ preset (e.g., /filter bass_boost)
        3. Verify HLS segments contain audio that differs from source
        4. Compare frequency spectrum of HLS audio vs unfiltered source

        Expected: EQ curve is audibly applied in HLS output.
        """
        pass

    def test_nightcore_speeds_video_and_audio(self):
        """Scenario 2: Nightcore 1.25x → pipeline restarts, both sped up.

        Steps:
        1. Play a music video (pipe active)
        2. Apply /filter nightcore (timescale speed=1.25)
        3. Verify pipeline restarts (new ffmpeg process)
        4. Verify HLS video has setpts=PTS/1.25 (faster playback)
        5. Verify HLS audio has atempo=1.25 (pitched up + faster)
        6. Verify total segment duration is ~80% of original

        Expected: Both video and audio are 1.25x speed in HLS output.
        """
        pass

    def test_filter_reset_falls_back_to_source(self):
        """Scenario 3: Filter reset → falls back to source audio.

        Steps:
        1. Play a music video with filters active (pipe enabled)
        2. Reset all filters (/filter reset)
        3. Verify pipe is disabled (Lavalink API)
        4. Verify pipeline restarts without pipe input
        5. Verify HLS audio matches source (no filtering)

        Expected: HLS uses source audio directly, no pipe involvement.
        """
        pass

    def test_kill_fifo_graceful_fallback(self):
        """Scenario 4: Kill FIFO → graceful fallback.

        Steps:
        1. Play a music video with pipe active
        2. Manually delete the FIFO: rm /tmp/hellodj_hls/.../audio.pipe
        3. Verify FFmpeg detects EOF/error on pipe read
        4. Verify bot restarts pipeline with source audio
        5. Verify video playback continues without interruption

        Expected: Graceful degradation to source audio within ~5 seconds.
        """
        pass

    def test_skip_during_pipe_clean_transition(self):
        """Scenario 5: Skip during active pipe → clean transition.

        Steps:
        1. Play a music video with pipe active
        2. Skip to next track in queue
        3. Verify pipe session is stopped (FIFO removed)
        4. Verify Lavalink pipe is disabled
        5. Verify new track starts cleanly (new pipe if filters still active)
        6. Verify no orphaned FIFOs remain

        Expected: Clean FIFO cleanup and fresh pipe for next track.
        """
        pass

    def test_discord_vc_receives_full_filters(self):
        """Scenario 6: Discord VC receives full filters simultaneously.

        Steps:
        1. Play a music video with filters (e.g., EQ + nightcore)
        2. Join the voice channel as a listener (not in Activity)
        3. Verify voice audio has ALL filters applied (including timescale)
        4. Verify Activity video has all non-timing filters + FFmpeg timescale
        5. Compare: VC audio should sound identical to Activity audio

        Expected: Dual output — VC gets full filter chain, Activity gets
        non-timing via pipe + timing via FFmpeg. Result sounds the same.
        """
        pass
