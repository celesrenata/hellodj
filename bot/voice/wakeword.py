"""Wake word detection using the custom Hello_DJ.onnx model.

Model input:  float32[1, 16, 96] — 16 time-steps × 96 mel bins, 80ms sliding window
Model output: float32[1, 1]     — sigmoid probability (≥0.5 = wake word detected)
"""

import logging
import os

import numpy as np
import onnxruntime as ort

log = logging.getLogger(__name__)

# Default threshold; can be raised at runtime to reduce false positives
_DEFAULT_THRESHOLD = 0.5


class WakeWordModel:
    """Lightweight ONNX wrapper for the custom Hello_DJ wake word model."""

    def __init__(self, model_path: str | None = None):
        model_path = model_path or os.getenv(
            "WAKE_WORD_MODEL_PATH",
            "/app/models/Hello_DJ.onnx",
        )
        if not os.path.exists(model_path):
            log.warning(
                "Wake word model not found at %s — voice activation will be disabled",
                model_path,
            )
            self._session = None
            return

        # Prefer CPU; GPU is overkill for a 9.5 MB ONNX running every 80ms
        providers = ["CPUExecutionProvider"]
        if "CUDAExecutionProvider" in ort.get_available_providers():
            providers.insert(0, "CUDAExecutionProvider")

        self._session = ort.InferenceSession(
            model_path,
            providers=providers,
        )
        self._input_name = self._session.get_inputs()[0].name
        self._output_name = self._session.get_outputs()[0].name
        self._input_shape = self._session.get_inputs()[0].shape  # [1, 16, 96]

        log.info(
            "Wake word model loaded (%s) — input %s, providers=%s",
            model_path,
            self._input_name,
            providers,
        )

    @property
    def available(self) -> bool:
        return self._session is not None

    def predict(self, mel: np.ndarray, threshold: float = _DEFAULT_THRESHOLD) -> bool:
        """Run inference on a single mel-spectrogram slice.

        Parameters
        ----------
        mel : np.ndarray
            Shape (1, 16, 96), float32. 16 time-steps × 96 mel bins.
        threshold : float
            Detection threshold (default 0.5). Raise to reduce false positives.

        Returns
        -------
        bool
            True if the wake word was detected (probability ≥ threshold).
        """
        if self._session is None:
            return False

        result = self._session.run(
            [self._output_name],
            {self._input_name: mel.astype(np.float32)},
        )
        prob = float(result[0][0][0])
        return prob >= threshold

    def predict_prob(self, mel: np.ndarray) -> float:
        """Return the raw sigmoid probability (for logging / diagnostics)."""
        if self._session is None:
            return 0.0
        result = self._session.run(
            [self._output_name],
            {self._input_name: mel.astype(np.float32)},
        )
        return float(result[0][0][0])
