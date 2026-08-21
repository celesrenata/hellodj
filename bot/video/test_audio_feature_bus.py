"""Tests for AudioFeatureBus — subscriber-gated audio analysis pipeline."""

from __future__ import annotations

import asyncio
import struct
import time

import numpy as np
import pytest

from video.audio_feature_bus import (
    BAND_BIN_RANGES,
    BIN_RESOLUTION,
    FFT_SIZE,
    FRAME_SAMPLES,
    FREQUENCY_BANDS,
    SAMPLE_RATE,
    AudioFeatureBus,
)
from video.visualizer_engines.base import AudioFeatures


def _generate_sine_pcm(freq: float, num_samples: int = FRAME_SAMPLES) -> bytes:
    """Generate PCM bytes for a pure sine wave at the given frequency.

    Returns 16-bit signed LE mono PCM at 48kHz.
    """
    t = np.arange(num_samples) / SAMPLE_RATE
    samples = (np.sin(2 * np.pi * freq * t) * 30000).astype(np.int16)
    return samples.tobytes()


def _generate_silence(num_samples: int = FRAME_SAMPLES) -> bytes:
    """Generate silent PCM (all zeros)."""
    return b"\x00" * (num_samples * 2)


class TestSubscriberGating:
    """Verify zero processing with no subscribers, start/stop on first/last."""

    @pytest.mark.asyncio
    async def test_no_processing_without_subscribers(self):
        """Bus should not be processing when no subscribers exist."""
        bus = AudioFeatureBus(guild_id=1)
        assert bus.subscriber_count == 0
        assert not bus.is_processing

    @pytest.mark.asyncio
    async def test_starts_on_first_subscriber(self):
        """Processing starts when the first subscriber subscribes."""
        bus = AudioFeatureBus(guild_id=1)
        callback = lambda features: None

        await bus.subscribe(callback)
        assert bus.subscriber_count == 1
        assert bus.is_processing

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_stops_on_last_unsubscribe(self):
        """Processing stops when the last subscriber unsubscribes."""
        bus = AudioFeatureBus(guild_id=1)
        cb1 = lambda features: None
        cb2 = lambda features: None

        await bus.subscribe(cb1)
        await bus.subscribe(cb2)
        assert bus.subscriber_count == 2
        assert bus.is_processing

        await bus.unsubscribe(cb1)
        assert bus.subscriber_count == 1
        assert bus.is_processing  # Still running, one subscriber remains

        await bus.unsubscribe(cb2)
        # Give the task a moment to cancel
        await asyncio.sleep(0.05)
        assert bus.subscriber_count == 0
        assert not bus.is_processing

    @pytest.mark.asyncio
    async def test_multiple_subscribe_unsubscribe_cycles(self):
        """Bus can restart processing after full stop."""
        bus = AudioFeatureBus(guild_id=1)
        callback = lambda features: None

        # First cycle
        await bus.subscribe(callback)
        assert bus.is_processing
        await bus.unsubscribe(callback)
        await asyncio.sleep(0.05)
        assert not bus.is_processing

        # Second cycle
        await bus.subscribe(callback)
        assert bus.is_processing
        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_duplicate_subscribe_does_not_double_count(self):
        """Subscribing the same callback twice doesn't increase count."""
        bus = AudioFeatureBus(guild_id=1)
        callback = lambda features: None

        await bus.subscribe(callback)
        await bus.subscribe(callback)
        assert bus.subscriber_count == 1

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_unsubscribe_nonexistent_is_safe(self):
        """Unsubscribing a callback that was never subscribed is a no-op."""
        bus = AudioFeatureBus(guild_id=1)
        callback = lambda features: None

        # Should not raise
        await bus.unsubscribe(callback)
        assert bus.subscriber_count == 0


