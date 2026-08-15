"""Text-to-speech using kokoro engine or speaches HTTP server, with PCM streaming to Discord voice.

Generates TTS audio, encodes to Opus frames, and sends via the base
``discord.VoiceClient.send_audio_packet(opus, encode=False)`` send path rather
than ``voice_client.play()`` on the wavelink/Lavalink player, which would
conflict with Lavalink-driven playback.

Supports pause-music → speak → resume pattern.
"""

import asyncio
import io
import logging
import os
import wave

import numpy as np

log = logging.getLogger(__name__)

# Kokoro generates audio at 24000 Hz (verified from hexgrad/Kokoro README).
KOKORO_SAMPLE_RATE = 24000
# Discord expects 48 kHz mono Opus frames of 20 ms (960 samples).
DISCORD_SAMPLE_RATE = 48000
DISCORD_FRAME_SAMPLES = 960


# ── kokoro TTS wrapper ───────────────────────────────────────────────────

class TTSEngine:
    """TTS engine wrapper. Uses kokoro if available, fallback to speaches."""

    def __init__(self, engine: str | None = None):
        self._engine = engine or os.getenv("TTS_ENGINE", "kokoro")
        self._model = None
        self._speaches_url = os.getenv("SPEACHES_URL", "")
        self._kokoro_url = os.getenv("KOKORO_URL", "")
        self._voice = os.getenv("TTS_VOICE", "af_heart")

    def _ensure_model(self):
        if self._model is not None:
            return

        if self._engine == "kokoro":
            if self._kokoro_url:
                # Remote kokoro: no in-process KPipeline is loaded. We lazily
                # call its REST API per synthesize, like speaches.
                self._model = {"url": self._kokoro_url, "voice": self._voice}
                log.info(
                    "kokoro TTS engine configured via remote URL (url=%s, voice=%s)",
                    self._kokoro_url, self._voice,
                )
            else:
                self._load_kokoro()
        elif self._engine == "speaches":
            self._load_speaches()
        else:
            log.error("Unknown TTS engine '%s' — TTS disabled", self._engine)
            self._model = None

    def _load_kokoro(self):
        try:
            from kokoro import KPipeline
            # 'a' => American English. Voice set via TTS_VOICE env (default af_heart).
            self._model = KPipeline(lang_code="a")
            log.info("kokoro TTS engine loaded (voice=%s)", self._voice)
        except ImportError:
            log.error(
                "kokoro not installed — TTS disabled. "
                "Add 'kokoro>=0.9.4' and 'misaki[en]' to requirements.txt"
            )
            self._model = None

    def _load_speaches(self):
        if not self._speaches_url:
            log.error(
                "speaches engine selected but SPEACHES_URL is not set — TTS disabled. "
                "Set SPEACHES_URL to a running speaches HTTP server."
            )
            self._model = None
            return
        # speaches is an HTTP server; we lazily call its REST API per synthesize.
        self._model = {"url": self._speaches_url, "voice": self._voice}
        log.info("speaches TTS engine configured (url=%s)", self._speaches_url)

    @property
    def available(self) -> bool:
        self._ensure_model()
        return self._model is not None

    def synthesize(self, text: str) -> tuple[np.ndarray, int] | None:
        """Generate PCM audio from text.

        Returns a tuple ``(pcm, sample_rate)`` where ``pcm`` is float32 mono
        audio, or None on failure. Sample rate is preserved so the caller can
        resample to 48 kHz for Discord if needed.
        """
        self._ensure_model()
        if self._model is None:
            return None

        try:
            if self._engine == "kokoro":
                if self._kokoro_url:
                    return self._synthesize_kokoro_remote(text)
                return self._synthesize_kokoro(text)
            elif self._engine == "speaches":
                return self._synthesize_speaches(text)
        except Exception as exc:
            log.warning("TTS synthesis failed: %s", exc)
            return None
        return None

    def _synthesize_kokoro(self, text: str) -> tuple[np.ndarray, int] | None:
        """kokoro KPipeline returns a generator of (graphemes, phonemes, audio).

        The audio samples are at KOKORO_SAMPLE_RATE (24000 Hz). We concatenate
        all chunks into a single float32 mono PCM array.
        """
        generator = self._model(text, voice=self._voice, speed=1.0)
        chunks = []
        for _gs, _ps, audio in generator:
            # audio is a torch tensor at 24000 Hz.
            chunks.append(audio.numpy())
        if not chunks:
            return None
        pcm = np.concatenate(chunks).astype(np.float32)
        return pcm, KOKORO_SAMPLE_RATE

    def _synthesize_kokoro_remote(self, text: str) -> tuple[np.ndarray, int] | None:
        """Remote kokoro via an OpenAI-compatible ``/v1/audio/speech`` endpoint.

        POST JSON body ``{"input": text, "voice": voice, "response_format": "wav"}``
        returns a WAV file, decoded via the stdlib ``wave`` module.
        Synchronous (urllib) — safe to call from the bot's async loop.
        """
        import json
        import urllib.request

        url = self._model["url"].rstrip("/") + "/v1/audio/speech"
        payload = {
            "input": text,
            "voice": self._model["voice"],
            "response_format": "wav",
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()

        pcm, sr = _decode_wav(data)
        return pcm, sr

    def _synthesize_speaches(self, text: str) -> tuple[np.ndarray, int] | None:
        """speaches exposes an OpenAI-compatible ``/v1/audio/speech`` endpoint.

        POST JSON body ``{"input": text, "voice": voice, "response_format": "wav"}``
        returns a WAV file. We decode it to PCM via the stdlib ``wave`` module.
        Synchronous (urllib) — safe to call from the bot's async loop.
        """
        import json
        import urllib.request

        url = self._model["url"].rstrip("/") + "/v1/audio/speech"
        payload = {
            "input": text,
            "voice": self._model["voice"],
            "response_format": "wav",
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()

        pcm, sr = _decode_wav(data)
        return pcm, sr

    async def stop(self) -> None:
        """No persistent resources to release."""
        return None


def _decode_wav(data: bytes) -> tuple[np.ndarray, int]:
    """Decode a WAV byte payload into (float32 mono PCM, sample_rate)."""
    with wave.open(io.BytesIO(data), "rb") as wav:
        sr = wav.getframerate()
        n_channels = wav.getnchannels()
        sampwidth = wav.getsampwidth()
        n_frames = wav.getnframes()
        raw = wav.readframes(n_frames)

    # Only handle 16-bit PCM; convert to float32 [-1, 1].
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)
    return samples, sr


# ── PCM audio source for discord.py ──────────────────────────────────────

class PCMAudioSource:
    """An in-memory source of 20 ms PCM frames at 48 kHz mono.

    Discord's voice send path (``discord.VoiceClient.play`` / the voice_recv
    ``AudioSource``) reads 20 ms frames via ``read()`` (960 samples) and encodes
    them as Opus. ``read()`` returns ``b""`` when finished so playback stops.
    """

    def __init__(self, pcm: np.ndarray, sample_rate: int = 16000):
        # Convert float32 → int16 and resample to 48 kHz for Discord
        if pcm.dtype == np.float32 or pcm.dtype == np.float64:
            pcm = (pcm * 32768).astype(np.int16)

        if sample_rate != DISCORD_SAMPLE_RATE:
            pcm = _resample(pcm, sample_rate, DISCORD_SAMPLE_RATE)

        self._pcm = pcm
        self._pos = 0
        self._frame_size = DISCORD_FRAME_SAMPLES  # 20 ms × 48 kHz mono

    def read(self) -> bytes:
        """Return the next 20 ms frame as bytes, or b"" when exhausted."""
        if self._pos >= len(self._pcm):
            return b""
        end = min(self._pos + self._frame_size, len(self._pcm))
        frame = self._pcm[self._pos : end]
        self._pos = end
        return frame.tobytes()

    def is_playing(self) -> bool:
        return self._pos < len(self._pcm)

    def frame_count(self) -> int:
        """Number of 20 ms frames remaining to send."""
        remaining = len(self._pcm) - self._pos
        return max(0, (remaining + self._frame_size - 1) // self._frame_size)


# ── Voice client PCM sender ──────────────────────────────────────────────

class TTSPLayer:
    """Sends TTS PCM audio to a Discord voice channel.

    Sends Opus frames directly via the base ``discord.VoiceClient``
    ``send_audio_packet(opus, encode=False)`` send path (the Opus data is
    already encoded) instead of ``voice_client.play()`` on the wavelink/Lavalink
    player, which would conflict with Lavalink playback.

    Handles pause-music → speak → resume.
    """

    def __init__(self, guild_id: int, voice_client):
        self.guild_id = guild_id
        self.voice_client = voice_client
        self._playing = False

    @property
    def is_playing(self) -> bool:
        return self._playing

    async def play_pcm(
        self,
        pcm: np.ndarray,
        sample_rate: int = 16000,
    ) -> None:
        """Send TTS PCM audio to the voice channel.

        Parameters
        ----------
        pcm : np.ndarray
            PCM audio (int16 or float32). Converted to int16 and resampled
            to 48 kHz for Discord.
        sample_rate : int
            Sample rate of the PCM. Upsampled to 48 kHz for Discord if needed.
        """
        if self.voice_client is None:
            log.warning("No voice client — cannot play TTS")
            return

        self._playing = True
        try:
            source = PCMAudioSource(pcm, sample_rate)
            encoder = _make_opus_encoder()

            # Send every 20 ms frame directly via the base VoiceClient
            # send_audio_packet path. The Opus frame is already encoded, so
            # encode=False avoids re-encoding raw PCM.
            while source.is_playing():
                frame_pcm = source.read()
                if not frame_pcm:
                    break
                # discord.py's Encoder is stereo-only; upmix mono → stereo
                frame_arr = np.frombuffer(frame_pcm, dtype=np.int16)
                stereo = np.empty(frame_arr.shape[0] * 2, dtype=np.int16)
                stereo[0::2] = frame_arr
                stereo[1::2] = frame_arr
                opus = encoder.encode(stereo.tobytes(), len(stereo) // 2)
                self.voice_client.send_audio_packet(opus, encode=False)
                # Wait one 20 ms frame interval so Discord keeps real-time pacing.
                await asyncio.sleep(0.02)

            log.info("TTS playback complete (guild=%s)", self.guild_id)
        except Exception as exc:
            # Log clearly and re-raise so the caller (_speak) knows TTS failed
            # and does not silently resume music as if nothing happened.
            log.error("TTS playback failed (guild=%s): %s", self.guild_id, exc)
            raise
        finally:
            self._playing = False

    async def stop(self) -> None:
        """Stop TTS playback."""
        self._playing = False
        if self.voice_client is not None:
            try:
                self.voice_client.stop_playing()
            except Exception:
                pass


def _make_opus_encoder():
    """Create a discord.py Opus encoder (48 kHz stereo, 20 ms frames).

    discord.py >= 2.7 has no `set_format`; the Encoder uses fixed Opus
    defaults (48 kHz stereo). We feed it stereo-interleaved PCM below.
    """
    import discord

    return discord.opus.Encoder()


def _resample(pcm: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Simple linear interpolation resampling.

    For production, use librosa or scipy.signal.resample.
    """
    if from_rate == to_rate:
        return pcm

    ratio = to_rate / from_rate
    n_orig = len(pcm)
    n_new = int(n_orig * ratio)
    resampled = np.zeros(n_new, dtype=np.int16)

    for i in range(n_new):
        pos = i / ratio
        idx = int(pos)
        frac = pos - idx
        if idx + 1 < n_orig:
            resampled[i] = int(pcm[idx] * (1 - frac) + pcm[idx + 1] * frac)
        else:
            resampled[i] = pcm[min(idx, n_orig - 1)]

    return resampled
