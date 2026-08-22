"""Beat-estimated timing engine for plain text lyrics.

Distributes plain text lyrics lines across song duration proportionally
by character count. When an AudioFeatureBus with active subscribers is
available, line start times are snapped to the nearest detected beat
within a ±500ms tolerance window for improved musical alignment.

Requirements: 2.1, 2.2, 2.3, 2.4, 9.1
"""

from __future__ import annotations

import bisect
import logging
from typing import TYPE_CHECKING

from video.lyrics_models import TimedLine

if TYPE_CHECKING:
    from video.audio_feature_bus import AudioFeatureBus

log = logging.getLogger(__name__)


async def compute_beat_timing(
    plain_text: str,
    duration_s: float,
    audio_bus: AudioFeatureBus | None = None,
) -> list[TimedLine]:
    """Distribute plain text lines across song duration.

    Algorithm:
    1. Split into non-empty lines
    2. Weight each line by character count / total characters
    3. Compute cumulative start times: line_start = cumulative_weight * duration
    4. If AudioFeatureBus available with subscribers and beat data:
       snap each line_start to nearest beat within ±500ms
    5. Return array of TimedLine (words=None for all)

    Args:
        plain_text: Raw lyrics text with newlines separating lines.
        duration_s: Total song duration in seconds.
        audio_bus: Optional AudioFeatureBus for beat-snapping.

    Returns:
        List of TimedLine with computed start times in milliseconds.
        Returns empty list for empty/whitespace-only text.
    """
    # Step 1: Split and filter empty lines
    lines = [line.strip() for line in plain_text.split("\n") if line.strip()]
    if not lines:
        return []

    duration_ms = int(duration_s * 1000)

    # Step 2: Compute weights by character count
    total_chars = sum(len(line) for line in lines)
    if total_chars == 0:
        # All lines are empty after strip — guard with equal distribution
        weights = [1.0 / len(lines)] * len(lines)
    else:
        weights = [len(line) / total_chars for line in lines]

    # Step 3: Cumulative start times
    cumulative = 0.0
    start_times: list[int] = []
    for weight in weights:
        start_times.append(int(cumulative * duration_ms))
        cumulative += weight

    # Step 4: Beat snapping (optional)
    if audio_bus and hasattr(audio_bus, "subscriber_count") and audio_bus.subscriber_count > 0:
        beat_timestamps = await _get_beat_timestamps(audio_bus, duration_ms)
        if beat_timestamps:
            start_times = _snap_to_beats(start_times, beat_timestamps, tolerance_ms=500)

    # Step 5: Build TimedLine array
    return [
        TimedLine(time_ms=start_times[i], text=lines[i], words=None)
        for i in range(len(lines))
    ]


def _snap_to_beats(
    line_starts: list[int],
    beat_timestamps: list[int],
    tolerance_ms: int = 500,
) -> list[int]:
    """Snap each line start to the nearest beat within tolerance.

    Uses binary search for efficiency. For each line start time, finds
    the closest beat timestamp. If the nearest beat is within ±tolerance_ms,
    the line start is moved to that beat. Otherwise, the original
    even-distribution time is preserved.

    Args:
        line_starts: List of line start times in ms (from even distribution).
        beat_timestamps: Sorted list of detected beat timestamps in ms.
        tolerance_ms: Maximum distance (ms) to snap to a beat. Default 500ms.

    Returns:
        New list of snapped start times (same length as line_starts).
    """
    if not beat_timestamps:
        return list(line_starts)

    snapped: list[int] = []
    for start in line_starts:
        idx = bisect.bisect_left(beat_timestamps, start)
        # Check nearest beat on either side
        candidates: list[int] = []
        if idx < len(beat_timestamps):
            candidates.append(beat_timestamps[idx])
        if idx > 0:
            candidates.append(beat_timestamps[idx - 1])
        # Pick closest within tolerance
        best = start
        min_dist = tolerance_ms + 1
        for candidate in candidates:
            dist = abs(candidate - start)
            if dist < min_dist:
                min_dist = dist
                best = candidate
        snapped.append(best if min_dist <= tolerance_ms else start)
    return snapped


async def _get_beat_timestamps(audio_bus: AudioFeatureBus, duration_ms: int) -> list[int]:
    """Get beat timestamps from AudioFeatureBus.

    Attempts multiple interface patterns since AudioFeatureBus may expose
    beat data in different ways depending on implementation stage.

    Returns empty list if beat data is unavailable.

    Args:
        audio_bus: The AudioFeatureBus instance to query.
        duration_ms: Total song duration in ms (for bounds reference).

    Returns:
        Sorted list of beat timestamps in milliseconds, or empty list.
    """
    try:
        # Pattern 1: async method returning beat list
        if hasattr(audio_bus, "get_beats"):
            result = await audio_bus.get_beats()
            if isinstance(result, list) and result:
                return sorted(result)

        # Pattern 2: direct attribute with beat timestamps
        if hasattr(audio_bus, "beat_timestamps"):
            timestamps = audio_bus.beat_timestamps
            if isinstance(timestamps, (list, tuple)) and timestamps:
                return sorted(timestamps)

    except Exception:
        log.debug(
            "AudioFeatureBus: failed to retrieve beat timestamps, "
            "falling back to even distribution",
            exc_info=True,
        )
    return []
