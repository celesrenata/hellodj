"""Local ONNX wake word detection — the ONLY on-box AI in this component.

The custom Hello_DJ wake word model is a tiny CPU ONNX model run over a sliding
mel-spectrogram window:

    Input:  float32[1, 16, 96]  — 16 time-steps x 96 mel bins (1.28 s window)
    Output: float32[1, 1]       — sigmoid probability (>= threshold => detected)

``onnxruntime`` and ``numpy`` are imported lazily so this module compiles and
imports in environments where those wheels are not installed (lint/compile CI).
All STT/intent/TTS live elsewhere and are delegated to managed AWS AI — this
module performs no cloud calls and holds no credentials.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["WakeWordModel"]

_DEFAULT_THRESHOLD = 0.5
_DEFAULT_MODEL_PATH = "/app/models/Hello_DJ.onnx"


class WakeWordModel:
    """Lightweight ONNX wrapper for the custom Hello_DJ wake word model.

    The model is loaded lazily on first use so constructing this object never
    imports onnxruntime. When the model file is missing or the runtime is
    unavailable, the model degrades gracefully to "unavailable" and every
    prediction returns ``False`` — voice activation is simply disabled.
    """

    def __init__(
        self,
        model_path: str | None = None,
        *,
        threshold: float = _DEFAULT_THRESHOLD,
        session: Any | None = None,
    ) -> None:
        """Initialise the wake word model wrapper.

        Args:
            model_path: Path to the ONNX model file. Defaults to the
                ``WAKE_WORD_MODEL_PATH`` env var or the standard mount path.
            threshold: Detection threshold; probabilities >= this count as a
                detection. Raise to reduce false positives.
            session: Optional pre-built inference session (injectable for
                tests); when provided, no model file or onnxruntime is needed.
        """
        self._model_path = model_path or os.getenv("WAKE_WORD_MODEL_PATH", _DEFAULT_MODEL_PATH)
        self._threshold = threshold
        self._session = session
        self._input_name: str | None = None
        self._output_name: str | None = None
        self._loaded = session is not None
        if session is not None:
            self._resolve_io_names()

    def _resolve_io_names(self) -> None:
        """Cache the model's input/output tensor names from the session."""
        if self._session is None:
            return
        self._input_name = self._session.get_inputs()[0].name
        self._output_name = self._session.get_outputs()[0].name

    def _ensure_loaded(self) -> None:
        """Load the ONNX session on first use; tolerate a missing model."""
        if self._loaded:
            return
        self._loaded = True
        if not os.path.exists(self._model_path):
            log.warning(
                "Wake word model not found at %s — voice activation disabled",
                self._model_path,
            )
            return
        try:
            import onnxruntime as ort  # noqa: PLC0415 - intentional lazy import

            providers = ["CPUExecutionProvider"]
            self._session = ort.InferenceSession(self._model_path, providers=providers)
            self._resolve_io_names()
            log.info("Wake word model loaded (%s)", self._model_path)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            log.warning("Failed to load wake word model: %s", exc)
            self._session = None

    @property
    def available(self) -> bool:
        """True when the ONNX model is loaded and ready to run inference."""
        self._ensure_loaded()
        return self._session is not None

    def predict_prob(self, mel: Any) -> float:
        """Return the raw sigmoid probability for a mel window.

        Args:
            mel: A ``float32`` array shaped ``(1, 16, 96)``.

        Returns:
            The model's probability in ``[0, 1]``; ``0.0`` when unavailable.
        """
        self._ensure_loaded()
        if self._session is None:
            return 0.0
        import numpy as np  # noqa: PLC0415 - intentional lazy import

        result = self._session.run(
            [self._output_name],
            {self._input_name: np.asarray(mel, dtype=np.float32)},
        )
        return float(result[0][0][0])

    def predict(self, mel: Any, threshold: float | None = None) -> bool:
        """Run inference and return whether the wake word was detected.

        Args:
            mel: A ``float32`` array shaped ``(1, 16, 96)``.
            threshold: Optional per-call override of the detection threshold.

        Returns:
            ``True`` when the probability is >= the (effective) threshold.
        """
        effective = self._threshold if threshold is None else threshold
        return self.predict_prob(mel) >= effective
