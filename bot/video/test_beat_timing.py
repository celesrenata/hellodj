"""Unit tests for beat_timing.py even-distribution and beat-snap algorithms.

Requirements: 2.1, 2.2, 2.3, 2.4, 9.1
"""

from __future__ import annotations

import pytest

from video.beat_timing import _snap_to_beats, compute_beat_timing
from video.lyrics_models import TimedLine


pytestmark = pytest.mark.asyncio(loop_scope="function")


class TestComputeBeatTiming:
    """Tests for compute_beat_timing even-distribution algorithm."""

    async def test_empty_text_returns_empty(self):
        """Empty text should return an empty list."""
        result = await compute_beat_timing("", 120.0)
        assert result == []

    async def test_whitespace_only_returns_empty(self):
        """Text with only whitespace/newlines should return empty list."""
        result = await compute_beat_timing("   \n\n  \n   ", 120.0)
        assert result == []

    async def test_single_line(self):
        """Single line should start at time 0."""
        result = await compute_beat_timing("Hello world", 60.0)
        assert len(result) == 1
        assert result[0].time_ms == 0
        assert result[0].text == "Hello world"
        assert result[0].words is None

    async def test_two_equal_lines(self):
        """Two lines of equal length should split duration evenly."""
        text = "AAAA\nBBBB"
        result = await compute_beat_timing(text, 10.0)
        assert len(result) == 2
        assert result[0].time_ms == 0
        assert result[1].time_ms == 5000  # halfway through 10s

    async def test_weighted_distribution(self):
        """Lines should be weighted by character count."""
        # "AA" = 2 chars, "AAAAAA" = 6 chars → total 8
        # weights: 2/8=0.25, 6/8=0.75
        # starts: 0, 0.25*10000=2500
        text = "AA\nAAAAAA"
        result = await compute_beat_timing(text, 10.0)
        assert len(result) == 2
        assert result[0].time_ms == 0
        assert result[1].time_ms == 2500

    async def test_three_lines_proportional(self):
        """Three lines distributed proportionally."""
        # 3 chars, 3 chars, 6 chars → total 12
        # weights: 3/12=0.25, 3/12=0.25, 6/12=0.5
        # starts: 0, 0.25*60000=15000, 0.5*60000=30000
        text = "abc\ndef\nabcdef"
        result = await compute_beat_timing(text, 60.0)
        assert len(result) == 3
        assert result[0].time_ms == 0
        assert result[1].time_ms == 15000
        assert result[2].time_ms == 30000

    async def test_filters_empty_lines(self):
        """Empty lines between content should be filtered out."""
        text = "Line one\n\n\nLine two\n\nLine three"
        result = await compute_beat_timing(text, 30.0)
        assert len(result) == 3
        assert result[0].text == "Line one"
        assert result[1].text == "Line two"
        assert result[2].text == "Line three"

    async def test_strips_whitespace_from_lines(self):
        """Leading/trailing whitespace should be stripped from lines."""
        text = "  Hello  \n  World  "
        result = await compute_beat_timing(text, 10.0)
        assert result[0].text == "Hello"
        assert result[1].text == "World"

    async def test_first_line_always_starts_at_zero(self):
        """The first line should always start at time_ms = 0."""
        text = "First\nSecond\nThird\nFourth\nFifth"
        result = await compute_beat_timing(text, 200.0)
        assert result[0].time_ms == 0

    async def test_all_times_within_duration(self):
        """All line start times should be within [0, duration_ms]."""
        text = "A\nB\nC\nD\nE\nF\nG\nH\nI\nJ"
        duration_s = 120.0
        duration_ms = int(duration_s * 1000)
        result = await compute_beat_timing(text, duration_s)
        for line in result:
            assert 0 <= line.time_ms <= duration_ms

    async def test_monotonically_non_decreasing(self):
        """Line start times should be non-decreasing."""
        text = "Short\nA much longer line here\nMedium line\nTiny\nAnother longer one"
        result = await compute_beat_timing(text, 180.0)
        for i in range(len(result) - 1):
            assert result[i].time_ms <= result[i + 1].time_ms

    async def test_words_always_none(self):
        """All TimedLine entries should have words=None (Phase 1)."""
        text = "Line one\nLine two\nLine three"
        result = await compute_beat_timing(text, 60.0)
        for line in result:
            assert line.words is None

    async def test_returns_timed_line_instances(self):
        """All results should be TimedLine instances."""
        text = "Hello\nWorld"
        result = await compute_beat_timing(text, 30.0)
        for line in result:
            assert isinstance(line, TimedLine)

    async def test_audio_bus_none_accepted(self):
        """audio_bus=None should work without error (Phase 1 ignores it)."""
        text = "Test line"
        result = await compute_beat_timing(text, 10.0, audio_bus=None)
        assert len(result) == 1

    async def test_zero_duration(self):
        """Zero duration should produce all lines at time_ms=0."""
        text = "Line one\nLine two\nLine three"
        result = await compute_beat_timing(text, 0.0)
        assert len(result) == 3
        for line in result:
            assert line.time_ms == 0

    async def test_very_short_duration(self):
        """Very short duration should still distribute correctly."""
        text = "A\nB"
        result = await compute_beat_timing(text, 0.001)  # 1ms total
        assert len(result) == 2
        assert result[0].time_ms == 0


