"""Speech-to-text using faster-whisper.

On wake word detection, we capture the speaker's audio until silence,
then transcribe with faster-whisper.
"""

import asyncio
import logging
import os
import time

import numpy as np

import metrics as _metrics
from debug import get_debug_logger

log = logging.getLogger(__name__)
dbg = get_debug_logger("stt")


def _record_stt(engine: str, duration_ms: float) -> None:
    """Fire-and-forget record an STT call onto the running event loop."""
    try:
        asyncio.create_task(_metrics.metrics.record_stt_call(engine, duration_ms))
    except Exception as exc:
        log.warning("Could not schedule STT metrics: %s", exc)

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
    """Speech-to-text using faster-whisper (local), an OpenAI-compatible remote
    endpoint, or AWS Bedrock/Amazon Transcribe.

    Engine selection via STT_ENGINE: local | openai | remote | bedrock.
    """

    def __init__(self, model_size: str = "base"):
        from config import cfg
        self._model_size = model_size or cfg("stt.model_size", "base")
        self._engine = cfg("stt.engine", "local")
        self._url = cfg("stt.url", "") or cfg("stt.whisper_endpoint", "")
        self._api_key = cfg("stt.api_key", "")
        self._model = None

    def _ensure_model(self):
        if self._model is not None:
            return

        if self._engine in ("remote", "openai"):
            if not self._url:
                log.error(
                    "STT engine '%s' selected but STT_URL / STT_WHISPER_ENDPOINT "
                    "is not set — STT disabled. Set STT_WHISPER_ENDPOINT to a "
                    "running OpenAI-compatible transcription server.",
                    self._engine,
                )
                self._model = None
                return
            # Remote mode: no in-process faster-whisper model is loaded.
            # Use a small marker so transcribe() branches to the remote call.
            self._model = {
                "url": self._url,
                "model": self._model_size,
                "api_key": self._api_key,
            }
            log.info(
                "%s STT engine configured (url=%s, model=%s)",
                self._engine, self._url, self._model_size,
            )
            return

        if self._engine == "bedrock":
            from config import cfg
            self._model = BedrockSTTEngine(
                region=cfg("aws.region", ""),
                role_arn=cfg("aws.role_arn", ""),
                bucket=cfg("bedrock.s3_bucket", ""),
            )
            log.info(
                "bedrock STT engine configured (region=%s, bucket=%s)",
                cfg("aws.region", "us-east-1"),
                cfg("bedrock.s3_bucket", ""),
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

        t0 = time.monotonic()
        try:
            if self._engine in ("remote", "openai"):
                text = self._transcribe_remote(pcm, sample_rate)
            elif self._engine == "bedrock":
                text = self._transcribe_bedrock(pcm, sample_rate)
            else:
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
        finally:
            duration_ms = (time.monotonic() - t0) * 1000.0
            _record_stt(self._engine, duration_ms)

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
        headers = {
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Content-Length": str(len(body)),
        }
        api_key = self._model.get("api_key") or self._api_key
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        req = urllib.request.Request(url, data=body, headers=headers)
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

    def _transcribe_bedrock(self, pcm: np.ndarray, sample_rate: int = 16000) -> str:
        """Transcribe via AWS Bedrock / Amazon Transcribe (file-based, boto3).

        The PCM is encoded as a mono WAV, uploaded to the configured S3 bucket,
        and transcribed by Amazon Transcribe. Returns the transcript text, or ""
        on failure.
        """
        if self._model is None:
            return ""
        engine = self._model  # BedrockSTTEngine instance
        try:
            text = engine.transcribe(pcm, sample_rate)
        except Exception as exc:
            log.warning("bedrock STT failed: %s", exc)
            return ""
        log.info(
            "STT result (bedrock, %.2fs audio): %s",
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


# ── AWS Bedrock / Amazon Transcribe STT backend ───────────────────────────

class BedrockSTTEngine:
    """Speech-to-text via Amazon Transcribe (file-based), using AWS boto3.

    Credentials come from AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION
    env vars, or an IAM role via AWS_ROLE_ARN (STS assume-role), or the EC2/EKS
    instance-role credential chain when no env vars are set. Audio is uploaded
    to an S3 bucket (BEDROCK_S3_BUCKET), transcribed, and the transcript text is
    returned. The S3 object is deleted after transcription.

    Synchronous (boto3) — safe to call from the bot's async loop for short
    clips, consistent with the existing remote (urllib) STT path.
    """

    def __init__(
        self,
        region: str | None = None,
        role_arn: str | None = None,
        bucket: str | None = None,
    ):
        from config import cfg
        self._region = region or cfg("aws.region", "us-east-1")
        self._role_arn = role_arn or cfg("aws.role_arn", "")
        self._bucket = bucket or cfg("bedrock.s3_bucket", "")
        self._language = cfg("stt.bedrock_language", "en-US")
        self._timeout = cfg.int("stt.bedrock_timeout", 60)
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
                RoleSessionName="hellodj-stt",
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
            # No role: rely on AWS env vars or the instance-role credential chain.
            self._session = boto3.Session(region_name=self._region)
        self._client = self._session.client("transcribe")

    def transcribe(self, pcm: np.ndarray, sample_rate: int = 16000) -> str:
        """Upload the PCM as WAV to S3, run a Transcribe job, return the text."""
        if not self._bucket:
            log.error(
                "bedrock STT selected but BEDROCK_S3_BUCKET is not set — STT disabled. "
                "Set BEDROCK_S3_BUCKET to the S3 bucket used for transcription."
            )
            return ""
        self._ensure_session()

        import io
        import uuid
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

        job_id = uuid.uuid4().hex
        key = f"hellodj-stt/{job_id}.wav"
        s3 = self._session.client("s3")
        try:
            s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=wav_bytes,
                ContentType="audio/wav",
            )
        except Exception as exc:
            log.warning("bedrock STT: S3 upload failed: %s", exc)
            return ""

        job_name = f"hellodj-{job_id}"
        try:
            self._client.start_transcription_job(
                TranscriptionJobName=job_name,
                LanguageCode=self._language,
                MediaFormat="wav",
                Media={"MediaFileUri": f"s3://{self._bucket}/{key}"},
            )
        except Exception as exc:
            log.warning("bedrock STT: start_transcription_job failed: %s", exc)
            self._cleanup(s3, key)
            return ""

        text = ""
        try:
            for _ in range(self._timeout):
                job = self._client.get_transcription_job(
                    TranscriptionJobName=job_name,
                )["TranscriptionJob"]
                status = job["TranscriptionJobStatus"]
                if status == "COMPLETED":
                    uri = job["Transcript"]["TranscriptFileUri"]
                    text = self._fetch_transcript(uri)
                    break
                if status == "FAILED":
                    log.warning(
                        "bedrock STT job %s FAILED: %s",
                        job_name, job.get("FailureReason", "unknown"),
                    )
                    break
                time.sleep(1)
        except Exception as exc:
            log.warning("bedrock STT: job polling failed: %s", exc)
        finally:
            self._cleanup(s3, key)
        return text

    def _fetch_transcript(self, uri: str) -> str:
        """Download and parse the Transcribe transcript JSON from its file URI."""
        import json
        import urllib.request

        with urllib.request.urlopen(uri, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # Amazon Transcribe transcript JSON: transcripts[0].transcript
        return data.get("transcripts", [{}])[0].get("transcript", "")

    def _cleanup(self, s3, key: str) -> None:
        """Best-effort delete of the uploaded S3 object."""
        try:
            s3.delete_object(Bucket=self._bucket, Key=key)
        except Exception:
            pass
