"""HelloDJ — Join/leave chime sound management.

Stores 808-style chime samples under ``data/sounds/`` and plays them through
the Discord voice connection (Opus → ``send_audio_packet``), the same path the
TTS pipeline uses (see ``voice/tts.py``).

Preset sounds are imported from samplefocus.com (the "Original 808 Cowbell"
sample and its close variants). The default preset is downloaded from the
samplefocus CDN at first use (or pre-seeded into ``data/sounds/``).

Guild-level configuration (which sound plays on join vs. leave) is persisted
via ``guild_settings`` so it survives restarts.

Shape of the per-guild setting (``guild_settings["chime"]``)::

    {
        "join":  "original-808-cowbell",   # preset key or "custom:<filename>"
        "leave": "original-808-cowbell",
        "volume": 100                       # 0-100, applied to the PCM
    }
"""

import asyncio
import logging
import os
import re
import shutil
import urllib.parse

import aiohttp
import numpy as np

import guild_settings

log = logging.getLogger(__name__)

SOUNDS_DIR = "data/sounds"

# ── Preset catalog ────────────────────────────────────────────────────────
# Each preset maps to a samplefocus.com sample page. The CDN mp3 URL is
# resolved lazily (see ``resolve_preset_url``) by scraping the sample page's
# embedded JSON (``"sample_mp3_url": "..."``), which is public and not
# Cloudflare-challenged. The default preset is pre-seeded into data/sounds/
# so the bot works even if samplefocus is unreachable.

PRESETS: dict[str, dict] = {
    "original-808-cowbell": {
        "name": "Original 808 Cowbell",
        "page": "https://samplefocus.com/samples/original-808-cowbell",
        "file": "original-808-cowbell.mp3",
        "source": "samplefocus.com",
    },
    "phonk-cowbell": {
        "name": "Phonk Cowbell",
        "page": "https://samplefocus.com/samples/phonk-cowbell",
        "file": "phonk-cowbell.mp3",
        "source": "samplefocus.com",
    },
    "dry-808-cowbell": {
        "name": "Dry 808 Cowbell",
        "page": "https://samplefocus.com/samples/dry-808-cowbell",
        "file": "dry-808-cowbell.mp3",
        "source": "samplefocus.com",
    },
    "high-808-cowbell": {
        "name": "High 808 Cowbell",
        "page": "https://samplefocus.com/samples/high-808-cowbell",
        "file": "high-808-cowbell.mp3",
        "source": "samplefocus.com",
    },
    "short-808-cowbell": {
        "name": "Short 808 Cowbell",
        "page": "https://samplefocus.com/samples/short-808-cowbell",
        "file": "short-808-cowbell.mp3",
        "source": "samplefocus.com",
    },
}

DEFAULT_PRESET = "original-808-cowbell"

# Browser-like headers for samplefocus (Cloudflare-fronted but the CDN + page
# HTML are served without a challenge for plain GETs).
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


# ── directory / file helpers ──────────────────────────────────────────────

def _ensure_dir() -> str:
    os.makedirs(SOUNDS_DIR, exist_ok=True)
    return SOUNDS_DIR


def sound_path(key: str) -> str:
    """Return the on-disk path for a preset key or ``custom:<filename>``.

    ``key`` may be a preset key (e.g. ``original-808-cowbell``) or a custom
    sound reference (``custom:my-sound.mp3``). The filename component is
    sanitized to prevent path traversal.
    """
    if key.startswith("custom:"):
        fname = key[len("custom:"):]
    else:
        preset = PRESETS.get(key)
        if preset is None:
            # Unknown key — treat the whole key as a filename (sanitized).
            fname = key
        else:
            fname = preset["file"]
    fname = os.path.basename(fname)  # strip any directory components
    fname = re.sub(r"[^A-Za-z0-9._-]", "_", fname) or "sound.mp3"
    return os.path.join(_ensure_dir(), fname)


def list_sounds() -> list[dict]:
    """List available sounds: presets (with on-disk status) + custom files."""
    out: list[dict] = []
    for key, preset in PRESETS.items():
        path = sound_path(key)
        out.append({
            "key": key,
            "name": preset["name"],
            "source": preset["source"],
            "on_disk": os.path.exists(path),
        })
    # Custom sounds already on disk (not part of the preset catalog).
    preset_files = {os.path.basename(p["file"]) for p in PRESETS.values()}
    try:
        for fname in sorted(os.listdir(SOUNDS_DIR)):
            if fname in preset_files or not os.path.isfile(os.path.join(SOUNDS_DIR, fname)):
                continue
            out.append({
                "key": f"custom:{fname}",
                "name": fname,
                "source": "custom",
                "on_disk": True,
            })
    except OSError:
        pass
    return out


# ── download / import ─────────────────────────────────────────────────────

