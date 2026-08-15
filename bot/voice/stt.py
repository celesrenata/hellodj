"""Speech-to-text using faster-whisper.

On wake word detection, we capture the speaker's audio until silence,
then transcribe with faster-whisper.
"""

import logging
import os

import numpy as np

log = logging.getLogger(__name__)

# ── silence detection ──────────────────────────────────────────────────────

SILENCE_THRESHOLD = 500       # RMS below this = silence
SILENCE_FRAMES_REQUIRED = 5   # consecutive silent 100ms chunks
FRAME_MS = 100                # 100ms analysis frames at 16 kHz
FRAME_SAMPLES = int(FRAME_MS * 16)  # 1600 samples at 16 kHz


def _rms(pcm: np.ndarray) -> float:
    """Root-mean-square energy of a PCM chunk."""
    return float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))


def detect_silence(pcm: np.ndarray, threshold: float = SILENCE_THRESHOLD) -> int | None:
    """Find the index (in samples) where silence begins.

    Returns the sample index at which silence starts, or None if no silence found.
    """
    n_frames = len(pcm) // FRAME_SAMPLES
    silent_count = 0
    for i in range(n_frames):
        chunk = pcm[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES]
        if _rms(chunk) < threshold:
            silent_count += 1
        else:
            silent_count = 0

        if silent_count >= SILENCE_FRAMES_REQUIRED:
            # Return the sample index at the start of this silence
            silence_start = (i - SILENCE_FRAMES_REQUIRED + 1) * FRAME_SAMPLES
            return max(silence_start, 0)

    return None


# ── Whisper transcription ─────────────────────────────────────────────────

class STTEngine:
    """Speech-to-text using faster-whisper."""

    def __init__(self, model_size: str = "base"):
        self._model_size = model_size or os.getenv("STT_MODEL_SIZE", "base")
        self._engine = os.getenv("STT_ENGINE", "local")
        self._url = os.getenv("STT_URL", "")
        self._model = None

    def _ensure_model(self):
        if self._model is not None:
            return

        if self._engine == "remote":
            if not self._url:
                log.error(
                    "STT engine 'remote' selected but STT_URL is not set — STT disabled. "
                    "Set STT_URL to a running remote STT server."
                )
                self._model = None
                return
            # Remote mode: no in-process faster-whisper model is loaded.
            # Use a small marker so transcribe() branches to the remote call.
            self._model = {"url": self._url, "model": self._model_size}
            log.info(
                "remote STT engine configured (url=%s, model=%s)",
                self._url, self._model_size,
            )
            return

        try:
            from faster_whisper import WhisperModel
        except ImportError:
            log.error(
                "faster-whisper not installed — STT disabled. "
                "Add 'faster-whisper>=1.0.0' to requirements.txt"
            )
            self._model = None
            return

        # Prefer CUDA; fall back to CPU
        device = "cuda"
        compute_type = "float16"
        try:
            import torch
            if not torch.cuda.is_available():
                device = "cpu"
                compute_type = "int8"
        except ImportError:
            device = "cpu"
            compute_type = "int8"

        log.info(
            "Loading faster-whisper %s (device=%s, compute=%s)...",
            self._model_size, device, compute_type,
        )
        self._model = WhisperModel(
            self._model_size,
            device=device,
            compute_type=compute_type,
        )

    def transcribe(self, pcm: np.ndarray, sample_rate: int = 16000) -> str:
        """Transcribe PCM audio to text.

        Parameters
        ----------
        pcm : np.ndarray
            PCM int16 audio at 16 kHz mono.
        sample_rate : int
            Sample rate (must match PCM).

        Returns
        -------
        str
            Transcribed text, or empty string on failure.
        """
        self._ensure_model()
        if self._model is None:
            return ""

        if self._engine == "remote":
            return self._transcribe_remote(pcm, sample_rate)

        # Normalize volume
        pcm_float = pcm.astype(np.float32) / 32768.0

        segments, info = self._model.transcribe(
            pcm_float,
            beam_size=3,
            no_speech_threshold=0.6,
            condition_on_previous_text=False,
        )

        text = " ".join(seg.text for seg in segments)
        log.info(
            "STT result (lang=%s, %.2fs audio): %s",
            info.language if info else "?",
            len(pcm) / sample_rate if len(pcm) > 0 else 0,
            text[:120],
        )
        return text

    def _transcribe_remote(self, pcm: np.ndarray, sample_rate: int = 16000) -> str:
        """Transcribe via a remote OpenAI-compatible ``/v1/audio/transcriptions`` endpoint.

        Sends the PCM as a WAV file in a ``multipart/form-data`` POST with
        ``file`` and ``model`` fields. Synchronous (urllib) — safe to call from
        the bot's async loop. On failure, logs and returns "".
        """
        import io
        import json
        import uuid
        import urllib.request
        import wave

        # Encode PCM int16 as a mono WAV in memory.
        pcm_int16 = pcm.astype(np.int16) if pcm.dtype != np.int16 else pcm
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(sample_rate)
            wav.writeframes(pcm_int16.tobytes())
        wav_bytes = buf.getvalue()

        boundary = uuid.uuid4().hex
        crlf = b"\r\n"
        body = (
            b"--" + boundary.encode() + crlf
            + b'Content-Disposition: form-data; name="file"; filename="audio.wav"' + crlf
            + b"Content-Type: audio/wav" + crlf + crlf
            + wav_bytes + crlf
            + b"--" + boundary.encode() + crlf
            + b'Content-Disposition: form-data; name="model"' + crlf + crlf
            + self._model["model"].encode("utf-8") + crlf
            + b"--" + boundary.encode() + b"--" + crlf
        )

        url = self._model["url"].rstrip("/") + "/v1/audio/transcriptions"
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "multipart/form-data; boundary=" + boundary,
                "Content-Length": str(len(body)),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
        except Exception as exc:
            log.warning("remote STT request failed: %s", exc)
            return ""

        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception as exc:
            log.warning("remote STT response parse failed: %s", exc)
            return ""

        text = payload.get("text", "")
        log.info(
            "STT result (remote, %.2fs audio): %s",
            len(pcm) / sample_rate if len(pcm) > 0 else 0,
            text[:120],
        )
        return text

    def transcribe_with_detection(
        self,
        pcm: np.ndarray,
        sample_rate: int = 16000,
    ) -> tuple[str, int | None]:
        """Transcribe audio with silence-based truncation.

        Returns (transcript, silence_sample_index).
        """
        silence_idx = detect_silence(pcm)
        if silence_idx is not None and silence_idx > 0:
            # Only transcribe up to the silence point
            pcm = pcm[:silence_idx]
            text = self.transcribe(pcm, sample_rate)
            return text, silence_idx
        else:
            text = self.transcribe(pcm, sample_rate)
            return text, None
