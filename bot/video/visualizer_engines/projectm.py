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
        self._playlist_handle: ctypes.c_void_p | None = None
        self._lib: ctypes.CDLL | None = None
        self._playlist_lib: ctypes.CDLL | None = None
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
        if self._pm_handle is not None and self._lib is not None and self._playlist_handle is not None:
            # Set soft cut duration to BLEND_DURATION for smooth transition
            self._lib.projectm_set_soft_cut_duration(
                self._pm_handle, ctypes.c_double(self._blend_duration)
            )
            # Advance to next random preset with soft cut (hard_cut=False)
            self._playlist_lib.projectm_playlist_play_next(
                self._playlist_handle, ctypes.c_bool(False)
            )
            log.debug(
                "projectM: track change → next preset with %.1fs blend",
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
        """Load the libprojectM shared library and playlist library via ctypes."""
        # Load core library
        lib_names = ["libprojectM-4.so", "libprojectM.so.4", "libprojectM.so"]
        last_error: OSError | None = None
        for name in lib_names:
            try:
                self._lib = ctypes.CDLL(name)
                log.debug("Loaded libprojectM from: %s", name)
                break
            except OSError as e:
                last_error = e
                continue
        else:
            log.error("Failed to load libprojectM (tried %s): %s", lib_names, last_error)
            raise last_error  # type: ignore[misc]

        # Load playlist library (separate .so in projectM 4.x)
        playlist_names = ["libprojectM-4-playlist.so", "libprojectM-playlist.so.4"]
        for name in playlist_names:
            try:
                self._playlist_lib = ctypes.CDLL(name)
                log.debug("Loaded libprojectM playlist from: %s", name)
                break
            except OSError as e:
                last_error = e
                continue
        else:
            log.error("Failed to load libprojectM playlist (tried %s): %s", playlist_names, last_error)
            raise last_error  # type: ignore[misc]

        # Set up function signatures for type safety
        self._setup_function_signatures()

    def _setup_function_signatures(self) -> None:
        """Configure ctypes function argument and return types."""
        lib = self._lib
        plib = self._playlist_lib

        # --- Core library (libprojectM-4.so) ---

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

        # projectm_set_texture_search_paths(handle, paths, count)
        lib.projectm_set_texture_search_paths.restype = None
        lib.projectm_set_texture_search_paths.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.c_size_t,
        ]

        # --- Playlist library (libprojectM-4-playlist.so) ---

        # projectm_playlist_create(projectm_handle) → playlist_handle
        plib.projectm_playlist_create.restype = ctypes.c_void_p
        plib.projectm_playlist_create.argtypes = [ctypes.c_void_p]

        # projectm_playlist_destroy(playlist_handle)
        plib.projectm_playlist_destroy.restype = None
        plib.projectm_playlist_destroy.argtypes = [ctypes.c_void_p]

        # projectm_playlist_add_path(playlist_handle, path, recurse)
        plib.projectm_playlist_add_path.restype = ctypes.c_uint
        plib.projectm_playlist_add_path.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_bool
        ]

        # projectm_playlist_set_shuffle(playlist_handle, shuffle)
        plib.projectm_playlist_set_shuffle.restype = None
        plib.projectm_playlist_set_shuffle.argtypes = [
            ctypes.c_void_p, ctypes.c_bool
        ]

        # projectm_playlist_play_next(playlist_handle, hard_cut) → position
        plib.projectm_playlist_play_next.restype = ctypes.c_uint
        plib.projectm_playlist_play_next.argtypes = [
            ctypes.c_void_p, ctypes.c_bool
        ]

        # projectm_playlist_size(playlist_handle) → count
        plib.projectm_playlist_size.restype = ctypes.c_uint
        plib.projectm_playlist_size.argtypes = [ctypes.c_void_p]

        # projectm_playlist_set_retry_count(playlist_handle, count)
        plib.projectm_playlist_set_retry_count.restype = None
        plib.projectm_playlist_set_retry_count.argtypes = [
            ctypes.c_void_p, ctypes.c_uint
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
        plib = self._playlist_lib
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

        # Suppress logo overlay by redirecting texture search paths
        self._suppress_logo()

        # Create playlist, add presets, enable shuffle
        preset_path = self._resolve_preset_path()
        self._playlist_handle = plib.projectm_playlist_create(handle)
        if not self._playlist_handle:
            raise RuntimeError("projectm_playlist_create() returned NULL")

        # Add preset directory (recursive scan for .milk files)
        added = plib.projectm_playlist_add_path(
            self._playlist_handle, preset_path.encode("utf-8"), ctypes.c_bool(True)
        )
        log.debug("projectM playlist: added %d presets from %s", added, preset_path)

        # Enable shuffle for variety
        plib.projectm_playlist_set_shuffle(self._playlist_handle, ctypes.c_bool(True))

        # Set retry count for failed presets
        plib.projectm_playlist_set_retry_count(self._playlist_handle, ctypes.c_uint(500))

        # Start playing the first preset (soft cut)
        if added > 0:
            plib.projectm_playlist_play_next(self._playlist_handle, ctypes.c_bool(False))

        # Suppress preset title toast overlay if API supports it
        self._suppress_toast()

        log.info(
            "projectM configured: category=%s, preset_duration=%.0fs, "
            "blend=%.1fs, sensitivity=%.1f, path=%s, presets=%d",
            self._preset_category,
            self._preset_duration,
            self._blend_duration,
            self._sensitivity,
            preset_path,
            added,
        )

    def _suppress_logo(self) -> None:
        """Prevent libprojectM from rendering the floating M logo overlay."""
        # Method 1: Disable the "easter egg" (the logo overlay)
        try:
            self._lib.projectm_set_easter_egg.restype = None
            self._lib.projectm_set_easter_egg.argtypes = [ctypes.c_void_p, ctypes.c_float]
            self._lib.projectm_set_easter_egg(self._pm_handle, ctypes.c_float(0.0))
            log.debug("projectM: easter egg (logo) disabled")
        except (AttributeError, OSError) as e:
            log.warning("projectM: projectm_set_easter_egg not available: %s", e)

        # Method 2: Set texture search paths to /dev/null (belt and suspenders)
        try:
            empty_path = ctypes.c_char_p(b"/dev/null")
            paths_array = (ctypes.c_char_p * 1)(empty_path)
            self._lib.projectm_set_texture_search_paths(
                self._pm_handle,
                paths_array,
                ctypes.c_size_t(1),
            )
            log.debug("projectM: texture search paths set to /dev/null")
        except (AttributeError, OSError):
            log.warning(
                "projectM: projectm_set_texture_search_paths not available, skipped"
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
        """Destroy the projectM instance and playlist if active."""
        if self._playlist_handle is not None and self._playlist_lib is not None:
            try:
                self._playlist_lib.projectm_playlist_destroy(self._playlist_handle)
                log.debug("projectM playlist destroyed")
            except Exception as e:
                log.warning("Error destroying projectM playlist: %s", e)
            finally:
                self._playlist_handle = None

        if self._pm_handle is not None and self._lib is not None:
            try:
                self._lib.projectm_destroy(self._pm_handle)
                log.debug("projectM instance destroyed")
            except Exception as e:
                log.warning("Error destroying projectM instance: %s", e)
            finally:
                self._pm_handle = None