class TestFeatureComputation:
    """Verify FFT, band energy, beat detection, and BPM work correctly."""

    @pytest.mark.asyncio
    async def test_dispatches_features_from_pcm(self):
        """Bus dispatches AudioFeatures to subscribers when fed PCM data."""
        bus = AudioFeatureBus(guild_id=1)
        received: list[AudioFeatures] = []

        def on_features(f: AudioFeatures):
            received.append(f)

        await bus.subscribe(on_features)

        # Feed enough PCM for one frame
        pcm = _generate_sine_pcm(440.0)
        bus.feed_pcm(pcm)

        # Wait for processing
        await asyncio.sleep(0.2)

        assert len(received) >= 1
        features = received[0]
        assert len(features.fft) == FFT_SIZE // 2  # 512 bins
        assert len(features.band_energy) == 7
        assert isinstance(features.beat, bool)
        assert isinstance(features.bpm, float)
        assert features.timestamp > 0

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_fft_detects_sine_frequency(self):
        """FFT output should peak at the frequency of the input sine wave."""
        bus = AudioFeatureBus(guild_id=1)
        received: list[AudioFeatures] = []

        def on_features(f: AudioFeatures):
            received.append(f)

        await bus.subscribe(on_features)

        # 1000 Hz sine wave
        pcm = _generate_sine_pcm(1000.0)
        bus.feed_pcm(pcm)
        await asyncio.sleep(0.2)

        assert len(received) >= 1
        fft = np.array(received[0].fft)
        peak_bin = int(np.argmax(fft))
        peak_freq = peak_bin * BIN_RESOLUTION

        # Should be within one bin of 1000 Hz
        assert abs(peak_freq - 1000.0) < BIN_RESOLUTION * 1.5

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_band_energy_concentrates_in_correct_band(self):
        """A 100 Hz tone should produce energy primarily in the bass band."""
        bus = AudioFeatureBus(guild_id=1)
        received: list[AudioFeatures] = []

        def on_features(f: AudioFeatures):
            received.append(f)

        await bus.subscribe(on_features)

        # 100 Hz → bass band (60–250 Hz)
        pcm = _generate_sine_pcm(100.0)
        bus.feed_pcm(pcm)
        await asyncio.sleep(0.2)

        assert len(received) >= 1
        band_energy = received[0].band_energy

        # Bass (index 1) should have the highest energy
        bass_idx = 1  # bass band
        assert band_energy[bass_idx] == max(band_energy)

        await bus.shutdown()

    @pytest.mark.asyncio
    async def test_silence_produces_low_energy(self):
        """Silent PCM should produce near-zero energy across all bands."""
        bus = AudioFeatureBus(guild_id=1)
        received: list[AudioFeatures] = []

        def on_features(f: AudioFeatures):
            received.append(f)

        await bus.subscribe(on_features)

        pcm = _generate_silence()
        bus.feed_pcm(pcm)
        await asyncio.sleep(0.2)

        assert len(received) >= 1
        for energy in received[0].band_energy:
            assert energy < 0.01

        await bus.shutdown()


class TestFeedPcm:
    """Verify PCM feeding behavior."""

    @pytest.mark.asyncio
    async def test_feed_pcm_ignored_when_not_running(self):
        """feed_pcm does nothing when the bus is not processing."""
        bus = AudioFeatureBus(guild_id=1)
        pcm = _generate_sine_pcm(440.0)

        # Should not raise
        bus.feed_pcm(pcm)
        assert bus._pcm_queue.empty()

    @pytest.mark.asyncio
    async def test_feed_pcm_drops_when_queue_full(self):
        """feed_pcm drops data when the internal queue is full (backpressure)."""
        bus = AudioFeatureBus(guild_id=1)
        bus._running = True  # Simulate running state without task

        # Fill the queue
        pcm = _generate_sine_pcm(440.0)
        for _ in range(100):
            bus.feed_pcm(pcm)

        assert bus._pcm_queue.full()

        # This should not raise — just drops silently
        bus.feed_pcm(pcm)


class TestShutdown:
    """Verify clean shutdown behavior."""

    @pytest.mark.asyncio
    async def test_shutdown_clears_subscribers_and_stops(self):
        """shutdown() removes all subscribers and stops processing."""
        bus = AudioFeatureBus(guild_id=1)
        cb1 = lambda f: None
        cb2 = lambda f: None

        await bus.subscribe(cb1)
        await bus.subscribe(cb2)
        assert bus.subscriber_count == 2

        await bus.shutdown()
        assert bus.subscriber_count == 0
        assert not bus.is_processing

    @pytest.mark.asyncio
    async def test_shutdown_is_idempotent(self):
        """Calling shutdown multiple times is safe."""
        bus = AudioFeatureBus(guild_id=1)
        await bus.shutdown()
        await bus.shutdown()  # Should not raise


class TestFrequencyBandConfiguration:
    """Verify frequency band configuration is correct."""

    def test_seven_bands_defined(self):
        """Should have exactly 7 frequency bands."""
        assert len(FREQUENCY_BANDS) == 7
        assert len(BAND_BIN_RANGES) == 7

    def test_bands_cover_audible_range(self):
        """Bands should cover 20 Hz to 20 kHz."""
        assert FREQUENCY_BANDS[0][1] == 20.0  # Sub-bass starts at 20 Hz
        assert FREQUENCY_BANDS[-1][2] == 20000.0  # Brilliance ends at 20 kHz

    def test_bands_are_contiguous(self):
        """Each band should start where the previous one ended (no gaps)."""
        for i in range(1, len(FREQUENCY_BANDS)):
            prev_end = FREQUENCY_BANDS[i - 1][2]
            curr_start = FREQUENCY_BANDS[i][1]
            assert prev_end == curr_start, (
                f"Gap between {FREQUENCY_BANDS[i-1][0]} and {FREQUENCY_BANDS[i][0]}"
            )

    def test_band_names_match_spec(self):
        """Band names should match the specification."""
        expected_names = [
            "sub_bass", "bass", "low_mid", "mid",
            "upper_mid", "presence", "brilliance",
        ]
        actual_names = [name for name, _, _ in FREQUENCY_BANDS]
        assert actual_names == expected_names