class TestSnapToBeats:
    """Tests for _snap_to_beats binary search logic.

    Requirements: 2.2, 2.3
    """

    def test_empty_beats_returns_original(self):
        """With no beat timestamps, original times are preserved."""
        line_starts = [0, 5000, 10000]
        result = _snap_to_beats(line_starts, [], tolerance_ms=500)
        assert result == [0, 5000, 10000]

    def test_exact_beat_match(self):
        """Line start that exactly matches a beat stays at that time."""
        line_starts = [0, 5000, 10000]
        beats = [0, 5000, 10000, 15000]
        result = _snap_to_beats(line_starts, beats, tolerance_ms=500)
        assert result == [0, 5000, 10000]

    def test_snaps_to_nearest_beat_within_tolerance(self):
        """Line start within tolerance of a beat snaps to that beat."""
        line_starts = [4800, 10200]
        beats = [5000, 10000, 15000]
        result = _snap_to_beats(line_starts, beats, tolerance_ms=500)
        # 4800 is 200ms from 5000 → snaps to 5000
        # 10200 is 200ms from 10000 → snaps to 10000
        assert result == [5000, 10000]

    def test_keeps_original_when_outside_tolerance(self):
        """Line start beyond tolerance from any beat stays at original."""
        line_starts = [3000]
        beats = [1000, 5000]
        result = _snap_to_beats(line_starts, beats, tolerance_ms=500)
        # 3000 is 2000ms from 1000 and 2000ms from 5000 → both exceed 500ms
        assert result == [3000]

    def test_prefers_closer_beat(self):
        """When between two beats, snaps to the closer one."""
        line_starts = [4900]
        beats = [4000, 5000]
        result = _snap_to_beats(line_starts, beats, tolerance_ms=500)
        # 4900 is 100ms from 5000, 900ms from 4000 → snaps to 5000
        # (900ms > 500ms tolerance, so 4000 wouldn't qualify anyway)
        assert result == [5000]

    def test_tolerance_boundary_inclusive(self):
        """Line start exactly at tolerance boundary snaps to that beat."""
        line_starts = [5500]
        beats = [5000, 7000]
        result = _snap_to_beats(line_starts, beats, tolerance_ms=500)
        # 5500 is exactly 500ms from 5000 → should snap (<=)
        assert result == [5000]

    def test_tolerance_boundary_exclusive_beyond(self):
        """Line start just beyond tolerance boundary stays at original."""
        line_starts = [5501]
        beats = [5000, 7000]
        result = _snap_to_beats(line_starts, beats, tolerance_ms=500)
        # 5501 is 501ms from 5000 and 1499ms from 7000 → both exceed tolerance
        assert result == [5501]

    def test_single_beat_single_line(self):
        """Single beat with single line within tolerance."""
        line_starts = [950]
        beats = [1000]
        result = _snap_to_beats(line_starts, beats, tolerance_ms=500)
        assert result == [1000]

    def test_line_before_all_beats(self):
        """Line start before the first beat snaps if within tolerance."""
        line_starts = [200]
        beats = [500, 1000, 1500]
        result = _snap_to_beats(line_starts, beats, tolerance_ms=500)
        # 200 is 300ms from 500 → within tolerance → snaps
        assert result == [500]

    def test_line_after_all_beats(self):
        """Line start after the last beat snaps if within tolerance."""
        line_starts = [15300]
        beats = [5000, 10000, 15000]
        result = _snap_to_beats(line_starts, beats, tolerance_ms=500)
        # 15300 is 300ms from 15000 → within tolerance → snaps
        assert result == [15000]

    def test_custom_tolerance(self):
        """Custom tolerance value is respected."""
        line_starts = [5200]
        beats = [5000, 6000]
        # With 100ms tolerance: 5200 is 200ms from 5000 → outside
        result_tight = _snap_to_beats(line_starts, beats, tolerance_ms=100)
        assert result_tight == [5200]
        # With 300ms tolerance: 5200 is 200ms from 5000 → inside
        result_loose = _snap_to_beats(line_starts, beats, tolerance_ms=300)
        assert result_loose == [5000]

    def test_multiple_lines_independent_snapping(self):
        """Each line snaps independently."""
        line_starts = [0, 2400, 5100, 8000]
        beats = [0, 2500, 5000, 7500, 10000]
        result = _snap_to_beats(line_starts, beats, tolerance_ms=500)
        # 0 → 0 (exact)
        # 2400 → 2500 (100ms away)
        # 5100 → 5000 (100ms away)
        # 8000 → 7500 (500ms away, at boundary)
        assert result == [0, 2500, 5000, 7500]