async def _fetch(session: aiohttp.ClientSession, url: str, **kw) -> bytes:
    """GET ``url``; on 403 retry once with a browser-like User-Agent header.

    Many CDNs (e.g. CloudFront) reject aiohttp/urllib's default user-agent with
    HTTP 403. The retry sends ``_UA`` so the request looks like a real browser
    download instead of a bare Python client.
    """
    async with session.get(url, **kw) as resp:
        if resp.status == 200:
            return await resp.read()
        if resp.status == 403:
            # Retry once with a browser-like UA header (CloudFront commonly
            # 403s on the default aiohttp/urllib user-agent).
            retry_kw = dict(kw)
            headers = dict(retry_kw.pop("headers", {}) or {})
            headers["User-Agent"] = _UA
            async with session.get(url, headers=headers, **retry_kw) as retry:
                if retry.status == 200:
                    return await retry.read()
                raise RuntimeError(f"HTTP {retry.status} fetching {url}")
        raise RuntimeError(f"HTTP {resp.status} fetching {url}")


async def resolve_preset_url(preset_key: str) -> str | None:
    """Resolve a preset's CDN mp3 URL by scraping its samplefocus page.

    The sample page embeds a JSON blob containing ``"sample_mp3_url"``. Returns
    None when the page cannot be fetched or the URL is not present.
    """
    preset = PRESETS.get(preset_key)
    if preset is None:
        return None
    page_url = preset["page"]
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": _UA}) as session:
            html = await _fetch(session, page_url)
    except Exception as exc:
        log.warning("sounds: could not fetch sample page %s: %s", page_url, exc)
        return None
    m = re.search(rb'"sample_mp3_url"\s*:\s*"([^"]+)"', html)
    if not m:
        log.warning("sounds: no sample_mp3_url found on %s", page_url)
        return None
    return m.group(1).decode("utf-8", "replace")


async def ensure_preset(preset_key: str) -> str | None:
    """Ensure a preset sound file exists on disk; download it if missing.

    Returns the on-disk path on success, or None when the file could not be
    obtained (and is not already present).

    Graceful 403 handling: on any download failure (including CloudFront's
    HTTP 403 on the default user-agent) we do NOT crash the sound system.
    Instead we log a clear warning and fall back to the seeded default preset
    file when the requested one is missing, so the 808 effect still plays.
    """
    path = sound_path(preset_key)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path

    url = await resolve_preset_url(preset_key)
    if not url:
        # No resolvable URL — fall back to the seeded default preset.
        return _fallback_to_default(preset_key, "no resolvable CDN URL")

    try:
        async with aiohttp.ClientSession(headers={"User-Agent": _UA}) as session:
            data = await _fetch(session, url)
    except Exception as exc:
        log.warning("sounds: download failed for %s (%s): %s", preset_key, url, exc)
        # Download failure (e.g. 403) — fall back to the seeded default preset.
        return _fallback_to_default(preset_key, f"download failed ({exc})")

    if len(data) < 100:
        log.warning("sounds: downloaded file for %s is suspiciously small (%d bytes)", preset_key, len(data))
        return _fallback_to_default(preset_key, f"suspiciously small download ({len(data)} bytes)")

    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    log.info("sounds: downloaded preset %s -> %s (%d bytes)", preset_key, path, len(data))
    return path


def _fallback_to_default(preset_key: str, reason: str) -> str | None:
    """Fall back to the seeded default preset file when a preset cannot be fetched.

    Uses ``data/sounds/original-808-cowbell.mp3`` (the pre-seeded default) so the
    808 effect still plays even when the requested preset's CDN 403s. Returns the
    fallback path, or None if no fallback exists.
    """
    default_path = sound_path(DEFAULT_PRESET)
    if preset_key == DEFAULT_PRESET:
        # The default itself failed — nothing to fall back to.
        log.warning("sounds: default preset %s could not be obtained (%s)", DEFAULT_PRESET, reason)
        return None
    if os.path.exists(default_path) and os.path.getsize(default_path) > 0:
        log.warning(
            "sounds: falling back to seeded default preset %s for %s (%s)",
            DEFAULT_PRESET, preset_key, reason,
        )
        return default_path
    log.warning(
        "sounds: requested preset %s unavailable (%s) and no seeded default exists",
        preset_key, reason,
    )
    return None


