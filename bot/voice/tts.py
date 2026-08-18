"""Text-to-speech with kokoro (local/remote), speaches HTTP server, an
OpenAI-compatible endpoint, or AWS Polly, streaming PCM to Discord voice.

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
import time
import wave

import numpy as np

import metrics as _metrics

log = logging.getLogger(__name__)


def _record_tts(engine: str, chars: int) -> None:
    """Fire-and-forget record a TTS synthesis call onto the running event loop."""
    try:
        asyncio.create_task(_metrics.metrics.record_tts_call(engine, chars))
    except Exception as exc:
        log.warning("Could not schedule TTS metrics: %s", exc)

# Kokoro generates audio at 24000 Hz (verified from hexgrad/Kokoro README).
KOKORO_SAMPLE_RATE = 24000
# Discord expects 48 kHz mono Opus frames of 20 ms (960 samples).
DISCORD_SAMPLE_RATE = 48000
DISCORD_FRAME_SAMPLES = 960


# ── kokoro TTS wrapper ───────────────────────────────────────────────────

class TTSEngine:
    """TTS engine wrapper. Backends: kokoro | speaches | openai | bedrock (Polly).

    - kokoro:  local KPipeline or an OpenAI-compatible remote endpoint
    - speaches: OpenAI-compatible HTTP server (TTS_SPEACHES_ENDPOINT)
    - openai:   OpenAI-compatible /v1/audio/speech endpoint (TTS_KOKORO_ENDPOINT)
    - bedrock:  AWS Polly (boto3)
    """

    def __init__(self, engine: str | None = None):
        self._engine = engine or os.getenv("TTS_ENGINE", "kokoro")
        self._model = None
        # Backward compat: SPEACHES_URL / KOKORO_URL == *_ENDPOINT equivalents.
        self._speaches_url = os.getenv("SPEACHES_URL", "") or os.getenv(
            "TTS_SPEACHES_ENDPOINT", ""
        )
        self._kokoro_url = os.getenv("KOKORO_URL", "") or os.getenv(
            "TTS_KOKORO_ENDPOINT", ""
        )
        self._api_key = os.getenv("TTS_API_KEY", "")
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
        elif self._engine in ("openai", "bedrock"):
            if self._engine == "openai":
                if not self._kokoro_url:
                    log.error(
                        "openai TTS engine selected but TTS_KOKORO_ENDPOINT "
                        "(or KOKORO_URL) is not set — TTS disabled. Set "
                        "TTS_KOKORO_ENDPOINT to an OpenAI-compatible speech endpoint."
                    )
                    self._model = None
                    return
                self._model = {
                    "url": self._kokoro_url,
                    "voice": self._voice,
                    "api_key": self._api_key,
                }
                log.info(
                    "openai TTS engine configured via OpenAI-compatible endpoint "
                    "(url=%s, voice=%s)",
                    self._kokoro_url, self._voice,
                )
            else:
                # bedrock: AWS Polly backend.
                self._model = BedrockTTSEngine(
                    voice_id=os.getenv("POLLY_VOICE_ID", "Joanna"),
                    output_format=os.getenv("POLLY_OUTPUT_FORMAT", "mp3"),
                    region=os.getenv("AWS_REGION", ""),
                    role_arn=os.getenv("AWS_ROLE_ARN", ""),
                )
                log.info(
                    "bedrock TTS engine configured via Polly "
                    "(voice=%s, format=%s)",
                    os.getenv("POLLY_VOICE_ID", "Joanna"),
                    os.getenv("POLLY_OUTPUT_FORMAT", "mp3"),
                )
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
                    result = self._synthesize_kokoro_remote(text)
                else:
                    result = self._synthesize_kokoro(text)
            elif self._engine == "speaches":
                result = self._synthesize_speaches(text)
            elif self._engine == "openai":
                result = self._synthesize_openai(text)
            elif self._engine == "bedrock":
                result = self._synthesize_bedrock(text)
            else:
                result = None

            if result is not None:
                _record_tts(self._engine, len(text))
            return result
        except Exception as exc:
            log.warning("TTS synthesis failed: %s", exc)
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
        return self._synthesize_openai(text)

    def _synthesize_openai(self, text: str) -> tuple[np.ndarray, int] | None:
        """Synthesize via an OpenAI-compatible ``/v1/audio/speech`` endpoint.

        POST JSON body ``{"input": text, "voice": voice, "response_format": "wav"}``
        returns a WAV file, decoded via the stdlib ``wave`` module. Sends a
        Bearer token when TTS_API_KEY is set. Synchronous (urllib) — safe to
        call from the bot's async loop.
        """
        import json
        import urllib.request

        url = self._model["url"].rstrip("/") + "/v1/audio/speech"
        payload = {
            "input": text,
            "voice": self._model["voice"],
            "response_format": "wav",
        }
        headers = {"Content-Type": "application/json"}
        api_key = self._model.get("api_key") or self._api_key
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()

        pcm, sr = _decode_wav(data)
        log.info("TTS synthesized (openai, voice=%s): %s", self._model["voice"], text[:60])
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

    def _synthesize_bedrock(self, text: str) -> tuple[np.ndarray, int] | None:
        """Synthesize via AWS Polly (boto3).

        Returns float32 mono PCM and its sample rate. Polly can return MP3 or
        raw PCM; PCM (16000 Hz signed 16-bit mono) is used directly, MP3 is
        decoded to PCM via ffmpeg. Synchronous (boto3) — safe to call from the
        bot's async loop, consistent with the other HTTP backends.
        """
        if self._model is None:
            return None
        pcm, sr = self._model.synthesize(text)
        log.info(
            "TTS synthesized (bedrock/polly, voice=%s): %s",
            self._model.voice_id, text[:60],
        )
        return pcm, sr

    async def stop(self) -> None:
        """No persistent resources to release."""
        return None


def _decode_pcm_bytes(data: bytes, sample_rate: int) -> tuple[np.ndarray, int]:
    """Decode raw signed 16-bit mono PCM bytes into (float32 mono, sample_rate)."""
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, sample_rate


def _decode_mp3_bytes(data: bytes) -> tuple[np.ndarray, int] | None:
    """Decode MP3 bytes into (float32 mono PCM, sample_rate) via ffmpeg.

    Returns None when ffmpeg is unavailable or the decode fails. Mirrors the
    ``sounds`` ffmpeg decode pattern (mono s16le).
    """
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        log.warning("bedrock/polly: ffmpeg not available — cannot decode MP3 to PCM")
        return None

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "mp3", "-i", "pipe:0",
                "-f", "s16le", "-acodec", "pcm_s16le",
                "-ar", "24000", "-ac", "1",
                "-",
            ],
            input=data, capture_output=True, timeout=30, check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("bedrock/polly: ffmpeg decode failed: %s", exc)
        return None
    if result.returncode != 0 or not result.stdout:
        log.warning(
            "bedrock/polly: ffmpeg returned %d for MP3 decode: %s",
            result.returncode, result.stderr.decode("utf-8", "replace")[:200],
        )
        return None
    return _decode_pcm_bytes(result.stdout, 24000)


# ── AWS Polly TTS backend ────────────────────────────────────────────────

class BedrockTTSEngine:
    """Text-to-speech via AWS Polly (boto3).

    Credentials come from AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION
    env vars, or an IAM role via AWS_ROLE_ARN (STS assume-role), or the EC2/EKS
    instance-role credential chain when no env vars are set.

    Output format is POLLY_OUTPUT_FORMAT (mp3 | pcm). MP3 output is decoded to
    PCM via ffmpeg; raw PCM output (16000 Hz signed 16-bit mono) is used
    directly. The returned sample rate is preserved so the caller resamples to
    48 kHz for Discord.
    """

    def __init__(
        self,
        voice_id: str = "Joanna",
        output_format: str = "mp3",
        region: str | None = None,
        role_arn: str | None = None,
    ):
        self.voice_id = voice_id
        self.output_format = output_format
        self._region = region or os.getenv("AWS_REGION", "us-east-1")
        self._role_arn = role_arn or os.getenv("AWS_ROLE_ARN", "")
        self._session = None
        self._client = None

    def _ensure_session(self):
        if self._session is not None:
            return
        import boto3  # raises ImportError if boto3 is not installed

        if self._role_arn:
            sts = boto3.client("sts", region_name=self._region)
            creds = sts.assume_role(
                RoleArn=self._role_arn,
                RoleSessionName="hellodj-tts",
            ).get("Credentials")
            if not creds:
                raise RuntimeError("STS assume-role returned no credentials")
            self._session = boto3.Session(
                region_name=self._region,
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
            )
        else:
            self._session = boto3.Session(region_name=self._region)
        self._client = self._session.client("polly")

    def synthesize(self, text: str) -> tuple[np.ndarray, int] | None:
        """Call Polly synthesize_speech and decode the audio to PCM.

        Returns (pcm, sample_rate), or None on failure.
        """
        self._ensure_session()

        response = self._client.synthesize_speech(
            Engine="neural",
            OutputFormat=self.output_format,
            Text=text,
            TextType="text",
            VoiceId=self.voice_id,
        )
        stream = response.get("AudioStream")
        if stream is None:
            log.warning("bedrock/polly: synthesize_speech returned no audio stream")
            return None
        data = stream.read()

        if self.output_format == "pcm":
            # Polly raw PCM: signed 16-bit, mono, little-endian at 16000 Hz.
            return _decode_pcm_bytes(data, 16000)

        if self.output_format == "mp3":
            decoded = _decode_mp3_bytes(data)
            if decoded is None:
                return None
            return decoded

        log.warning(
            "bedrock/polly: unsupported POLLY_OUTPUT_FORMAT '%s' — use mp3 or pcm",
            self.output_format,
        )
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

        # Diagnosis: prove the TTS outbound path has a REAL Discord voice
        # connection. send_audio_packet reads self.sequence/timestamp/ssrc/
        # mode/secret_key off the VoiceConnectionState; a wavelink-only forward
        # (Lavalink PATCH, no socket) leaves _connection unconnected, so the
        # first send_audio_packet raises or silently no-ops.
        try:
            vc_type = type(self.voice_client).__name__
            try:
                is_conn = self.voice_client.is_connected()
            except Exception as e:
                is_conn = f"EXC {e!r}"
            conn = getattr(self.voice_client, "_connection", None)
            ssrc = None
            mode = None
            if conn is not None and not isinstance(conn, str):
                ssrc = getattr(conn, "ssrc", None)
                mode = getattr(conn, "mode", None)
            log.info(
                "TTS play diag (guild=%s) vc_type=%s is_connected=%s "
                "_connection=%s ssrc=%s mode=%s",
                self.guild_id, vc_type, is_conn,
                conn if conn is not None else "MISSING",
                ssrc, mode,
            )
        except Exception:
            log.exception("TTS play diag failed")

        self._playing = True
        try:
            source = PCMAudioSource(pcm, sample_rate)
            encoder = _make_opus_encoder()

            # Send every 20 ms frame directly via the base VoiceClient
            # send_audio_packet path. The Opus frame is already encoded, so
            # encode=False avoids re-encoding raw PCM.
            sent = 0
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
                sent += 1
                # Wait one 20 ms frame interval so Discord keeps real-time pacing.
                await asyncio.sleep(0.02)

            log.info("TTS playback complete (guild=%s, frames_sent=%d)", self.guild_id, sent)
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
