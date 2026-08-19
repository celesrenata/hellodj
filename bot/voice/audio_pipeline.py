"""Audio pipeline: Opus capture, PCM decode, per-SSRC ring buffers, mel extraction.

Discord voice delivers 20ms Opus frames at 48 kHz. We:
1. Decode Opus → PCM (48 kHz, mono)
2. Downsample PCM to 16 kHz (wake word model expects 16 kHz)
3. Maintain per-user ring buffers of ~1.28 s (64 × 20 ms frames → 1280 samples @ 16 kHz)
4. Every 80 ms (4 frames), compute a 96-bin mel-spectrogram over the last 80 ms window
5. Concatenate 16 consecutive mel slices → (1, 16, 96) input for the wake word model
"""

import array
import logging
import struct

import numpy as np

from debug import get_debug_logger

log = logging.getLogger(__name__)
dbg = get_debug_logger("audio_pipeline")

# ── constants ──────────────────────────────────────────────────────────────

OPUS_FRAME_MS = 20                 # each Opus frame covers 20 ms
OPUS_SAMPLE_RATE = 48000           # Opus output sample rate
TARGET_SAMPLE_RATE = 16000         # wake word model sample rate
DECIMATION = OPUS_SAMPLE_RATE // TARGET_SAMPLE_RATE  # 3:1 downsampling

# 1.28 seconds of audio at 16 kHz
RING_BUFFER_SAMPLES = int(1.28 * TARGET_SAMPLE_RATE)          # 20480 → actually 1280 * 16 = 20480
# Wait — 16 time-steps × 80 ms = 1.28 s. At 16 kHz that's 20480 samples.
# But the model input is 16 × 96 mel features. Each mel covers 80 ms = 1280 samples.
# So the ring buffer needs 16 × 1280 = 20480 samples.
RING_BUFFER_SIZE = RING_BUFFER_SAMPLES  # 20480

# Mel-spectrogram parameters (matches openWakeWord training)
N_MELS = 96
N_FFT = 512
HOP_LENGTH = 320                     # 20 ms at 16 kHz (320 samples)
WIN_LENGTH = 640                     # 40 ms window

# We run inference every 80 ms = 4 Opus frames
INFERENCE_STEP = 4                   # number of Opus frames between inferences

# Number of consecutive mel slices needed for model input
MEL_HISTORY_LENGTH = 16


# ── SSRC → user mapping ───────────────────────────────────────────────────

class SSRCUserMap:
    """Maps Discord voice SSRC identifiers to user IDs.

    Discord sends a SpeakingStart event when a user begins speaking,
    which includes the SSRC. We cache the mapping here.
    """

    def __init__(self):
        self._ssrc_to_user: dict[int, int] = {}

    def register(self, ssrc: int, user_id: int) -> None:
        self._ssrc_to_user[ssrc] = user_id

    def unregister(self, ssrc: int) -> None:
        self._ssrc_to_user.pop(ssrc, None)

    def get_user(self, ssrc: int) -> int | None:
        return self._ssrc_to_user.get(ssrc)


# ── PCM ring buffer ───────────────────────────────────────────────────────

class PCMSource:
    """Per-SSRC ring buffer of decoded PCM samples at 16 kHz."""

    def __init__(self, ssrc: int):
        self.ssrc = ssrc
        # Ring buffer at 16 kHz mono, int16
        self.buffer = np.zeros(RING_BUFFER_SIZE, dtype=np.int16)
        self.write_pos = 0
        self.frame_count = 0  # total Opus frames received

    def append_pcm(self, pcm_48k: np.ndarray) -> None:
        """Append decoded PCM (48 kHz, mono, int16) after downsampling to 16 kHz."""
        # Downsample 3:1 — take every 3rd sample (simple decimation)
        pcm_16k = pcm_48k[::DECIMATION]  # 960 → 320 samples per 20ms frame

        n = len(pcm_16k)
        end = self.write_pos + n
        if end <= RING_BUFFER_SIZE:
            self.buffer[self.write_pos : end] = pcm_16k
        else:
            # Wrap around
            first = RING_BUFFER_SIZE - self.write_pos
            self.buffer[self.write_pos :] = pcm_16k[:first]
            self.buffer[: n - first] = pcm_16k[first:]

        self.write_pos = end % RING_BUFFER_SIZE
        self.frame_count += 1

    def get_latest(self, n_samples: int) -> np.ndarray:
        """Get the most recent ``n_samples`` from the ring buffer (no wrap)."""
        if self.frame_count == 0:
            return np.zeros(n_samples, dtype=np.int16)

        start = (self.write_pos - n_samples) % RING_BUFFER_SIZE
        if start + n_samples <= RING_BUFFER_SIZE:
            return self.buffer[start : start + n_samples].copy()
        else:
            # Wrapped — concatenate
            first = RING_BUFFER_SIZE - start
            return np.concatenate([
                self.buffer[start:],
                self.buffer[: n_samples - first],
            ])

    def get_continuous_since(self, frame_index: int) -> np.ndarray:
        """Get all PCM from a given frame index to present (for STT capture)."""
        current_frames = self.frame_count
        frames_to_get = current_frames - frame_index
        if frames_to_get <= 0:
            return np.array([], dtype=np.int16)

        samples_per_frame = 320  # 20 ms at 16 kHz
        n_samples = frames_to_get * samples_per_frame
        # Cap at ring buffer size
        n_samples = min(n_samples, RING_BUFFER_SIZE)
        return self.get_latest(n_samples)

    def reset(self) -> None:
        """Clear buffer and reset frame counter."""
        self.buffer.fill(0)
        self.write_pos = 0
        self.frame_count = 0


