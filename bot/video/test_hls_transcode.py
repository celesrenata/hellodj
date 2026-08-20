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
