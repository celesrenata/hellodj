"""Stroke registry for Video Activity whiteboard.

Provides per-guild stroke storage for whiteboard synchronization.
Strokes are stored in insertion order and capped at a maximum count
to prevent unbounded memory growth.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class StrokeData:
    """Server-side stroke record."""

    id: str
    type: str  # freehand, line, rect, ellipse, circle, triangle, star, arrow, text, sticker
    author: str  # user_id string
    color: str  # hex color
    width: float  # normalized width
    points: list[list[float]]  # [[x, y], ...]
    text: str | None = None
    text_bg: bool = False
    sticker_category: str | None = None  # category slug (for type "sticker")
    sticker_filename: str | None = None  # image filename (for type "sticker")
    animated: bool = False  # whether the shape rotates continuously


class StrokeRegistry:
    """Per-guild stroke storage for whiteboard sync.

    Maintains insertion order. Maximum 500 strokes per guild.
    """

    MAX_STROKES = 500

    def __init__(self) -> None:
        self._strokes: dict[str, StrokeData] = {}  # id → StrokeData (insertion-ordered dict)

    def add(self, stroke: StrokeData) -> bool:
        """Add a stroke. Returns False if at capacity."""
        if len(self._strokes) >= self.MAX_STROKES:
            return False
        self._strokes[stroke.id] = stroke
        return True

    def remove(self, stroke_id: str) -> bool:
        """Remove a stroke by ID. Returns False if not found."""
        return self._strokes.pop(stroke_id, None) is not None

    def clear(self) -> None:
        """Remove all strokes."""
        self._strokes.clear()

    def get_all(self) -> list[dict]:
        """Return all strokes as dicts in insertion order for late-joiner sync."""
        return [dataclasses.asdict(s) for s in self._strokes.values()]

    def __len__(self) -> int:
        return len(self._strokes)
