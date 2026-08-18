"""HelloDJ — File upload handler for messages with audio/video attachments.

Detects whether an uploaded Discord attachment is a song (audio) or a video,
downloads it into ``data/uploads/``, extracts the audio track from video files
via ffmpeg, and plays the audio through the shared playback engine.

Local playback path
-------------------
wavelink 3.5.2 (verified against the installed wheel) exposes NO ``LocalTrack``,
``LocalPath`` or ``TrackSource.Local``, and the deployed Lavalink
(``lavalink/application.yml``) does not enable ``sources.local``. Local files
therefore cannot be pushed through the wavelink queue the way remote tracks are
(``_resolve_and_play`` resolves entries via ``Playable.search``, which cannot
resolve a local file). Instead, this module plays uploaded audio through the
same real path the rest of the bot uses for local PCM audio —
``sounds.decode_to_pcm`` (ffmpeg → 48 kHz mono int16) then
``voice.tts.TTSPLayer.play_pcm``, mirroring ``sounds.play_sound`` and
``voice/voice_commands.py:_speak`` (pause-music → play → resume).
"""

import asyncio
import logging
import os
import re
import shutil
import time

from pathlib import Path

import player
import sounds

log = logging.getLogger(__name__)

# ── upload directory & extension catalog ──────────────────────────────────

UPLOADS_DIR = "data/uploads"

AUDIO_EXTENSIONS = {
    "mp3", "ogg", "wav", "flac", "m4a", "opus", "aac", "wma",
}

VIDEO_EXTENSIONS = {
    "mp4", "mkv", "webm", "avi", "mov", "m4v",
}

IMAGE_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "svg",
}

# MIME prefixes that map to a media kind regardless of extension.
_MIME_AUDIO_PREFIXES = ("audio/",)
_MIME_VIDEO_PREFIXES = ("video/",)
_MIME_IMAGE_PREFIXES = ("image/",)


# ── type detection ────────────────────────────────────────────────────────

def detect_type(attachment) -> str:
    """Return ``'audio'``, ``'video'``, ``'image'`` or ``'unknown'`` for an attachment.

    Uses the MIME type from the attachment first (``attachment.content_type``),
    then falls back to the filename extension. A filename whose basename is
    sanitized/path-traversal-proofed before the extension is compared.
    """
    content_type = getattr(attachment, "content_type", None) or ""
    if content_type:
        ct = content_type.lower()
        if ct.startswith(_MIME_AUDIO_PREFIXES):
            return "audio"
        if ct.startswith(_MIME_VIDEO_PREFIXES):
            return "video"
        if ct.startswith(_MIME_IMAGE_PREFIXES):
            return "image"

    fname = os.path.basename(getattr(attachment, "filename", "") or "")
    ext = os.path.splitext(fname)[1].lstrip(".").lower()
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return "unknown"


def _sanitize_filename(filename: str) -> str:
    """Sanitize an uploaded filename: basename + safe charset (mirrors sounds.py)."""
    fname = os.path.basename(filename or "upload.bin")
    fname = re.sub(r"[^A-Za-z0-9._-]", "_", fname) or "upload.bin"
    return fname


# ── download ──────────────────────────────────────────────────────────────

async def download_attachment(attachment, dest_dir: str = UPLOADS_DIR) -> Path:
    """Download a Discord attachment to ``dest_dir`` and return its Path.

    Uses discord.py's native ``Attachment.save()`` (no aiohttp needed — the
    attachment already carries its CDN URL and the client session). Raises on
    download failure or an empty file.
    """
    os.makedirs(dest_dir, exist_ok=True)
    fname = _sanitize_filename(getattr(attachment, "filename", None) or "upload.bin")
    path = Path(dest_dir) / fname
    # Add a timestamp suffix to avoid clobbering same-named files and to give
    # cleanup_old_files an mtime signal that is distinct per upload.
    path = path.with_stem(f"{path.stem}-{int(time.time())}")

    try:
        await attachment.save(path)
    except Exception as exc:
        log.warning("file_handler: download failed for %s: %s", fname, exc)
        raise
    if not path.exists() or path.stat().st_size == 0:
        log.warning("file_handler: downloaded %s is empty/missing", path)
        raise RuntimeError(f"Downloaded file is empty: {fname}")

    log.info("file_handler: downloaded %s -> %s (%d bytes)",
             fname, path, path.stat().st_size)
    return path


# ── ffmpeg helpers ────────────────────────────────────────────────────────

def ffmpeg_available() -> bool:
    """Return True when the ffmpeg binary is on PATH (NixOS: /run/current-system/sw/bin)."""
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    """Return True when the ffprobe binary is on PATH."""
    return shutil.which("ffprobe") is not None


def extract_audio(video_path: Path, dest_dir: str = UPLOADS_DIR) -> Path | None:
    """Extract the audio track from a video file to a 48 kHz mono WAV.

    Runs ffmpeg ``-i input -vn -acodec pcm_s16le -ar 48000 -ac 1 output.wav``
    in a thread executor (mirrors ``sounds._ffmpeg_decode_blocking``). Returns
    the WAV Path on success, or None when ffmpeg is unavailable or extraction
    fails.
    """
    if not ffmpeg_available():
        log.warning("file_handler: ffmpeg not available - cannot extract audio from %s", video_path)
        return None

    os.makedirs(dest_dir, exist_ok=True)
    out_path = Path(dest_dir) / f"{video_path.stem}-audio.wav"
    loop = asyncio.get_running_loop()
    return loop.run_in_executor(
        None,
        lambda: _extract_audio_blocking(str(video_path), str(out_path)),
    )


