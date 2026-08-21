"""Native Python spectrum visualizer engine — server-rendered, numpy-based.

Produces raw RGBA frames (1280×720) at ~30fps using numpy for buffer
manipulation. Subscribes to AudioFeatureBus for real-time audio features
and renders a 7-band spectrum bar visualizer with beat-reactive effects.

This is a reference implementation — other engines (projectM, vgalizer)
will be more sophisticated but follow the same VisualizerRenderer interface.

Requirements: 7.1
"""

from __future__ import annotations

import asyncio
import colorsys
import logging
from typing import AsyncIterator

import numpy as np

from .base import AudioFeatures, TrackMetadata, VisualizerRenderer

log = logging.getLogger(__name__)

# Frame dimensions and timing
WIDTH = 1280
HEIGHT = 720
CHANNELS = 4  # RGBA
FPS = 30
FRAME_INTERVAL = 1.0 / FPS
FRAME_SIZE = WIDTH * HEIGHT * CHANNELS  # bytes per frame

# Bar layout: 7 bars with spacing
NUM_BANDS = 7
BAR_SPACING = WIDTH // (NUM_BANDS + 2)  # Spacing unit (bars + side margins)
BAR_WIDTH = BAR_SPACING - 8  # Leave 8px gap between bars
MAX_BAR_HEIGHT = 600  # Maximum bar height in pixels

# Colors — base hues for each band (spread across spectrum)
BASE_HUES = [i / NUM_BANDS for i in range(NUM_BANDS)]

# Beat effect parameters
BEAT_PULSE_DECAY = 0.85  # How quickly the beat pulse fades (per frame)
HUE_SHIFT_ON_BEAT = 0.08  # How much the hue shifts on each beat
GLOW_EXPANSION = 12  # Extra pixels on each side during beat glow

# Smoothing for bar heights (avoids jittery visuals)
BAR_SMOOTHING = 0.3  # Lower = smoother (exponential moving average factor)