async def import_sound(url: str, name: str | None = None) -> str:
    """Download an arbitrary audio URL into the sounds directory.

    Returns the on-disk path. Raises on failure.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Sound URL must be http(s).")
    default_name = os.path.basename(parsed.path) or "sound.mp3"
    fname = os.path.basename(name or default_name)
    fname = re.sub(r"[^A-Za-z0-9._-]", "_", fname) or "sound.mp3"
    path = os.path.join(_ensure_dir(), fname)

    headers = {"User-Agent": _UA, "Referer": url}
    async with aiohttp.ClientSession(headers=headers) as session:
        data = await _fetch(session, url)

    if len(data) < 100:
        raise RuntimeError("Downloaded sound is too small to be valid audio.")

    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    log.info("sounds: imported %s -> %s (%d bytes)", url, path, len(data))
    return path


# ── decode (ffmpeg → 48 kHz mono int16 PCM) ──────────────────────────────

def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


async def decode_to_pcm(path: str, target_rate: int = 48000) -> np.ndarray | None:
    """Decode an audio file to mono int16 PCM at ``target_rate`` via ffmpeg.

    Runs the blocking ffmpeg subprocess in a thread executor so the event
    loop stays responsive. Returns a numpy int16 array, or None when ffmpeg
    is unavailable or the decode fails.
    """
    if not _ffmpeg_available():
        log.warning("sounds: ffmpeg not available - cannot decode %s", path)
        return None
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: _ffmpeg_decode_blocking(path, target_rate),
    )


def _ffmpeg_decode_blocking(path: str, target_rate: int) -> np.ndarray | None:
    import subprocess
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", path,
                "-f", "s16le", "-acodec", "pcm_s16le",
                "-ar", str(target_rate), "-ac", "1",
                "-",
            ],
            capture_output=True, timeout=30, check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("sounds: ffmpeg decode failed for %s: %s", path, exc)
        return None
    if result.returncode != 0 or not result.stdout:
        log.warning("sounds: ffmpeg returned %d for %s: %s",
                    result.returncode, path, result.stderr.decode("utf-8", "replace")[:200])
        return None
    return np.frombuffer(result.stdout, dtype=np.int16)


# ── playback ──────────────────────────────────────────────────────────────

async def play_sound(voice_client, path: str, volume: int = 100) -> bool:
    """Play a local audio file through a Discord voice client.

    Decodes to 48 kHz mono PCM (ffmpeg), applies volume, and sends Opus frames
    via the base ``VoiceClient.send_audio_packet`` path (same as TTS). Returns
    True when audio was sent, False otherwise.
    """
    if voice_client is None:
        log.warning("sounds: no voice client to play through")
        return False

    pcm = await decode_to_pcm(path)
    if pcm is None or len(pcm) == 0:
        log.warning("sounds: could not decode %s for playback", path)
        return False

    # Apply volume (0-100).
    vol = max(0, min(100, int(volume))) / 100.0
    if vol < 1.0:
        pcm = (pcm.astype(np.float32) * vol).astype(np.int16)

    try:
        from voice.tts import TTSPLayer
    except Exception as exc:  # pragma: no cover - voice module optional
        log.warning("sounds: TTSPLayer unavailable: %s", exc)
        return False

    layer = TTSPLayer(0, voice_client)
    try:
        await layer.play_pcm(pcm, sample_rate=48000)
        return True
    except Exception as exc:
        log.warning("sounds: playback failed for %s: %s", path, exc)
        return False


# ── guild chime configuration ─────────────────────────────────────────────

def get_chime_config(guild_id: int) -> dict:
    """Return the guild's chime config (join/leave/volume) with defaults."""
    cfg = guild_settings.get_setting(guild_id, "chime") or {}
    return {
        "join": cfg.get("join", DEFAULT_PRESET),
        "leave": cfg.get("leave", DEFAULT_PRESET),
        "volume": int(cfg.get("volume", 100)),
    }


def set_chime_config(guild_id: int, *, join: str | None = None,
                     leave: str | None = None, volume: int | None = None) -> dict:
    """Update the guild's chime config and persist. Returns the new config."""
    current = get_chime_config(guild_id)
    if join is not None:
        current["join"] = join
    if leave is not None:
        current["leave"] = leave
    if volume is not None:
        current["volume"] = max(0, min(100, int(volume)))
    guild_settings.set_setting(guild_id, "chime", current)
    return current


async def play_chime(guild_id: int, kind: str, voice_client) -> bool:
    """Play the configured chime for ``kind`` ("join" or "leave").

    Ensures the sound file is present (downloading the preset if needed), then
    plays it through the voice client. Returns True when audio was sent.
    """
    cfg = get_chime_config(guild_id)
    key = cfg.get(kind, DEFAULT_PRESET)
    volume = cfg.get("volume", 100)

    path = sound_path(key)
    if not (os.path.exists(path) and os.path.getsize(path) > 0):
        # Try to (re)download the preset.
        if key in PRESETS:
            path = await ensure_preset(key) or path
        else:
            log.warning("sounds: chime %r not found on disk and not a preset", key)
            return False

    if not os.path.exists(path):
        log.warning("sounds: chime file missing for %r (guild=%s)", key, guild_id)
        return False

    return await play_sound(voice_client, path, volume=volume)