def _extract_audio_blocking(video_path: str, out_path: str) -> Path | None:
    """Blocking ffmpeg extraction (runs in executor). Returns the WAV path or None."""
    import subprocess
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", video_path,
                "-vn", "-acodec", "pcm_s16le",
                "-ar", "48000", "-ac", "1",
                "-y", out_path,
            ],
            capture_output=True, timeout=120, check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("file_handler: ffmpeg extract failed for %s: %s", video_path, exc)
        return None
    if result.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        log.warning(
            "file_handler: ffmpeg returned %d for %s: %s",
            result.returncode, video_path,
            result.stderr.decode("utf-8", "replace")[:200],
        )
        return None
    log.info("file_handler: extracted audio %s -> %s", video_path, out_path)
    return Path(out_path)


# ── full upload pipeline ──────────────────────────────────────────────────

async def process_upload(attachment, player, channel) -> dict | None:
    """Full pipeline: detect type → download → (extract audio if video).

    Parameters
    ----------
    attachment : discord.Attachment
        The attachment to process.
    player : unused
        Kept for API symmetry with the plan; playback is performed separately
        by ``play_uploaded_file`` using the real shared player.
    channel : unused
        Kept for API symmetry; the text channel is supplied by the bot handler
        for the confirmation embed.

    Returns a playable-info dict::

        {
            "media_type": "audio" | "video",
            "playable_path": Path,
            "title": str,
            "size_bytes": int,
            "source": "upload",
        }

    Returns ``None`` when the attachment is an image (images posted in chat
    are silently ignored — the bot only reacts to directly-uploaded audio or
    video files). Raises ``ValueError`` for other unsupported/unknown file
    types, and re-raises download or ffmpeg failures.
    """
    kind = detect_type(attachment)
    if kind == "image":
        # Images posted in chat are silently ignored — the bot only reacts to
        # files that are directly uploaded to it (audio/video).
        fname = getattr(attachment, "filename", "unknown")
        log.info("file_handler: image attachment ignored (not playable): %s", fname)
        return None

    if kind == "unknown":
        fname = getattr(attachment, "filename", "unknown")
        log.warning("file_handler: unsupported attachment type ignored: %s", fname)
        raise ValueError(f"Unsupported file type: {fname}")

    path = await download_attachment(attachment)

    title = getattr(attachment, "filename", None) or path.name
    size = path.stat().st_size

    playable_path = path
    if kind == "video":
        extracted = await extract_audio(path)
        if extracted is None:
            raise RuntimeError("Could not extract audio track from video file.")
        playable_path = extracted
        log.info("file_handler: video %s -> playable audio %s", path, extracted)

    return {
        "media_type": kind,
        "playable_path": playable_path,
        "title": title,
        "size_bytes": size,
        "source": "upload",
    }


# ── local PCM playback through the shared engine ──────────────────────────

async def play_uploaded_file(guild_id: int, player_obj, path: Path, title: str) -> bool:
    """Play a local uploaded audio file through the connected voice client.

    Mirrors ``sounds.play_sound`` / ``voice_commands._speak``: decode to
    48 kHz mono int16 via ffmpeg, pause any Lavalink playback, send the Opus
    frames via ``TTSPLayer.play_pcm``, then resume. Returns True when audio
    was sent.
    """
    if player_obj is None:
        log.warning("file_handler: no player to play %s through", path)
        return False

    # Pause music (mirrors voice_commands._speak).
    was_playing = player_obj.playing and not player_obj.paused
    if was_playing:
        try:
            await player_obj.pause(True)
        except Exception:
            log.exception("file_handler: could not pause player guild=%s", guild_id)

    try:
        pcm = await sounds.decode_to_pcm(str(path), target_rate=48000)
        if pcm is None or len(pcm) == 0:
            log.warning("file_handler: could not decode %s for playback", path)
            return False

        from voice.tts import TTSPLayer
        layer = TTSPLayer(guild_id, player_obj)
        try:
            await layer.play_pcm(pcm, sample_rate=48000)
        finally:
            # Resume music (mirrors voice_commands._speak try/finally).
            if was_playing:
                try:
                    await player_obj.pause(False)
                except Exception:
                    log.exception("file_handler: could not resume player guild=%s", guild_id)
        return True
    except Exception as exc:
        log.error("file_handler: playback failed for %s: %s", path, exc)
        if was_playing:
            try:
                await player_obj.pause(False)
            except Exception:
                pass
        return False


# ── cleanup ───────────────────────────────────────────────────────────────

def cleanup_old_files(dest_dir: str = UPLOADS_DIR, max_age_hours: int = 24) -> int:
    """Delete uploaded files older than ``max_age_hours`` in ``dest_dir``.

    Returns the number of files removed. Best-effort: individual failures are
    logged and do not abort the sweep. Called on startup via bot.py.
    """
    if not os.path.isdir(dest_dir):
        log.info("file_handler: %s missing - nothing to clean up", dest_dir)
        return 0

    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    try:
        for fname in os.listdir(dest_dir):
            p = os.path.join(dest_dir, fname)
            if not os.path.isfile(p):
                continue
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    removed += 1
                    log.info("file_handler: removed stale upload %s", p)
            except OSError as exc:
                log.warning("file_handler: could not remove %s: %s", p, exc)
    except OSError as exc:
        log.warning("file_handler: could not list %s: %s", dest_dir, exc)

    if removed:
        log.info("file_handler: cleanup removed %d stale upload(s) from %s", removed, dest_dir)
    return removed
