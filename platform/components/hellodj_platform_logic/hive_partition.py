"""Hive-partitioned S3 key derivation for the HelloDJ Log_Store.

This module is the single source of truth for how a log event maps to a
Hive-partitioned Amazon S3 key (Requirement 10.1). The observability/analytics
CDK stack writes logs into the S3 ``Log_Store`` using these keys and the Glue
crawler catalogs them by the same ``year=/month=/day=/hour=`` partition scheme,
so the infrastructure-as-code layer and any runtime log shipper agree on one
derivation.

Key shape (Property 11 / R10.1)::

    <prefix>/year=YYYY/month=MM/day=DD/hour=HH[/<suffix>]

The four partition segments are zero-padded to a fixed width (``year`` to 4
digits, ``month``/``day``/``hour`` to 2 digits) so lexicographic ordering of
keys matches chronological ordering. The optional ``prefix`` groups events by
source/dataset (e.g. ``bot-logs``) and the optional ``suffix`` carries the
object name (e.g. ``part-0001.json``); both are preserved verbatim so the
derivation round-trips.

Round-trip invariant (Property 11):
    * ``from_key(to_key(event)) == event`` for every valid :class:`LogEvent`.
    * ``to_key(from_key(key)) == key`` for every key produced by :func:`to_key`.

Both functions are pure: they depend only on their inputs and perform no live
AWS calls, so they can be imported by both the CDK layer and runtime components
and exercised directly by property-based tests.

Requirements: 10.1
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Fixed zero-pad widths for each Hive partition segment. Padding ``year`` to 4
# digits and the rest to 2 keeps keys lexicographically ordered by time.
_YEAR_WIDTH = 4
_MONTH_WIDTH = 2
_DAY_WIDTH = 2
_HOUR_WIDTH = 2

# Inclusive value ranges for each partition field. ``year`` is bounded to the
# four-digit space so it never overflows the fixed pad width.
_YEAR_RANGE = range(0, 10000)
_MONTH_RANGE = range(1, 13)
_DAY_RANGE = range(1, 32)
_HOUR_RANGE = range(0, 24)

# One Hive partition segment: ``key=<zero-padded-digits>``. The four ordered
# segments (year, month, day, hour) are matched together by ``_KEY_PATTERN``.
_PARTITION = (
    r"year=(?P<year>\d{4})/"
    r"month=(?P<month>\d{2})/"
    r"day=(?P<day>\d{2})/"
    r"hour=(?P<hour>\d{2})"
)

# Full key: an optional ``<prefix>/`` group, the four partition segments, and an
# optional ``/<suffix>`` tail. Anchored so only well-formed keys parse.
_KEY_PATTERN = re.compile(
    r"^(?:(?P<prefix>.+?)/)?" + _PARTITION + r"(?:/(?P<suffix>.+))?$"
)


@dataclass(frozen=True)
class LogEvent:
    """A log event reduced to its Hive partition fields plus key framing.

    Instances are the round-trip unit for :func:`to_key`/:func:`from_key`: the
    four partition fields become the ``year=/month=/day=/hour=`` segments, while
    ``prefix`` and ``suffix`` frame those segments in the S3 key and are carried
    through the derivation unchanged.

    Attributes:
        year: Calendar year, ``0 <= year <= 9999`` (rendered as 4 digits).
        month: Calendar month, ``1 <= month <= 12`` (rendered as 2 digits).
        day: Day of month, ``1 <= day <= 31`` (rendered as 2 digits).
        hour: Hour of day, ``0 <= hour <= 23`` (rendered as 2 digits).
        prefix: Optional path prefix grouping events by source/dataset. It must
            not be empty and must not contain a segment matching a Hive
            partition (``year=``/``month=``/``day=``/``hour=``) so parsing is
            unambiguous. ``None`` means no prefix.
        suffix: Optional trailing path (e.g. an object name) appended after the
            partition segments. It must not be empty. ``None`` means no suffix.
    """

    year: int
    month: int
    day: int
    hour: int
    prefix: str | None = None
    suffix: str | None = None

    def __post_init__(self) -> None:
        """Validate every field so only round-trippable events can exist.

        Raises:
            TypeError: if any partition field is not an ``int``.
            ValueError: if any partition field is out of range, or if
                ``prefix``/``suffix`` is empty or otherwise unparseable.
        """
        _validate_partition_field("year", self.year, _YEAR_RANGE)
        _validate_partition_field("month", self.month, _MONTH_RANGE)
        _validate_partition_field("day", self.day, _DAY_RANGE)
        _validate_partition_field("hour", self.hour, _HOUR_RANGE)
        _validate_prefix(self.prefix)
        _validate_suffix(self.suffix)


def _validate_partition_field(name: str, value: int, allowed: range) -> None:
    """Validate a single partition field is an in-range integer.

    ``bool`` is rejected explicitly because ``bool`` is a subclass of ``int``
    and would otherwise slip through as ``0``/``1``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value)!r}")
    if value not in allowed:
        raise ValueError(
            f"{name}={value} is out of range "
            f"[{allowed.start}, {allowed.stop - 1}]"
        )