# ── Mel-spectrogram extraction ────────────────────────────────────────────

def compute_mel_slice(pcm_16k: np.ndarray) -> np.ndarray:
    """Compute 96 mel bins for an 80 ms (1280 samples) window at 16 kHz.

    Uses librosa's mel filterbank but with a simple STFT implementation
    to avoid the full librosa dependency. Returns shape (96,).
    """
    # Short-time Fourier Transform
    n_fft = N_FFT
    hop = HOP_LENGTH
    win = np.hanning(WIN_LENGTH)

    # Pad to at least window length
    if len(pcm_16k) < WIN_LENGTH:
        padded = np.pad(pcm_16k, (0, WIN_LENGTH - len(pcm_16k)), mode="constant")
    else:
        padded = pcm_16k[:WIN_LENGTH]

    # Apply window
    windowed = padded[:WIN_LENGTH] * win

    # FFT
    spectrum = np.fft.rfft(windowed, n=n_fft)
    power = np.abs(spectrum) ** 2

    # Mel filterbank (96 bins, 0–8000 Hz, since 16 kHz Nyquist)
    freqs = np.linspace(0, TARGET_SAMPLE_RATE // 2, n_fft // 2 + 1)
    mel = _mel_filterbank(freqs, n_mels=N_MELS)
    mel_spectrum = np.dot(power, mel.T)

    # Log scale
    mel_spectrum = np.maximum(mel_spectrum, 1e-10)
    mel_log = np.log(mel_spectrum)

    # Normalize to roughly unit variance (matching training normalization)
    mel_log = (mel_log - np.mean(mel_log)) / (np.std(mel_log) + 1e-6)

    return mel_log.astype(np.float32)


def _mel_filterbank(freqs: np.ndarray, n_mels: int = 96) -> np.ndarray:
    """Create a mel filterbank matrix.

    Returns shape (n_mels, n_freqs).
    """
    # Mel scale: convert Hz → mel
    def hz_to_mel(hz):
        return 2595 * np.log10(1 + hz / 700)

    def mel_to_hz(mel):
        return 700 * (10 ** (mel / 2595) - 1)

    low_mel = hz_to_mel(0)
    high_mel = hz_to_mel(TARGET_SAMPLE_RATE // 2)
    mel_points = np.linspace(low_mel, high_mel, n_mels + 2)
    hz_points = mel_to_hz(mel_points)

    n_freqs = len(freqs)
    bank = np.zeros((n_mels, n_freqs), dtype=np.float32)

    for i in range(n_mels):
        f_l = hz_points[i]
        f_c = hz_points[i + 1]
        f_h = hz_points[i + 2]

        for j in range(n_freqs):
            if freqs[j] >= f_l and freqs[j] <= f_c:
                bank[i, j] = (freqs[j] - f_l) / (f_c - f_l)
            elif freqs[j] >= f_c and freqs[j] <= f_h:
                bank[i, j] = (f_h - freqs[j]) / (f_h - f_c)

    return bank


# ── Opus decoder ──────────────────────────────────────────────────────────

class OpusDecoder:
    """Decodes Opus frames to PCM using discord.py's built-in Opus decoder."""

    def __init__(self):
        # discord.py provides `discord.opus.Decoder` for decode.
        # NOTE: discord.py >= 2.7 has no `set_format`; Decoder uses fixed Opus
        # defaults (48 kHz) and decodes to STEREO (2ch) interleaved PCM.
        import discord

        self._decoder = discord.opus.Decoder()

    def decode(self, opus_frame: bytes) -> np.ndarray:
        """Decode one Opus frame → PCM int16 MONO array (960 samples @ 48 kHz).

        discord.py's Decoder emits stereo-interleaved PCM (1920 samples/frame);
        Discord voice audio is mono, so we downmix L/R → mono.
        """
        pcm = self._decoder.decode(opus_frame)
        arr = np.frombuffer(pcm, dtype=np.int16)
        # interleaved [L0,R0,L1,R1,...] → average each L/R pair → mono
        mono = (arr[0::2].astype(np.int32) + arr[1::2].astype(np.int32)) // 2
        return mono.astype(np.int16)


# ── Audio Pipeline Orchestrator ───────────────────────────────────────────

class AudioPipeline:
    """Receives Opus frames, maintains per-SSRC buffers, and runs wake word inference.

    Usage::

        pipeline = AudioPipeline(wakeword_model)
        pipeline.on_voice_receive(ssrc, opus_data, user_id)
        # ... every 80ms, call pipeline.tick() to run wake word detection
        result = pipeline.tick()  # -> (ssrc, user_id) | None
    """

    def __init__(self, wakeword_model):
        self.wakeword = wakeword_model
        self.decoder = OpusDecoder()
        self.ssrc_map = SSRCUserMap()
        self._sources: dict[int, PCMSource] = {}
        self._mel_history: dict[int, list[np.ndarray]] = {}
        self._frame_counter: dict[int, int] = {}
        self._inference_counter: dict[int, int] = {}

    def on_speaking_start(self, ssrc: int, user_id: int) -> None:
        """Register a user when they start speaking."""
        self.ssrc_map.register(ssrc, user_id)
        if ssrc not in self._sources:
            self._sources[ssrc] = PCMSource(ssrc)
            self._mel_history[ssrc] = []
            self._frame_counter[ssrc] = 0
            self._inference_counter[ssrc] = 0

    def on_speaking_stop(self, ssrc: int) -> None:
        """Called when a user stops speaking (optional cleanup)."""
        self.ssrc_map.unregister(ssrc)
        self._sources.pop(ssrc, None)
        self._mel_history.pop(ssrc, None)
        self._frame_counter.pop(ssrc, None)
        self._inference_counter.pop(ssrc, None)

    def on_voice_receive(self, ssrc: int, opus_data: bytes, user_id: int | None = None) -> None:
        """Process an incoming Opus frame.

        Parameters
        ----------
        ssrc : int
            Discord voice SSRC (speaker identifier).
        opus_data : bytes
            Raw Opus frame (20ms).
        user_id : int, optional
            If known from SpeakingStart, pass it here.
        """
        # Ensure source exists
        if ssrc not in self._sources:
            self._sources[ssrc] = PCMSource(ssrc)
            self._mel_history[ssrc] = []
            self._frame_counter[ssrc] = 0
            self._inference_counter[ssrc] = 0

        if user_id is not None:
            self.ssrc_map.register(ssrc, user_id)

        # Decode Opus → PCM
        pcm = self.decoder.decode(opus_data)

        # Append to ring buffer
        self._sources[ssrc].append_pcm(pcm)

        # Update counters
        self._frame_counter[ssrc] += 1
        self._inference_counter[ssrc] = self._frame_counter[ssrc] // INFERENCE_STEP

    def tick(self) -> tuple[int, int] | None:
        """Run wake word detection for all active SSRCs.

        Called every 80ms (4 Opus frames). Returns (ssrc, user_id) if wake word detected.

        Returns None if no detection occurred.
        """
        if not self.wakeword.available:
            return None

        for ssrc, source in list(self._sources.items()):
            if source.frame_count < MEL_HISTORY_LENGTH * INFERENCE_STEP:
                continue  # Not enough audio yet

            # Compute mel slice for the latest 80ms window
            latest_80ms = source.get_latest(1280)  # 1280 samples @ 16 kHz
            mel_slice = compute_mel_slice(latest_80ms)

            # Store in mel history ring
            history = self._mel_history[ssrc]
            history.append(mel_slice)
            if len(history) > MEL_HISTORY_LENGTH:
                history.pop(0)

            # Run inference when we have 16 consecutive slices
            if len(history) >= MEL_HISTORY_LENGTH:
                # Stack: list[ndarray] → ndarray shape (16, 96) → reshape (1, 16, 96)
                mel_input = np.stack(history[-MEL_HISTORY_LENGTH:], axis=0)  # (16, 96)
                mel_input = mel_input[np.newaxis, :, :]  # (1, 16, 96)

                if self.wakeword.predict(mel_input):
                    user_id = self.ssrc_map.get_user(ssrc)
                    log.info(
                        "Wake word detected — ssrc=%s user_id=%s prob=%.3f",
                        ssrc, user_id,
                        self.wakeword.predict_prob(mel_input),
                    )
                    return (ssrc, user_id or 0)

        return None

    def capture_speech_since(self, ssrc: int, frame_index: int) -> np.ndarray:
        """Get PCM audio from a given frame index to present (for STT).

        Parameters
        ----------
        ssrc : int
            SSRC of the speaker.
        frame_index : int
            Opus frame index at which wake word was detected.

        Returns
        -------
        np.ndarray
            PCM int16 array at 16 kHz.
        """
        source = self._sources.get(ssrc)
        if source is None:
            return np.array([], dtype=np.int16)

        # Get the frame_index at wake word detection time
        # We want audio from that point forward
        wake_frame = frame_index
        return source.get_continuous_since(wake_frame)

    def reset_ssrc(self, ssrc: int) -> None:
        """Reset a source after wake word detection (prevent re-detection)."""
        source = self._sources.get(ssrc)
        if source is not None:
            source.reset()
        self._mel_history[ssrc] = []
        self._frame_counter[ssrc] = 0
        self._inference_counter[ssrc] = 0
