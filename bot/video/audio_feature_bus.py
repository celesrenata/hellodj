"""AudioFeatureBus — Subscriber-gated audio analysis pipeline.

Performs FFT, beat detection, BPM estimation, and 7-band energy analysis on
PCM audio data received from Discord voice_recv. Zero processing when no
visualizer engines are subscribed.

Requirements: 4.1, 4.2, 4.3, 4.4, 4.5
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Callable

import numpy as np

from video.visualizer_engines.base import AudioFeatures

log = logging.getLogger(__name__)

# Audio constants
SAMPLE_RATE = 48_000  # Discord voice uses 48 kHz
FFT_SIZE = 1024  # 1024-sample FFT → 512 magnitude bins
FRAME_SAMPLES = FFT_SIZE  # Samples per analysis frame (~21.3ms at 48kHz)
FRAME_DURATION = FRAME_SAMPLES / SAMPLE_RATE  # ~0.0213s

# BPM estimation
BPM_UPDATE_INTERVAL = 2.0  # Update BPM estimate every 2 seconds
BPM_MIN = 60.0
BPM_MAX = 200.0
ONSET_HISTORY_SECONDS = 8.0  # Keep 8s of onset history for autocorrelation
ONSET_HISTORY_FRAMES = int(ONSET_HISTORY_SECONDS / FRAME_DURATION)

# Beat detection
BEAT_THRESHOLD_MULTIPLIER = 1.5  # Current energy must exceed average by this factor
SPECTRAL_FLUX_HISTORY = 43  # ~1 second of frames for running average

# 7-band frequency ranges (Hz) mapped to FFT bin indices at 48kHz with 1024-sample FFT
# Bin resolution = SAMPLE_RATE / FFT_SIZE = 46.875 Hz per bin
BIN_RESOLUTION = SAMPLE_RATE / FFT_SIZE

FREQUENCY_BANDS: list[tuple[str, float, float]] = [
    ("sub_bass", 20.0, 60.0),
    ("bass", 60.0, 250.0),
    ("low_mid", 250.0, 500.0),
    ("mid", 500.0, 2000.0),
    ("upper_mid", 2000.0, 4000.0),
    ("presence", 4000.0, 6000.0),
    ("brilliance", 6000.0, 20000.0),
]


def _freq_to_bin(freq: float) -> int:
    """Convert a frequency (Hz) to FFT bin index."""
    return int(round(freq / BIN_RESOLUTION))


# Precompute bin ranges for each band
BAND_BIN_RANGES: list[tuple[int, int]] = [
    (_freq_to_bin(low), _freq_to_bin(high)) for _, low, high in FREQUENCY_BANDS
]


class AudioFeatureBus:
    """Subscriber-gated audio analysis pipeline.

    Performs FFT, beat detection, and BPM estimation on PCM audio data.
    Zero processing when no subscribers are connected. Starts/stops
    within 100ms of subscriber changes.
    """

    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self._subscribers: set[Callable[[AudioFeatures], None]] = set()
        self._processing_task: asyncio.Task | None = None
        self._pcm_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self._lock = asyncio.Lock()
        self._running = False

        # Analysis state (reset on start)
        self._prev_spectrum: np.ndarray | None = None
        self._spectral_flux_history: deque[float] = deque(maxlen=SPECTRAL_FLUX_HISTORY)
        self._onset_history: deque[float] = deque(maxlen=ONSET_HISTORY_FRAMES)
        self._last_bpm_update: float = 0.0
        self._current_bpm: float = 120.0  # Default BPM until estimated
        self._pcm_buffer: bytearray = bytearray()

    @property
    def subscriber_count(self) -> int:
        """Number of currently subscribed consumers."""
        return len(self._subscribers)

    @property
    def is_processing(self) -> bool:
        """True if the analysis loop is currently running."""
        return self._processing_task is not None and not self._processing_task.done()

    async def subscribe(self, callback: Callable[[AudioFeatures], None]) -> None:
        """Add a subscriber. Starts processing if this is the first."""
        async with self._lock:
            self._subscribers.add(callback)
            if len(self._subscribers) == 1:
                await self._start_processing()

    async def unsubscribe(self, callback: Callable[[AudioFeatures], None]) -> None:
        """Remove a subscriber. Stops processing if this was the last."""
        async with self._lock:
            self._subscribers.discard(callback)
            if len(self._subscribers) == 0:
                await self._stop_processing()

    def feed_pcm(self, pcm_data: bytes) -> None:
        """Feed raw PCM data (16-bit signed LE, mono, 48kHz) into the bus.

        Called from voice_recv worker thread via run_coroutine_threadsafe or
        directly if already on the event loop. Non-blocking — drops data if
        the queue is full (backpressure).
        """
        if not self._running:
            return
        try:
            self._pcm_queue.put_nowait(pcm_data)
        except asyncio.QueueFull:
            pass  # Drop frames under backpressure — visualizer is non-critical

    async def _start_processing(self) -> None:
        """Begin audio analysis pipeline. Must complete within 100ms."""
        log.debug("AudioFeatureBus[guild=%d]: starting processing", self.guild_id)
        self._running = True
        self._reset_analysis_state()
        self._processing_task = asyncio.create_task(
            self._analysis_loop(), name=f"audio-feature-bus-{self.guild_id}"
        )

    async def _stop_processing(self) -> None:
        """Halt audio analysis pipeline. Must complete within 100ms."""
        log.debug("AudioFeatureBus[guild=%d]: stopping processing", self.guild_id)
        self._running = False
        if self._processing_task:
            self._processing_task.cancel()
            try:
                await self._processing_task
            except asyncio.CancelledError:
                pass
            self._processing_task = None
        # Drain the queue
        while not self._pcm_queue.empty():
            try:
                self._pcm_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._pcm_buffer.clear()

    def _reset_analysis_state(self) -> None:
        """Reset all analysis state for a fresh start."""
        self._prev_spectrum = None
        self._spectral_flux_history.clear()
        self._onset_history.clear()
        self._last_bpm_update = 0.0
        self._current_bpm = 120.0
        self._pcm_buffer.clear()

    async def _analysis_loop(self) -> None:
        """Main loop: read PCM → compute features → dispatch to subscribers."""
        bytes_per_frame = FRAME_SAMPLES * 2  # 16-bit (2 bytes per sample)

        try:
            while self._running:
                # Wait for PCM data
                try:
                    pcm_data = await asyncio.wait_for(
                        self._pcm_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                self._pcm_buffer.extend(pcm_data)

                # Process all complete frames in the buffer
                while len(self._pcm_buffer) >= bytes_per_frame:
                    frame_bytes = bytes(self._pcm_buffer[:bytes_per_frame])
                    del self._pcm_buffer[:bytes_per_frame]

                    features = self._compute_features(frame_bytes)
                    if features is not None:
                        self._dispatch(features)

                    # Yield to event loop periodically
                    await asyncio.sleep(0)

        except asyncio.CancelledError:
            log.debug("AudioFeatureBus[guild=%d]: analysis loop cancelled", self.guild_id)
            raise

    def _compute_features(self, frame_bytes: bytes) -> AudioFeatures | None:
        """Compute audio features from a single frame of PCM data.

        Args:
            frame_bytes: 1024 samples of 16-bit signed LE mono PCM (2048 bytes).

        Returns:
            AudioFeatures dataclass or None if computation fails.
        """
        try:
            # Convert bytes to float samples normalised to [-1.0, 1.0]
            samples = np.frombuffer(frame_bytes, dtype=np.int16).astype(np.float64)
            samples /= 32768.0

            # Apply Hanning window to reduce spectral leakage
            window = np.hanning(FFT_SIZE)
            windowed = samples * window

            # Compute FFT — take first half (positive frequencies)
            spectrum = np.abs(np.fft.rfft(windowed))[:FFT_SIZE // 2]

            # Compute 7-band energy
            band_energy = self._compute_band_energy(spectrum)

            # Beat detection via spectral flux
            beat = self._detect_beat(spectrum)

            # Update BPM estimation
            now = time.monotonic()
            if now - self._last_bpm_update >= BPM_UPDATE_INTERVAL:
                self._estimate_bpm()
                self._last_bpm_update = now

            # Store spectrum for next frame's spectral flux
            self._prev_spectrum = spectrum

            return AudioFeatures(
                fft=spectrum.tolist(),
                beat=beat,
                bpm=self._current_bpm,
                band_energy=band_energy,
                timestamp=now,
            )

        except Exception:
            log.exception(
                "AudioFeatureBus[guild=%d]: error computing features", self.guild_id
            )
            return None

    def _compute_band_energy(self, spectrum: np.ndarray) -> list[float]:
        """Compute energy in each of the 7 frequency bands.

        Sums the squared magnitude of FFT bins within each band's frequency
        range, then takes the square root for a perceptually-useful energy value.
        """
        energies: list[float] = []
        for bin_low, bin_high in BAND_BIN_RANGES:
            # Clamp to valid bin range
            bin_low = max(0, bin_low)
            bin_high = min(len(spectrum), bin_high)
            if bin_low >= bin_high:
                energies.append(0.0)
            else:
                band_slice = spectrum[bin_low:bin_high]
                energy = float(np.sqrt(np.sum(band_slice ** 2) / max(1, len(band_slice))))
                energies.append(energy)
        return energies

    def _detect_beat(self, spectrum: np.ndarray) -> bool:
        """Detect beat via spectral flux (onset detection).

        Spectral flux = sum of positive differences between current and
        previous spectrum. A beat is detected when the current flux exceeds
        the running average by a threshold multiplier.
        """
        if self._prev_spectrum is None:
            self._onset_history.append(0.0)
            return False

        # Compute spectral flux (half-wave rectified difference)
        diff = spectrum - self._prev_spectrum
        flux = float(np.sum(np.maximum(diff, 0.0)))

        self._spectral_flux_history.append(flux)
        self._onset_history.append(flux)

        if len(self._spectral_flux_history) < 2:
            return False

        # Beat detected if flux exceeds running average by threshold
        avg_flux = sum(self._spectral_flux_history) / len(self._spectral_flux_history)
        is_beat = flux > avg_flux * BEAT_THRESHOLD_MULTIPLIER and avg_flux > 0

        return is_beat

    def _estimate_bpm(self) -> None:
        """Estimate BPM via autocorrelation of the onset function.

        Uses the onset (spectral flux) history to find the dominant periodicity,
        which corresponds to the tempo.
        """
        if len(self._onset_history) < 50:
            return  # Not enough data for meaningful estimation

        onset_signal = np.array(self._onset_history, dtype=np.float64)

        # Normalise
        onset_signal -= onset_signal.mean()
        std = onset_signal.std()
        if std < 1e-10:
            return  # Silent or constant — no tempo detectable
        onset_signal /= std

        # Autocorrelation via FFT (faster than direct computation)
        n = len(onset_signal)
        fft_size = 1
        while fft_size < 2 * n:
            fft_size *= 2
        fft_signal = np.fft.rfft(onset_signal, n=fft_size)
        autocorr = np.fft.irfft(fft_signal * np.conj(fft_signal))[:n]

        # Convert BPM range to lag range (in frames)
        # lag = frames_per_beat = (60 / BPM) / FRAME_DURATION
        min_lag = int((60.0 / BPM_MAX) / FRAME_DURATION)
        max_lag = int((60.0 / BPM_MIN) / FRAME_DURATION)
        max_lag = min(max_lag, n - 1)

        if min_lag >= max_lag or min_lag >= n:
            return

        # Find peak in autocorrelation within BPM range
        search_region = autocorr[min_lag:max_lag + 1]
        if len(search_region) == 0:
            return

        peak_idx = int(np.argmax(search_region)) + min_lag

        if peak_idx <= 0:
            return

        # Convert lag (frames) to BPM
        beat_period_seconds = peak_idx * FRAME_DURATION
        bpm = 60.0 / beat_period_seconds

        # Clamp to valid range
        if BPM_MIN <= bpm <= BPM_MAX:
            # Smooth with previous estimate (exponential moving average)
            self._current_bpm = 0.7 * self._current_bpm + 0.3 * bpm

    def _dispatch(self, features: AudioFeatures) -> None:
        """Send computed features to all subscribers."""
        for callback in list(self._subscribers):
            try:
                callback(features)
            except Exception:
                log.exception(
                    "AudioFeatureBus[guild=%d]: subscriber callback error",
                    self.guild_id,
                )

    async def shutdown(self) -> None:
        """Full shutdown — stop processing and clear all subscribers."""
        async with self._lock:
            self._subscribers.clear()
            await self._stop_processing()