def _validate_prefix(prefix: str | None) -> None:
    """Validate an optional key prefix.

    A prefix, when present, must be a non-empty string with no leading/trailing
    slash and must not itself contain a Hive partition segment, otherwise the
    boundary between prefix and partition would be ambiguous when parsing.
    """
    if prefix is None:
        return
    if not isinstance(prefix, str):
        raise TypeError(f"prefix must be a str or None, got {type(prefix)!r}")
    if not prefix:
        raise ValueError("prefix must be a non-empty string or None")
    if prefix.startswith("/") or prefix.endswith("/"):
        raise ValueError(f"prefix must not start or end with '/': {prefix!r}")
    for segment in prefix.split("/"):
        if not segment:
            raise ValueError(f"prefix must not contain empty segments: {prefix!r}")
        if _PARTITION_SEGMENT.match(segment):
            raise ValueError(
                f"prefix must not contain a Hive partition segment: {prefix!r}"
            )


def _validate_suffix(suffix: str | None) -> None:
    """Validate an optional key suffix.

    A suffix, when present, must be a non-empty string with no leading/trailing
    slash so it round-trips as the tail of the key.
    """
    if suffix is None:
        return
    if not isinstance(suffix, str):
        raise TypeError(f"suffix must be a str or None, got {type(suffix)!r}")
    if not suffix:
        raise ValueError("suffix must be a non-empty string or None")
    if suffix.startswith("/") or suffix.endswith("/"):
        raise ValueError(f"suffix must not start or end with '/': {suffix!r}")
    for segment in suffix.split("/"):
        if not segment:
            raise ValueError(f"suffix must not contain empty segments: {suffix!r}")


# A single ``key=digits`` partition segment, used to reject prefixes that would
# collide with the year/month/day/hour partition boundary.
_PARTITION_SEGMENT = re.compile(r"^(?:year|month|day|hour)=\d+$")


def to_key(event: LogEvent) -> str:
    """Derive the Hive-partitioned S3 key for a log event (R10.1).

    The four partition fields are zero-padded and rendered as
    ``year=YYYY/month=MM/day=DD/hour=HH``. When ``event.prefix`` is set it is
    prepended as ``<prefix>/``; when ``event.suffix`` is set it is appended as
    ``/<suffix>`` (Property 11).

    Args:
        event: The validated log event to derive a key for.

    Returns:
        The Hive-partitioned S3 key.

    Raises:
        TypeError: if ``event`` is not a :class:`LogEvent`.
    """
    if not isinstance(event, LogEvent):
        raise TypeError(f"event must be a LogEvent, got {type(event)!r}")

    partition = (
        f"year={event.year:0{_YEAR_WIDTH}d}/"
        f"month={event.month:0{_MONTH_WIDTH}d}/"
        f"day={event.day:0{_DAY_WIDTH}d}/"
        f"hour={event.hour:0{_HOUR_WIDTH}d}"
    )

    parts = []
    if event.prefix is not None:
        parts.append(event.prefix)
    parts.append(partition)
    if event.suffix is not None:
        parts.append(event.suffix)
    key = "/".join(parts)

    # Invariant (Property 11): the derived key parses back to the same event.
    assert from_key(key) == event, (
        f"to_key produced a non-round-tripping key: {key!r}"
    )
    return key


def from_key(key: str) -> LogEvent:
    """Recover the exact partition values from a Hive-partitioned key (R10.1).

    Parses a key of the form
    ``[<prefix>/]year=YYYY/month=MM/day=DD/hour=HH[/<suffix>]`` and reconstructs
    the :class:`LogEvent`, recovering the four partition values plus the prefix
    and suffix exactly (Property 11).

    Args:
        key: A Hive-partitioned S3 key produced by :func:`to_key`.

    Returns:
        The :class:`LogEvent` whose fields the key encodes.

    Raises:
        TypeError: if ``key`` is not a ``str``.
        ValueError: if ``key`` is not a well-formed Hive-partitioned key.
    """
    if not isinstance(key, str):
        raise TypeError(f"key must be a str, got {type(key)!r}")

    match = _KEY_PATTERN.match(key)
    if match is None:
        raise ValueError(f"key is not a valid Hive-partitioned key: {key!r}")

    return LogEvent(
        year=int(match.group("year")),
        month=int(match.group("month")),
        day=int(match.group("day")),
        hour=int(match.group("hour")),
        prefix=match.group("prefix"),
        suffix=match.group("suffix"),
    )