class NativeEngine(VisualizerRenderer):
    """Numpy-based spectrum bar visualizer — server-rendered.

    Renders 7 vertical bars corresponding to the 7 frequency bands from
    AudioFeatureBus. Bar height is proportional to band energy. Colour hue
    shifts on beat detection and a pulse/glow effect expands bars on beats.
    """

    def __init__(self) -> None:
        self._metadata: TrackMetadata | None = None
        self._feature_queue: asyncio.Queue[AudioFeatures] = asyncio.Queue(maxsize=5)
        self._running = False

        # Rendering state
        self._bar_heights: np.ndarray = np.zeros(NUM_BANDS, dtype=np.float64)
        self._hue_offset: float = 0.0
        self._beat_pulse: float = 0.0  # 0.0–1.0, decays each frame
        self._latest_features: AudioFeatures | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self, metadata: TrackMetadata | None = None) -> None:
        """Set up frame dimensions and initial rendering state."""
        self._metadata = metadata
        self._reset_state()
        log.debug("NativeEngine: initialized (1280x720 @ 30fps)")

    async def activate(self, metadata: TrackMetadata | None = None) -> None:
        """Start accepting audio features for rendering."""
        self._metadata = metadata or self._metadata
        self._running = True
        log.debug("NativeEngine: activated")

    async def suspend(self) -> None:
        """Stop rendering and clear state."""
        self._running = False
        self._reset_state()
        # Drain the feature queue
        while not self._feature_queue.empty():
            try:
                self._feature_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        log.debug("NativeEngine: suspended")

    async def resume(self, metadata: TrackMetadata | None = None) -> None:
        """Resume rendering from suspended state."""
        self._metadata = metadata or self._metadata
        self._running = True
        log.debug("NativeEngine: resumed")

    async def stop(self) -> None:
        """Full shutdown — release all resources."""
        self._running = False
        self._reset_state()
        while not self._feature_queue.empty():
            try:
                self._feature_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._latest_features = None
        log.debug("NativeEngine: stopped")

    async def on_track_change(self, metadata: TrackMetadata) -> None:
        """Update track metadata (displayed in future iterations)."""
        self._metadata = metadata

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_client_side(self) -> bool:
        """False — this engine produces raw frames on the server."""
        return False

    @property
    def consumes_gpu_while_suspended(self) -> bool:
        """False — numpy uses CPU only, no GPU context held."""
        return False

    @property
    def client_config(self) -> None:
        """None — server-rendered engines don't send config to the frontend."""
        return None

    # ------------------------------------------------------------------
    # Audio feature callback
    # ------------------------------------------------------------------

    def on_audio_features(self, features: AudioFeatures) -> None:
        """Callback for AudioFeatureBus — pushes features onto internal queue.

        This is called by the AudioFeatureBus dispatcher. Non-blocking:
        drops old data if the queue is full (backpressure).
        """
        try:
            self._feature_queue.put_nowait(features)
        except asyncio.QueueFull:
            # Drop oldest and insert newest to stay current
            try:
                self._feature_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._feature_queue.put_nowait(features)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------------
    # Frame rendering
    # ------------------------------------------------------------------

    async def render_frames(self) -> AsyncIterator[bytes]:
        """Yield raw RGBA frames at ~30fps.

        Each frame is 1280*720*4 = 3,686,400 bytes. The generator runs
        until the engine is stopped or suspended.
        """
        while self._running:
            # Consume latest audio features (non-blocking, use most recent)
            self._drain_feature_queue()

            # Render a frame
            frame = self._render_frame()

            yield frame.tobytes()

            # Pace at ~30fps
            await asyncio.sleep(FRAME_INTERVAL)

    # ------------------------------------------------------------------
    # Internal rendering
    # ------------------------------------------------------------------

    def _reset_state(self) -> None:
        """Reset all rendering state to initial values."""
        self._bar_heights = np.zeros(NUM_BANDS, dtype=np.float64)
        self._hue_offset = 0.0
        self._beat_pulse = 0.0

    def _drain_feature_queue(self) -> None:
        """Read all available features from the queue, keeping only the latest."""
        while not self._feature_queue.empty():
            try:
                self._latest_features = self._feature_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def _render_frame(self) -> np.ndarray:
        """Render a single RGBA frame as a numpy array.

        Returns:
            numpy array of shape (720, 1280, 4) dtype uint8.
        """
        # Start with black transparent background
        frame = np.zeros((HEIGHT, WIDTH, CHANNELS), dtype=np.uint8)

        # Dark background (very dark blue-grey)
        frame[:, :, 0] = 12  # R
        frame[:, :, 1] = 12  # G
        frame[:, :, 2] = 18  # B
        frame[:, :, 3] = 255  # A (fully opaque)

        features = self._latest_features

        if features is not None:
            # Update beat pulse
            if features.beat:
                self._beat_pulse = 1.0
                self._hue_offset += HUE_SHIFT_ON_BEAT

            # Update target bar heights with smoothing
            for i, energy in enumerate(features.band_energy[:NUM_BANDS]):
                target = min(energy * MAX_BAR_HEIGHT, MAX_BAR_HEIGHT)
                self._bar_heights[i] = (
                    self._bar_heights[i] * (1.0 - BAR_SMOOTHING)
                    + target * BAR_SMOOTHING
                )
        else:
            # No audio data — decay bars to zero
            self._bar_heights *= 0.9

        # Decay beat pulse
        self._beat_pulse *= BEAT_PULSE_DECAY

        # Draw the 7 spectrum bars
        self._draw_bars(frame)

        return frame

    def _draw_bars(self, frame: np.ndarray) -> None:
        """Draw 7 vertical bars on the frame based on current state."""
        glow_extra = int(self._beat_pulse * GLOW_EXPANSION)

        for i in range(NUM_BANDS):
            height = int(self._bar_heights[i])
            if height < 2:
                height = 2  # Minimum visible height

            # Calculate bar position
            x_start = BAR_SPACING + (i * BAR_SPACING) - glow_extra
            x_end = x_start + BAR_WIDTH + (glow_extra * 2)

            # Clamp to frame bounds
            x_start = max(0, x_start)
            x_end = min(WIDTH, x_end)

            # Bar grows upward from bottom
            y_top = HEIGHT - height
            y_top = max(0, y_top)

            # Compute colour for this bar (hue based on band + offset + beat)
            hue = (BASE_HUES[i] + self._hue_offset) % 1.0
            saturation = 0.8 + (self._beat_pulse * 0.2)  # More saturated on beat
            value = 0.7 + (self._beat_pulse * 0.3)  # Brighter on beat

            r, g, b = colorsys.hsv_to_rgb(hue, saturation, value)
            color = np.array([int(r * 255), int(g * 255), int(b * 255)], dtype=np.uint8)

            # Draw the bar (filled rectangle)
            frame[y_top:HEIGHT, x_start:x_end, 0] = color[0]
            frame[y_top:HEIGHT, x_start:x_end, 1] = color[1]
            frame[y_top:HEIGHT, x_start:x_end, 2] = color[2]
            frame[y_top:HEIGHT, x_start:x_end, 3] = 255

            # Draw a brighter cap at the top of the bar
            cap_height = min(4, height)
            cap_color = np.array(
                [min(255, int(r * 255) + 80),
                 min(255, int(g * 255) + 80),
                 min(255, int(b * 255) + 80)],
                dtype=np.uint8,
            )
            frame[y_top:y_top + cap_height, x_start:x_end, 0] = cap_color[0]
            frame[y_top:y_top + cap_height, x_start:x_end, 1] = cap_color[1]
            frame[y_top:y_top + cap_height, x_start:x_end, 2] = cap_color[2]
            frame[y_top:y_top + cap_height, x_start:x_end, 3] = 255