class TestComputeBeatTimingWithAudioBus:
    """Tests for compute_beat_timing integration with AudioFeatureBus.

    Requirements: 2.2, 9.1
    """

    async def test_audio_bus_with_subscribers_snaps(self):
        """When audio_bus has subscribers and beats, lines are snapped."""

        class MockAudioBus:
            subscriber_count = 2
            beat_timestamps = [0, 2500, 5000, 7500, 10000]

        text = "AAAA\nBBBB"  # Equal weight → even split at 0, 5000
        result = await compute_beat_timing(text, 10.0, audio_bus=MockAudioBus())
        assert len(result) == 2
        assert result[0].time_ms == 0  # 0 → exact match at beat 0
        assert result[1].time_ms == 5000  # 5000 → exact match at beat 5000

    async def test_audio_bus_with_zero_subscribers_no_snap(self):
        """When audio_bus has zero subscribers, even distribution is used."""

        class MockAudioBus:
            subscriber_count = 0
            beat_timestamps = [0, 2500, 5000, 7500, 10000]

        text = "AAAA\nBBBB"
        result = await compute_beat_timing(text, 10.0, audio_bus=MockAudioBus())
        assert len(result) == 2
        assert result[0].time_ms == 0
        assert result[1].time_ms == 5000  # Even distribution (happens to match)

    async def test_audio_bus_with_get_beats_method(self):
        """AudioBus with async get_beats() method is supported."""

        class MockAudioBus:
            subscriber_count = 1

            async def get_beats(self):
                return [0, 3000, 6000, 9000]

        # "AA" (2 chars) + "AAAAAA" (6 chars) = 8 total
        # weights: 0.25, 0.75 → starts: 0, 2500
        text = "AA\nAAAAAA"
        result = await compute_beat_timing(text, 10.0, audio_bus=MockAudioBus())
        assert len(result) == 2
        assert result[0].time_ms == 0
        # 2500 is 500ms from 3000 → within tolerance → snaps to 3000
        assert result[1].time_ms == 3000

    async def test_audio_bus_no_beat_data_uses_even(self):
        """AudioBus with subscribers but no beat data uses even distribution."""

        class MockAudioBus:
            subscriber_count = 3
            beat_timestamps = []

        text = "AAAA\nBBBB"
        result = await compute_beat_timing(text, 10.0, audio_bus=MockAudioBus())
        assert len(result) == 2
        assert result[0].time_ms == 0
        assert result[1].time_ms == 5000

    async def test_audio_bus_exception_falls_back(self):
        """If AudioBus raises an exception, falls back to even distribution."""

        class MockAudioBus:
            subscriber_count = 2

            async def get_beats(self):
                raise RuntimeError("Bus error")

        text = "AAAA\nBBBB"
        result = await compute_beat_timing(text, 10.0, audio_bus=MockAudioBus())
        assert len(result) == 2
        assert result[0].time_ms == 0
        assert result[1].time_ms == 5000

    async def test_audio_bus_none_still_works(self):
        """audio_bus=None continues to work (existing fallback)."""
        text = "Hello\nWorld"
        result = await compute_beat_timing(text, 10.0, audio_bus=None)
        assert len(result) == 2
        assert result[0].time_ms == 0
