"""Property-based test for Hive log-partition key derivation round-trip.

Feature: aws-saas-replatform, Property 11

Property 11 (Hive log-partition key derivation round-trip):
    For any log event with a timestamp and partition fields, the S3 key
    derivation function produces a Hive-partitioned key (for example
    ``.../year=YYYY/month=MM/day=DD/hour=HH/...``), and parsing that key
    recovers exactly the original partition values. Concretely:

        * ``from_key(to_key(event)) == event`` for every valid ``LogEvent``.
        * ``to_key(from_key(key)) == key`` for every key produced by ``to_key``.

    Partition fields are generated as integers within their valid Hive ranges
    (year 0-9999, month 1-12, day 1-31, hour 0-23) and the optional prefix /
    suffix are generated to satisfy the ``LogEvent`` constraints (non-empty, no
    leading/trailing slash, no empty segments, and — for the prefix — no
    segment that collides with a ``year=/month=/day=/hour=`` partition).

Validates: Requirements 10.1
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.hive_partition import (
    LogEvent,
    from_key,
    to_key,
)

# Characters allowed inside a single path segment of a prefix/suffix. We avoid
# ``/`` (the segment separator) so segments never introduce empty parts, and
# keep to a printable, S3-key-friendly alphabet while still exploring a wide
# input space.
_SEGMENT_CHARS = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "-_.=+"
)


def _is_partition_segment(segment: str) -> bool:
    """True if a segment collides with a Hive partition segment.

    Mirrors ``hive_partition._PARTITION_SEGMENT`` so the generator can exclude
    prefixes that ``LogEvent`` would reject (``year=``/``month=``/``day=``/
    ``hour=`` followed by digits).
    """
    key, _, rest = segment.partition("=")
    return key in {"year", "month", "day", "hour"} and rest.isdigit() and bool(
        rest
    )


@st.composite
def path_segments(draw: st.DrawFn) -> str:
    """Generate one non-empty path segment (no ``/``)."""
    return draw(st.text(alphabet=_SEGMENT_CHARS, min_size=1, max_size=12))


@st.composite
def prefixes(draw: st.DrawFn) -> str | None:
    """Generate a valid optional prefix or ``None``.

    A valid prefix is either ``None`` or a ``/``-joined sequence of non-empty
    segments where no segment matches a Hive partition segment (so the
    prefix/partition boundary stays unambiguous), and with no leading/trailing
    slash.
    """
    if draw(st.booleans()):
        return None
    segments = draw(
        st.lists(
            path_segments().filter(lambda s: not _is_partition_segment(s)),
            min_size=1,
            max_size=3,
        )
    )
    return "/".join(segments)


@st.composite
def suffixes(draw: st.DrawFn) -> str | None:
    """Generate a valid optional suffix or ``None``.

    A valid suffix is either ``None`` or a ``/``-joined sequence of non-empty
    segments with no leading/trailing slash. Unlike the prefix, a suffix may
    contain partition-like segments because it is anchored after the partition.
    """
    if draw(st.booleans()):
        return None
    segments = draw(st.lists(path_segments(), min_size=1, max_size=3))
    return "/".join(segments)


@st.composite
def log_events(draw: st.DrawFn) -> LogEvent:
    """Generate an arbitrary valid ``LogEvent`` across the full input space."""
    return LogEvent(
        year=draw(st.integers(min_value=0, max_value=9999)),
        month=draw(st.integers(min_value=1, max_value=12)),
        day=draw(st.integers(min_value=1, max_value=31)),
        hour=draw(st.integers(min_value=0, max_value=23)),
        prefix=draw(prefixes()),
        suffix=draw(suffixes()),
    )


@settings(max_examples=200)
@given(event=log_events())
def test_event_key_event_round_trip(event: LogEvent) -> None:
    """``from_key(to_key(event)) == event`` for every valid event.

    Feature: aws-saas-replatform, Property 11
    Validates: Requirements 10.1
    """
    key = to_key(event)

    # The derived key is Hive-partitioned: it contains the four ordered,
    # fixed-width partition segments.
    assert (
        f"year={event.year:04d}/"
        f"month={event.month:02d}/"
        f"day={event.day:02d}/"
        f"hour={event.hour:02d}"
    ) in key

    # Parsing recovers exactly the original event (all six fields).
    recovered = from_key(key)
    assert recovered == event
    assert recovered.year == event.year
    assert recovered.month == event.month
    assert recovered.day == event.day
    assert recovered.hour == event.hour
    assert recovered.prefix == event.prefix
    assert recovered.suffix == event.suffix


@settings(max_examples=200)
@given(event=log_events())
def test_key_event_key_round_trip(event: LogEvent) -> None:
    """``to_key(from_key(key)) == key`` for keys produced by ``to_key``.

    Feature: aws-saas-replatform, Property 11
    Validates: Requirements 10.1
    """
    key = to_key(event)
    assert to_key(from_key(key)) == key
