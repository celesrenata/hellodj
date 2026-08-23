"""Tests for HLSTranscodePipeline.build_ffmpeg_args() multi-audio support."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from video.hls_transcode import HLSTranscodePipeline


@pytest.fixture
def pipeline():
    """Create a pipeline instance for testing."""
    return HLSTranscodePipeline(
        guild_id=123456,
        session_id="test-session",
        source_codec="h264",
    )


class TestBuildFfmpegArgsSingleAudio:
    """When 0 or 1 audio track: muxed A/V output (original behavior)."""

    def test_no_audio_tracks_uses_single_output(self, pipeline):
        """Empty audio_tracks → single muxed playlist.m3u8 output."""
        pipeline.audio_tracks = []
        args = pipeline.build_ffmpeg_args("/input.mkv", _res_720p())

        # Should NOT contain -var_stream_map or -master_pl_name
        assert "-var_stream_map" not in args
        assert "-master_pl_name" not in args

        # Output should be the direct playlist path
        assert str(pipeline.playlist_path) in args

        # Segment pattern should be simple seg%05d.ts
        seg_idx = args.index("-hls_segment_filename") + 1
        assert "seg%05d.ts" in args[seg_idx]
        assert "%v" not in args[seg_idx]

    def test_one_audio_track_uses_single_output(self, pipeline):
        """Single audio track → same as no multi-audio."""
        pipeline.audio_tracks = [{"lang": "en", "label": "English", "stream_index": 1}]
        args = pipeline.build_ffmpeg_args("/input.mkv", _res_720p())

        assert "-var_stream_map" not in args
        assert "-master_pl_name" not in args
        assert str(pipeline.playlist_path) in args

    def test_single_audio_no_explicit_map(self, pipeline):
        """Single audio should not have explicit -map args."""
        pipeline.audio_tracks = [{"lang": "ja", "label": "Japanese", "stream_index": 1}]
        args = pipeline.build_ffmpeg_args("/input.mkv", _res_720p())

        assert "-map" not in args


class TestBuildFfmpegArgsMultiAudio:
    """When > 1 audio track: multi-variant HLS output."""

    def test_two_audio_tracks_uses_var_stream_map(self, pipeline):
        """Two audio tracks → -var_stream_map with video + 2 audio variants."""
        pipeline.audio_tracks = [
            {"lang": "ja", "label": "Japanese", "stream_index": 1},
            {"lang": "en", "label": "English", "stream_index": 2},
        ]
        args = pipeline.build_ffmpeg_args("/input.mkv", _res_720p())

        assert "-var_stream_map" in args
        vsm_idx = args.index("-var_stream_map") + 1
        vsm = args[vsm_idx]

        # Video variant
        assert "v:0,name:video,agroup:audio" in vsm
        # Audio variants with language tags
        assert "a:0,name:audio_ja,agroup:audio,language:ja" in vsm
        assert "a:1,name:audio_en,agroup:audio,language:en" in vsm

    def test_multi_audio_has_master_pl_name(self, pipeline):
        """Multi-audio output specifies -master_pl_name playlist.m3u8."""
        pipeline.audio_tracks = [
            {"lang": "ja", "label": "Japanese", "stream_index": 1},
            {"lang": "en", "label": "English", "stream_index": 2},
        ]
        args = pipeline.build_ffmpeg_args("/input.mkv", _res_720p())

        assert "-master_pl_name" in args
        mpl_idx = args.index("-master_pl_name") + 1
        assert args[mpl_idx] == "playlist.m3u8"

    def test_multi_audio_segment_pattern_uses_variant_name(self, pipeline):
        """Segment filename pattern uses %v for variant name substitution."""
        pipeline.audio_tracks = [
            {"lang": "ja", "label": "Japanese", "stream_index": 1},
            {"lang": "en", "label": "English", "stream_index": 2},
        ]
        args = pipeline.build_ffmpeg_args("/input.mkv", _res_720p())

        seg_idx = args.index("-hls_segment_filename") + 1
        assert "%v" in args[seg_idx]
        assert "seg%05d_%v.ts" in args[seg_idx]

    def test_multi_audio_output_uses_variant_m3u8(self, pipeline):
        """Output path template uses %v.m3u8 for per-variant playlists."""
        pipeline.audio_tracks = [
            {"lang": "ja", "label": "Japanese", "stream_index": 1},
            {"lang": "en", "label": "English", "stream_index": 2},
        ]
        args = pipeline.build_ffmpeg_args("/input.mkv", _res_720p())

        # Last arg should be the output template with %v
        output_arg = args[-1]
        assert output_arg.endswith("%v.m3u8")

    def test_multi_audio_maps_all_streams(self, pipeline):
        """Multi-audio explicitly maps video + each audio stream."""
        pipeline.audio_tracks = [
            {"lang": "ja", "label": "Japanese", "stream_index": 1},
            {"lang": "en", "label": "English", "stream_index": 2},
            {"lang": "fr", "label": "French", "stream_index": 3},
        ]
        args = pipeline.build_ffmpeg_args("/input.mkv", _res_720p())

        # Count -map occurrences
        map_indices = [i for i, a in enumerate(args) if a == "-map"]
        assert len(map_indices) == 4  # 1 video + 3 audio

        # Check mapped streams
        mapped = [args[i + 1] for i in map_indices]
        assert "0:v:0" in mapped
        assert "0:a:0" in mapped
        assert "0:a:1" in mapped
        assert "0:a:2" in mapped

    def test_multi_audio_fallback_lang_name(self, pipeline):
        """When track has no lang, falls back to indexed name aud{idx}."""
        pipeline.audio_tracks = [
            {"label": "Unknown", "stream_index": 1},
            {"lang": "en", "label": "English", "stream_index": 2},
        ]
        args = pipeline.build_ffmpeg_args("/input.mkv", _res_720p())

        vsm_idx = args.index("-var_stream_map") + 1
        vsm = args[vsm_idx]

        # First track should use fallback name
        assert "name:audio_aud0" in vsm
        assert "language:aud0" in vsm


def _res_720p():
    """Create a 720p Resolution for testing."""
    from video import Resolution
    return Resolution.RES_720P


class TestBuildVisualizerFfmpegArgs:
    """Tests for _build_visualizer_ffmpeg_args() — rawvideo stdin → QSV HLS."""

    def test_default_args_structure(self, pipeline):
        """Default parameters produce a valid ffmpeg command for rawvideo input."""
        args = pipeline._build_visualizer_ffmpeg_args()

        assert args[0] == "ffmpeg"
        assert "-hide_banner" in args
        assert "-loglevel" in args
        assert "-y" in args

    def test_rawvideo_input_format(self, pipeline):
        """Input should be raw video from stdin (pipe:0)."""
        args = pipeline._build_visualizer_ffmpeg_args()

        # rawvideo format
        f_idx = args.index("-f")
        assert args[f_idx + 1] == "rawvideo"

        # pixel format
        pf_idx = args.index("-pixel_format")
        assert args[pf_idx + 1] == "rgba"

        # input is stdin pipe
        i_idx = args.index("-i")
        assert args[i_idx + 1] == "pipe:0"

    def test_default_resolution(self, pipeline):
        """Default resolution is 1280x720."""
        args = pipeline._build_visualizer_ffmpeg_args()

        vs_idx = args.index("-video_size")
        assert args[vs_idx + 1] == "1280x720"

    def test_custom_resolution(self, pipeline):
        """Custom width/height are reflected in -video_size."""
        args = pipeline._build_visualizer_ffmpeg_args(width=1920, height=1080)

        vs_idx = args.index("-video_size")
        assert args[vs_idx + 1] == "1920x1080"

    def test_default_framerate(self, pipeline):
        """Default framerate is 30."""
        args = pipeline._build_visualizer_ffmpeg_args()

        fr_idx = args.index("-framerate")
        assert args[fr_idx + 1] == "30"

    def test_custom_framerate(self, pipeline):
        """Custom fps is reflected in -framerate."""
        args = pipeline._build_visualizer_ffmpeg_args(fps=60)

        fr_idx = args.index("-framerate")
        assert args[fr_idx + 1] == "60"

    def test_qsv_encode_settings(self, pipeline):
        """Should use h264_qsv encoder with fast preset."""
        args = pipeline._build_visualizer_ffmpeg_args()

        assert "-c:v" in args
        cv_idx = args.index("-c:v")
        assert args[cv_idx + 1] == "h264_qsv"

        assert "-preset" in args
        p_idx = args.index("-preset")
        assert args[p_idx + 1] == "fast"

        assert "-profile:v" in args
        pv_idx = args.index("-profile:v")
        assert args[pv_idx + 1] == "main"

    def test_hwupload_filter(self, pipeline):
        """Should have format=nv12,hwupload filter for QSV upload."""
        args = pipeline._build_visualizer_ffmpeg_args()

        vf_idx = args.index("-vf")
        assert args[vf_idx + 1] == "format=nv12,hwupload=extra_hw_frames=64"

    def test_qsv_hw_device_init(self, pipeline):
        """Should initialize QSV hardware device."""
        args = pipeline._build_visualizer_ffmpeg_args()

        assert "-init_hw_device" in args
        hw_idx = args.index("-init_hw_device")
        assert args[hw_idx + 1] == "qsv=qsv:hw"

        assert "-filter_hw_device" in args
        fhw_idx = args.index("-filter_hw_device")
        assert args[fhw_idx + 1] == "qsv"

    def test_hls_output_format(self, pipeline):
        """HLS output settings: 2s segments, rolling window of 10."""
        args = pipeline._build_visualizer_ffmpeg_args()

        # Find the HLS format flag (second -f occurrence, after rawvideo)
        f_indices = [i for i, a in enumerate(args) if a == "-f"]
        assert len(f_indices) == 2
        assert args[f_indices[1] + 1] == "hls"

        # Segment duration
        ht_idx = args.index("-hls_time")
        assert args[ht_idx + 1] == "2"

        # Rolling window size
        hls_idx = args.index("-hls_list_size")
        assert args[hls_idx + 1] == "10"

    def test_hls_flags_delete_and_independent(self, pipeline):
        """HLS flags should include delete_segments+independent_segments."""
        args = pipeline._build_visualizer_ffmpeg_args()

        hf_idx = args.index("-hls_flags")
        assert args[hf_idx + 1] == "delete_segments+independent_segments"

    def test_output_path_uses_guild_id(self, pipeline):
        """Output path should be /tmp/hellodj_hls/{guild_id}/viz/playlist.m3u8."""
        args = pipeline._build_visualizer_ffmpeg_args()

        # Last arg is the output playlist path
        output_path = args[-1]
        assert "/tmp/hellodj_hls/123456/viz/playlist.m3u8" in output_path

    def test_segment_filename_pattern(self, pipeline):
        """Segment filename should be in the viz directory."""
        args = pipeline._build_visualizer_ffmpeg_args()

        seg_idx = args.index("-hls_segment_filename")
        seg_pattern = args[seg_idx + 1]
        assert "/tmp/hellodj_hls/123456/viz/seg%05d.ts" in seg_pattern

    def test_bitrate_settings(self, pipeline):
        """Should set 2500k bitrate with 3750k maxrate and 5000k bufsize."""
        args = pipeline._build_visualizer_ffmpeg_args()

        bv_idx = args.index("-b:v")
        assert args[bv_idx + 1] == "2500k"

        mr_idx = args.index("-maxrate")
        assert args[mr_idx + 1] == "3750k"

        bs_idx = args.index("-bufsize")
        assert args[bs_idx + 1] == "5000k"

    def test_gop_and_keyframes(self, pipeline):
        """Should set GOP size 60 and force keyframes every 2s."""
        args = pipeline._build_visualizer_ffmpeg_args()

        g_idx = args.index("-g")
        assert args[g_idx + 1] == "60"

        fk_idx = args.index("-force_key_frames")
        assert args[fk_idx + 1] == "expr:gte(t,n_forced*2)"

    def test_constant_output_framerate(self, pipeline):
        """Should set -r 30 for constant output framerate."""
        args = pipeline._build_visualizer_ffmpeg_args()

        r_idx = args.index("-r")
        assert args[r_idx + 1] == "30"

    def test_public_alias_works(self, pipeline):
        """The public alias build_visualizer_ffmpeg_args should produce same result."""
        private_args = pipeline._build_visualizer_ffmpeg_args()
        public_args = pipeline.build_visualizer_ffmpeg_args()
        assert private_args == public_args


class TestStdinPipeProperty:
    """Tests for the stdin_pipe property."""

    def test_stdin_pipe_none_when_no_process(self, pipeline):
        """stdin_pipe returns None when no process is running."""
        assert pipeline.stdin_pipe is None

    def test_stdin_pipe_none_when_process_has_no_stdin(self, pipeline):
        """stdin_pipe returns None when process exists but has no stdin."""
        from unittest.mock import MagicMock

        mock_process = MagicMock()
        mock_process.stdin = None
        pipeline.process = mock_process

        assert pipeline.stdin_pipe is None

    def test_stdin_pipe_returns_stdin_when_available(self, pipeline):
        """stdin_pipe returns the process stdin when available."""
        from unittest.mock import MagicMock

        mock_stdin = MagicMock()
        mock_process = MagicMock()
        mock_process.stdin = mock_stdin
        pipeline.process = mock_process

        assert pipeline.stdin_pipe is mock_stdin
