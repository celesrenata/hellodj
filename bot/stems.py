"""HelloDJ — Stem separation support (OPTIONAL, AI-model based).

This module is the *hook* for true audio stem separation — splitting a mixed
track into isolated vocals / drums / bass / melody stems.

REALITY CHECK (be honest with users)
------------------------------------
Lavalink's built-in filters do NOT support stem isolation. The only DSP
approximation Lavalink offers is the ``karaoke`` filter, which *attenuates the
vocal band* — it yields an *instrumental* version (vocals removed) but CANNOT
produce an isolated vocals / drums / bass / melody stem.

True stem separation requires a **source-separation model** (e.g. Demucs,
Spleeter, or a local ONNX/torch model). Such models are heavy (torch/onnx,
multi-GB) and are **NOT installed by default**. Installing them is optional and
documented separately (see ``requirements-stems.txt``).

Why this module returns ``None`` by default
-------------------------------------------
HelloDJ plays audio through Lavalink as a *streamed URL* — Lavalink resolves
and decodes the track server-side. This module cannot reach into that stream to
decode PCM, run a separation model, and re-stream the isolated stem without a
major architectural change (local PCM decode + model inference + a new audio
pipeline). That is explicitly out of scope. What this module provides today:

- ``stems_available()`` — detects whether a separation model is installed.
- ``isolate_stem()`` — the future call site. Returns ``None`` unless a model is
  actually available AND the audio can be reached; callers must fall back to
  the Lavalink karaoke approximation and clearly message the limitation.

Configuration
-------------
- ``STEM_MODEL`` (optional env var) — a model name/path for a separation model
  (e.g. ``demucs`` or a path to an ONNX model). When unset and no model import
  is present, separation is unavailable and only the karaoke fallback works.
"""

import logging
import os

log = logging.getLogger(__name__)

# Stem types we accept. Maps to a human-readable label for user messages.
STEM_TYPES = ("vocals", "drums", "bass", "melody")

STEM_LABELS = {
    "vocals": "vocals",
    "drums": "drums",
    "bass": "bass",
    "melody": "melody",
}

# ── model detection ────────────────────────────────────────────────────────
# Heavy source-separation backends we can optionally detect. We do NOT import
# them eagerly (that would pull torch/onnx into the process); we only probe
# whether they are importable. The actual model loading is left to the caller
# and documented as optional.

_SEPARATION_BACKENDS = (
    "demucs",        # Meta's Demucs — torch-based, heavy
    "spleeter",      # Deezer's Spleeter — tensorflow-based, heavy
    "onnxruntime",   # ONNX runtime — required for ONNX separation models
    "torch",         # PyTorch — required for demucs
)


def _model_configured() -> bool:
    """True when ``STEM_MODEL`` is set to a non-empty value."""
    return bool(os.getenv("STEM_MODEL", "").strip())


def _backend_importable(backend: str) -> bool:
    """Return True if the given backend module can be imported."""
    try:
        __import__(backend)
        return True
    except Exception:
        return False


def stems_available() -> bool:
    """Report whether true stem separation is available.

    True only when EITHER ``STEM_MODEL`` is configured OR a supported
    separation backend (demucs/spleeter/onnxruntime/torch) is importable.
    This is a *capability probe* — it does not guarantee a specific model is
    downloaded or that an audio file is reachable.
    """
    if _model_configured():
        return True
    return any(_backend_importable(b) for b in _SEPARATION_BACKENDS)


def stems_reason() -> str:
    """Human-readable explanation of why separation is or isn't available."""
    if _model_configured():
        return f"`STEM_MODEL` is configured (`{os.getenv('STEM_MODEL', '').strip()}`)."
    importable = [b for b in _SEPARATION_BACKENDS if _backend_importable(b)]
    if importable:
        return "A separation backend is importable: " + ", ".join(importable) + "."
    return (
        "No source-separation model is installed and `STEM_MODEL` is unset. "
        "True stem isolation requires an optional heavy AI model (demucs/"
        "spleeter/onnx) that is not installed by default."
    )


def validate_stem_type(stem_type: str) -> str | None:
    """Normalise a requested stem type; return None when invalid."""
    stem_type = (stem_type or "").strip().lower()
    if stem_type in STEM_TYPES:
        return stem_type
    return None


async def isolate_stem(audio_path: str, stem_type: str) -> str | None:
    """Isolate the requested stem from a local audio file.

    This is the *future call site* for real separation. Under the current
    streaming architecture it is NOT wired into playback (HelloDJ streams URLs
    through Lavalink and cannot inject a locally separated stem without a major
    pipeline change). It exists so a later AI integration has a defined seam.

    Returns the path to the isolated stem audio, or ``None`` when:
    - the stem type is invalid,
    - no model/backend is available, or
    - the model cannot be loaded / the file cannot be processed.

    Callers MUST fall back to the Lavalink karaoke approximation and tell the
    user exactly what they got when this returns ``None``.
    """
    stem_type = validate_stem_type(stem_type)
    if stem_type is None:
        log.warning("isolate_stem: invalid stem type %r", stem_type)
        return None

    if not stems_available():
        log.info(
            "isolate_stem: no separation model available "
            "(audio=%r stem=%r) — returning None for karaoke fallback",
            audio_path, stem_type,
        )
        return None

    # Model is nominally available, but we do NOT actually load it here.
    # Loading a heavy model + running inference is intentionally deferred to a
    # future AI integration (and is out of scope for this command). We log the
    # seam and return None so the caller always takes the documented fallback.
    log.warning(
        "isolate_stem: model backend present (stem=%r) but the streaming "
        "architecture cannot yet inject a separated stem into Lavalink "
        "playback. Falling back to the Lavalink approximation.",
        stem_type,
    )
    return None
