"""Whiteboard stroke registry for the Activity (Requirement 6.2).

Provides per-guild stroke storage for whiteboard synchronization. Strokes are
stored in insertion order and capped to prevent unbounded memory growth. Stroke
validation is done here (pure logic) so both the HTTP and WebSocket layers share
one authoritative validator, and so it is testable without aiohttp.

This mirrors the legacy ``stroke_registry.StrokeData``/``StrokeRegistry`` model
so the client whiteboard protocol is preserved through the re-platform.
"""

from __future__ import annotations

import dataclasses

from .models import STROKE_TYPES

__all__ = ["StrokeData", "StrokeRegistry", "validate_stroke_payload"]


@dataclasses.dataclass
class StrokeData:
    """Server-side stroke record."""

    id: str
    type: str
    author: str
    color: str
    width: float
    points: list[list[float]]
    text: str | None = None
    text_bg: bool = False
    sticker_category: str | None = None
    sticker_filename: str | None = None
    animated: bool = False
    rotation: float = 0.0
    filled: bool = False


def validate_stroke_payload(data: dict) -> tuple[StrokeData | None, str | None]:
    """Validate a ``stroke_add`` payload and build a :class:`StrokeData`.

    Returns a ``(stroke, None)`` pair on success or ``(None, error)`` with a
    human-readable reason on failure. Pure function — no I/O.

    Args:
        data: The decoded ``stroke_add`` message body.

    Returns:
        A tuple of the validated stroke (or ``None``) and an error message
        (or ``None``).
    """
    stroke_id = data.get("id")
    stroke_type = data.get("stroke_type")
    points = data.get("points")
    color = data.get("color")
    width = data.get("width")
    author = data.get("author")

    required = [stroke_id, stroke_type, points, color, author]
    if not all(required) or width is None:
        return None, "stroke_add: missing required fields"

    if stroke_type not in STROKE_TYPES:
        return None, f"stroke_add: invalid type '{stroke_type}'"

    if not isinstance(points, list) or len(points) == 0:
        return None, "stroke_add: empty points array"

    sticker_category = data.get("sticker_category")
    sticker_filename = data.get("sticker_filename")
    if stroke_type == "sticker" and not (sticker_category and sticker_filename):
        return None, (
            "stroke_add: sticker requires sticker_category and sticker_filename"
        )

    try:
        width_value = float(width)
    except (TypeError, ValueError):
        return None, "stroke_add: width must be numeric"

    stroke = StrokeData(
        id=str(stroke_id),
        type=str(stroke_type),
        author=str(author),
        color=str(color),
        width=width_value,
        points=[[float(x), float(y)] for x, y in points],
        text=data.get("text"),
        text_bg=bool(data.get("text_bg", False)),
        sticker_category=sticker_category,
        sticker_filename=sticker_filename,
        animated=bool(data.get("animated", False)),
        rotation=float(data.get("rotation", 0.0) or 0.0),
        filled=bool(data.get("filled", False)),
    )
    return stroke, None


class StrokeRegistry:
    """Per-guild stroke storage for whiteboard sync.

    Maintains insertion order and enforces a per-guild maximum (default 500).
    """

    def __init__(self, max_strokes: int = 500) -> None:
        """Initialise an empty registry with the given capacity."""
        self._max = max(1, int(max_strokes))
        self._strokes: dict[str, StrokeData] = {}

    @property
    def max_strokes(self) -> int:
        """The per-guild stroke capacity."""
        return self._max

    def add(self, stroke: StrokeData) -> bool:
        """Add ``stroke``; return ``False`` if at capacity.

        Re-adding an existing id updates it in place and always succeeds (it
        does not consume additional capacity).
        """
        if stroke.id not in self._strokes and len(self._strokes) >= self._max:
            return False
        self._strokes[stroke.id] = stroke
        return True

    def remove(self, stroke_id: str) -> bool:
        """Remove a stroke by id; return ``False`` if it was not present."""
        return self._strokes.pop(stroke_id, None) is not None

    def clear(self) -> None:
        """Remove all strokes for the guild."""
        self._strokes.clear()

    def get_all(self) -> list[dict]:
        """Return all strokes as dicts in insertion order (late-joiner sync)."""
        return [dataclasses.asdict(s) for s in self._strokes.values()]

    def __len__(self) -> int:
        return len(self._strokes)
