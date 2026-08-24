"""projectM Engine — libprojectM 4.x binding for Milkdrop preset rendering.

Wraps the projectM C API via ctypes to render preset-driven audio
visualizations into an EGL headless FBO. Feeds FFT audio data per-frame
and manages preset lifecycle (shuffle, categories, track-change blend).

Requirements: Req 5 (AC 1-6), Req 17 (AC 1-3)
"""

from __future__ import annotations

import ctypes
import logging
from pathlib import Path

from .base import AudioFeatures, TrackMetadata
from .gpu_engine_base import GPUEngineBase

log = logging.getLogger(__name__)

# Default preset directory (bundled in Docker image)
PRESET_DIR = "/app/data/presets/projectm"

# Blend duration for track changes (Req 5 AC 4)
BLEND_DURATION = 3.0

# Default projectM configuration
DEFAULT_PRESET_DURATION = 30.0
DEFAULT_BRIGHTNESS = 1.0
DEFAULT_SENSITIVITY = 1.0

# Resolution
WIDTH = 1280
HEIGHT = 720


class ProjectMEngine(GPUEngineBase):
    """Milkdrop-compatible audio visualization via libprojectM.

    Binds to libprojectM 4.x C API for preset rendering into our EGL FBO.
    The library manages its own OpenGL state within the active GL context
    we provide.

    Configurable parameters (via config schema):
        - preset_category: Folder name or "all" for all categories
        - blend_duration: Blend time between presets (1-10s)
        - preset_duration: Time before auto-switching preset (10-300s)
        - brightness: Output brightness multiplier (0.5-2.0)
        - sensitivity: Beat detection sensitivity (0.5-2.0)
    """

    def __init__(self, **kwargs) -> None:
        super().__init__()
        self._pm_handle: ctypes.c_void_p | None = None
        self._lib: ctypes.CDLL | None = None
        self._metadata: TrackMetadata | None = None

        # Configurable parameters (defaults from config_schema)
        self._preset_category: str = kwargs.get("preset_category", "all")
        self._blend_duration: float = kwargs.get("blend_duration", BLEND_DURATION)
        self._preset_duration: float = kwargs.get("preset_duration", DEFAULT_PRESET_DURATION)
        self._brightness: float = kwargs.get("brightness", DEFAULT_BRIGHTNESS)
        self._sensitivity: float = kwargs.get("sensitivity", DEFAULT_SENSITIVITY)

        # Audio buffer for PCM feed
        self._pcm_buffer = (ctypes.c_float * 512)()

    # ------------------------------------------------------------------
    # GPUEngineBase hooks
    # ------------------------------------------------------------------

    async def _on_gl_ready(self, metadata: TrackMetadata | None) -> None:
        """Load libprojectM, create instance, configure presets and rendering.

        Called after EGL context is created and made current.
        """
        if metadata is not None:
            self._metadata = metadata
        self._load_library()
        self._create_instance()
        self._configure_instance()

    def _render_gl_frame(self, features: AudioFeatures | None) -> None:
        """Feed audio data and render one projectM frame.

        Called at 30fps by GPUEngineBase render loop.
        """
        if self._pm_handle is None or self._lib is None:
            return

        # Feed FFT data as PCM float samples
        if features is not None:
            for i, val in enumerate(features.fft[:512]):
                self._pcm_buffer[i] = val
            self._lib.projectm_pcm_add_float(
                self._pm_handle,
                ctypes.cast(self._pcm_buffer, ctypes.POINTER(ctypes.c_float)),
                512,
                1,  # mono channel
            )

        # Render the frame into the current FBO
        self._lib.projectm_opengl_render_frame(self._pm_handle)

    # ------------------------------------------------------------------
    # Track change handling
    # ------------------------------------------------------------------

    async def on_track_change(self, metadata: TrackMetadata) -> None:
        """Select a new random preset with smooth blend on track change.

        Uses BLEND_DURATION (3s) for a soft cut transition (Req 5 AC 4).
        """
        self._metadata = metadata
        if self._pm_handle is not None and self._lib is not None:
            # Set soft cut duration to BLEND_DURATION for smooth transition
            self._lib.projectm_set_soft_cut_duration(
                self._pm_handle, ctypes.c_double(self._blend_duration)
            )
            # Select random preset with hard_cut=False for smooth blend
            self._lib.projectm_select_random_preset(
                self._pm_handle, ctypes.c_bool(False)
            )
            log.debug(
                "projectM: track change → random preset with %.1fs blend",
                self._blend_duration,
            )

    # ------------------------------------------------------------------
    # Suspend/Resume overrides (GPU memory management)
    # ------------------------------------------------------------------

    async def suspend(self) -> None:
        """Destroy projectM instance and GL context (frees GPU memory).

        Req 5 AC 5: Engine SHALL destroy the OpenGL context to release
        GPU memory and recreate it on resume.
        """
        self._destroy_projectm()
        await super().suspend()

    async def resume(self, metadata: TrackMetadata | None = None) -> None:
        """Recreate GL context and projectM instance."""
        self._metadata = metadata or self._metadata
        await super().resume(metadata)

    async def stop(self) -> None:
        """Full shutdown — destroy projectM and GL context."""
        self._destroy_projectm()
        await super().stop()

    # ------------------------------------------------------------------
    # Preset path resolution (Req 17 AC 1-3)
    # ------------------------------------------------------------------

    def _resolve_preset_path(self) -> str:
        """Resolve the preset directory based on preset_category config.

        Returns path to category subfolder when preset_category != "all",
        falling back to the base directory if category doesn't exist.
        """
        base = Path(PRESET_DIR)
        if self._preset_category == "all":
            return str(base)

        category_path = base / self._preset_category
        if category_path.is_dir():
            return str(category_path)

        log.warning(
            "projectM: preset category %r not found at %s, using all presets",
            self._preset_category,
            category_path,
        )
        return str(base)

    @classmethod
    def get_available_categories(cls) -> dict[str, int]:
        """Return available preset categories with their preset counts.

        Returns:
            Dict mapping category name to number of preset files.
        """
        base = Path(PRESET_DIR)
        categories: dict[str, int] = {}

        if not base.is_dir():
            return categories

        for entry in sorted(base.iterdir()):
            if entry.is_dir():
                # Count .milk and .prjm files
                preset_count = sum(
                    1
                    for f in entry.iterdir()
                    if f.suffix.lower() in (".milk", ".prjm")
                )
                if preset_count > 0:
                    categories[entry.name] = preset_count

        return categories

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_library(self) -> None:
        """Load the libprojectM shared library via ctypes."""
        try:
            self._lib = ctypes.CDLL("libprojectM-4.so")
        except OSError:
            # Try alternate naming conventions
            try:
                self._lib = ctypes.CDLL("libprojectM.so.4")
            except OSError as e:
                log.error("Failed to load libprojectM: %s", e)
                raise

        # Set up function signatures for type safety
        self._setup_function_signatures()

    def _setup_function_signatures(self) -> None:
        """Configure ctypes function argument and return types."""
        lib = self._lib

        # projectm_create() → handle (void*)
        lib.projectm_create.restype = ctypes.c_void_p
        lib.projectm_create.argtypes = []

        # projectm_destroy(handle)
        lib.projectm_destroy.restype = None
        lib.projectm_destroy.argtypes = [ctypes.c_void_p]

        # projectm_set_window_size(handle, width, height)
        lib.projectm_set_window_size.restype = None
        lib.projectm_set_window_size.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int
        ]

        # projectm_set_preset_duration(handle, seconds)
        lib.projectm_set_preset_duration.restype = None
        lib.projectm_set_preset_duration.argtypes = [
            ctypes.c_void_p, ctypes.c_double
        ]

        # projectm_set_soft_cut_duration(handle, seconds)
        lib.projectm_set_soft_cut_duration.restype = None
        lib.projectm_set_soft_cut_duration.argtypes = [
            ctypes.c_void_p, ctypes.c_double
        ]

        # projectm_set_beat_sensitivity(handle, sensitivity)
        lib.projectm_set_beat_sensitivity.restype = None
        lib.projectm_set_beat_sensitivity.argtypes = [
            ctypes.c_void_p, ctypes.c_float
        ]

        # projectm_set_preset_path(handle, path)
        lib.projectm_set_preset_path.restype = None
        lib.projectm_set_preset_path.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p
        ]

        # projectm_set_shuffle_enabled(handle, enabled)
        lib.projectm_set_shuffle_enabled.restype = None
        lib.projectm_set_shuffle_enabled.argtypes = [
            ctypes.c_void_p, ctypes.c_bool
        ]

        # projectm_pcm_add_float(handle, data, num_samples, channels)
        lib.projectm_pcm_add_float.restype = None
        lib.projectm_pcm_add_float.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_uint,
            ctypes.c_uint,
        ]

        # projectm_opengl_render_frame(handle)
        lib.projectm_opengl_render_frame.restype = None
        lib.projectm_opengl_render_frame.argtypes = [ctypes.c_void_p]

        # projectm_select_random_preset(handle, hard_cut)
        lib.projectm_select_random_preset.restype = None
        lib.projectm_select_random_preset.argtypes = [
            ctypes.c_void_p, ctypes.c_bool
        ]

        # projectm_set_texture_search_paths(handle, paths, count)
        lib.projectm_set_texture_search_paths.restype = None
        lib.projectm_set_texture_search_paths.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.c_size_t,
        ]

    def _create_instance(self) -> None:
        """Create a new projectM instance."""
        self._pm_handle = self._lib.projectm_create()
        if not self._pm_handle:
            raise RuntimeError("projectm_create() returned NULL")
        log.debug("projectM instance created: handle=%s", self._pm_handle)

    def _configure_instance(self) -> None:
        """Apply all configuration to the projectM instance."""
        lib = self._lib
        handle = self._pm_handle

        # Set window size to match our FBO
        lib.projectm_set_window_size(handle, WIDTH, HEIGHT)

        # Set preset timing
        lib.projectm_set_preset_duration(
            handle, ctypes.c_double(self._preset_duration)
        )
        lib.projectm_set_soft_cut_duration(
            handle, ctypes.c_double(self._blend_duration)
        )

        # Beat sensitivity
        lib.projectm_set_beat_sensitivity(
            handle, ctypes.c_float(self._sensitivity)
        )

        # Preset path (resolved from category)
        preset_path = self._resolve_preset_path()
        lib.projectm_set_preset_path(handle, preset_path.encode("utf-8"))

        # Enable shuffle for variety
        lib.projectm_set_shuffle_enabled(handle, ctypes.c_bool(True))

        # Suppress logo overlay by redirecting texture search paths
        self._suppress_logo()

        # Suppress preset title toast overlay if API supports it
        self._suppress_toast()

        log.info(
            "projectM configured: category=%s, preset_duration=%.0fs, "
            "blend=%.1fs, sensitivity=%.1f, path=%s",
            self._preset_category,
            self._preset_duration,
            self._blend_duration,
            self._sensitivity,
            preset_path,
        )

    def _suppress_logo(self) -> None:
        """Prevent libprojectM from loading logo texture by setting search paths to /dev/null."""
        try:
            empty_path = ctypes.c_char_p(b"/dev/null")
            paths_array = (ctypes.c_char_p * 1)(empty_path)
            self._lib.projectm_set_texture_search_paths(
                self._pm_handle,
                paths_array,
                ctypes.c_size_t(1),
            )
            log.debug("projectM: texture search paths set to /dev/null (logo suppressed)")
        except AttributeError:
            log.warning(
                "projectM: projectm_set_texture_search_paths not available, logo suppression skipped"
            )

    def _suppress_toast(self) -> None:
        """Suppress preset title toast overlay if the API supports it.

        Note: projectm_set_toast_message does not exist in libprojectM 4.x.
        This check exists for forward/backward compatibility.
        """
        try:
            toast_fn = getattr(self._lib, "projectm_set_toast_message", None)
            if toast_fn is not None:
                toast_fn.restype = None
                toast_fn.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
                toast_fn(self._pm_handle, b"")
                log.debug("projectM: toast message suppressed")
            else:
                log.debug("projectM: projectm_set_toast_message not available (expected for 4.x)")
        except (AttributeError, OSError):
            log.debug("projectM: projectm_set_toast_message symbol not found (expected for 4.x)")

    def _destroy_projectm(self) -> None:
        """Destroy the projectM instance if active."""
        if self._pm_handle is not None and self._lib is not None:
            try:
                self._lib.projectm_destroy(self._pm_handle)
                log.debug("projectM instance destroyed")
            except Exception as e:
                log.warning("Error destroying projectM instance: %s", e)
            finally:
                self._pm_handle = None
