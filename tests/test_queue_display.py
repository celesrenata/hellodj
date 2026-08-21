"""Tests for the unified queue display module.

Covers:
- format_duration() — M:SS, H:MM:SS, "Live" formatting
- format_queue_item() — prefix emoji, title truncation, index numbering
- build_queue_embed() — now playing, pagination, footer
- build_dual_queue_embed() — dual-session sections
- QueuePaginationView — button disabled states at boundaries
- Property 14: Queue display formatting
- Property 15: Queue pagination
"""

from __future__ import annotations

import math
import time

import discord
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bot.playback.queue_display import (
    ITEMS_PER_PAGE,
    MAX_TITLE_LENGTH,
    QueuePaginationView,
    build_dual_queue_embed,
    build_queue_embed,
    format_duration,
    format_queue_item,
)
from bot.playback.session_registry import ChannelSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    session_type: str = "audio",
    queue: list[dict] | None = None,
    current: dict | None = None,
) -> ChannelSession:
    return ChannelSession(
        guild_id=1,
        channel_id=100,
        session_type=session_type,  # type: ignore[arg-type]
        started_at=time.time(),
        queue=queue if queue is not None else [],
        current=current,
    )


def _make_track(
    title: str = "Test Track",
    duration: int | None = 240000,
    author: str | None = "Artist",
) -> dict:
    return {"title": title, "duration": duration, "author": author}


# ---------------------------------------------------------------------------
# Unit Tests — format_duration
# ---------------------------------------------------------------------------


class TestFormatDuration:
    def test_none_returns_live(self) -> None:
        assert format_duration(None) == "Live"

    def test_zero_returns_live(self) -> None:
        assert format_duration(0) == "Live"

    def test_negative_returns_live(self) -> None:
        assert format_duration(-1000) == "Live"

    def test_short_duration_mss(self) -> None:
        # 4 minutes 23 seconds = 263000 ms
        assert format_duration(263000) == "4:23"

    def test_one_second(self) -> None:
        assert format_duration(1000) == "0:01"

    def test_exactly_one_minute(self) -> None:
        assert format_duration(60000) == "1:00"

    def test_under_one_hour(self) -> None:
        # 59:59 = 3599000 ms
        assert format_duration(3599000) == "59:59"

    def test_exactly_one_hour(self) -> None:
        # 1:00:00 = 3600000 ms
        assert format_duration(3600000) == "1:00:00"

    def test_over_one_hour(self) -> None:
        # 1:05:30 = 3930000 ms
        assert format_duration(3930000) == "1:05:30"

    def test_multi_hour(self) -> None:
        # 2:30:45 = 9045000 ms
        assert format_duration(9045000) == "2:30:45"

    def test_seconds_padded(self) -> None:
        # 3:05 = 185000 ms
        assert format_duration(185000) == "3:05"

    def test_hours_minutes_padded(self) -> None:
        # 1:02:03 = 3723000 ms
        assert format_duration(3723000) == "1:02:03"


# ---------------------------------------------------------------------------
# Unit Tests — format_queue_item
# ---------------------------------------------------------------------------


class TestFormatQueueItem:
    def test_audio_prefix(self) -> None:
        item = _make_track(title="Song", duration=180000)
        result = format_queue_item(item, "audio", 1)
        assert "🎵" in result

    def test_video_prefix(self) -> None:
        item = _make_track(title="Video", duration=180000)
        result = format_queue_item(item, "video", 1)
        assert "🎬" in result

    def test_index_displayed(self) -> None:
        item = _make_track(title="Song", duration=180000)
        result = format_queue_item(item, "audio", 5)
        assert "`5.`" in result

    def test_duration_in_brackets(self) -> None:
        item = _make_track(title="Song", duration=263000)
        result = format_queue_item(item, "audio", 1)
        assert "[4:23]" in result

    def test_title_truncation(self) -> None:
        long_title = "A" * 150
        item = _make_track(title=long_title, duration=180000)
        result = format_queue_item(item, "audio", 1)
        # Truncated to 100 chars means 97 chars + "..."
        assert "..." in result
        # The full 150-char title should NOT appear
        assert long_title not in result

    def test_title_at_limit_not_truncated(self) -> None:
        exact_title = "B" * 100
        item = _make_track(title=exact_title, duration=180000)
        result = format_queue_item(item, "audio", 1)
        assert exact_title in result
        assert "..." not in result

    def test_missing_title_shows_unknown(self) -> None:
        item = {"duration": 180000}
        result = format_queue_item(item, "audio", 1)
        assert "Unknown" in result

    def test_live_stream_duration(self) -> None:
        item = _make_track(title="Stream", duration=None)
        result = format_queue_item(item, "audio", 1)
        assert "[Live]" in result


# ---------------------------------------------------------------------------
# Unit Tests — build_queue_embed
# ---------------------------------------------------------------------------


class TestBuildQueueEmbed:
    def test_empty_queue_shows_empty_message(self) -> None:
        session = _make_session(
            current=_make_track(title="Playing Now", duration=200000)
        )
        embed = build_queue_embed(session)
        # Check fields
        fields = {f.name: f.value for f in embed.fields}
        assert "Queue is empty" in fields["Up Next"]

    def test_now_playing_shown(self) -> None:
        session = _make_session(
            current=_make_track(title="Current Song", duration=300000)
        )
        embed = build_queue_embed(session)
        fields = {f.name: f.value for f in embed.fields}
        assert "Current Song" in fields["Now Playing"]

    def test_no_current_shows_nothing(self) -> None:
        session = _make_session(current=None)
        embed = build_queue_embed(session)
        fields = {f.name: f.value for f in embed.fields}
        assert "Nothing playing" in fields["Now Playing"]

    def test_queue_items_displayed(self) -> None:
        queue = [_make_track(title=f"Track {i}", duration=180000) for i in range(5)]
        session = _make_session(queue=queue, current=_make_track())
        embed = build_queue_embed(session)
        fields = {f.name: f.value for f in embed.fields}
        assert "Track 0" in fields["Up Next"]
        assert "Track 4" in fields["Up Next"]

    def test_pagination_page_1_shows_first_10(self) -> None:
        queue = [_make_track(title=f"Track {i}", duration=180000) for i in range(15)]
        session = _make_session(queue=queue, current=_make_track())
        embed = build_queue_embed(session, page=1)
        fields = {f.name: f.value for f in embed.fields}
        assert "Track 0" in fields["Up Next"]
        assert "Track 9" in fields["Up Next"]
        assert "Track 10" not in fields["Up Next"]

    def test_pagination_page_2_shows_remaining(self) -> None:
        queue = [_make_track(title=f"Track {i}", duration=180000) for i in range(15)]
        session = _make_session(queue=queue, current=_make_track())
        embed = build_queue_embed(session, page=2)
        fields = {f.name: f.value for f in embed.fields}
        assert "Track 10" in fields["Up Next"]
        assert "Track 14" in fields["Up Next"]
        assert "Track 0" not in fields["Up Next"]

    def test_footer_shows_page_info(self) -> None:
        queue = [_make_track() for _ in range(25)]
        session = _make_session(queue=queue, current=_make_track())
        embed = build_queue_embed(session, page=2)
        assert embed.footer is not None
        assert "Page 2/3" in embed.footer.text  # type: ignore[operator]

    def test_footer_shows_queue_count(self) -> None:
        queue = [_make_track() for _ in range(7)]
        session = _make_session(queue=queue, current=_make_track())
        embed = build_queue_embed(session)
        assert embed.footer is not None
        assert "7 track(s)" in embed.footer.text  # type: ignore[operator]

    def test_audio_session_color_blurple(self) -> None:
        session = _make_session(session_type="audio", current=_make_track())
        embed = build_queue_embed(session)
        assert embed.color == discord.Color.blurple()

    def test_video_session_color_red(self) -> None:
        session = _make_session(session_type="video", current=_make_track())
        embed = build_queue_embed(session)
        assert embed.color == discord.Color.red()

    def test_page_clamped_to_valid_range(self) -> None:
        queue = [_make_track() for _ in range(5)]
        session = _make_session(queue=queue, current=_make_track())
        # Page 100 should be clamped to page 1 (only 1 page exists)
        embed = build_queue_embed(session, page=100)
        assert embed.footer is not None
        assert "Page 1/1" in embed.footer.text  # type: ignore[operator]

    def test_page_zero_clamped_to_1(self) -> None:
        queue = [_make_track() for _ in range(5)]
        session = _make_session(queue=queue, current=_make_track())
        embed = build_queue_embed(session, page=0)
        assert embed.footer is not None
        assert "Page 1/1" in embed.footer.text  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Unit Tests — build_dual_queue_embed
# ---------------------------------------------------------------------------


class TestBuildDualQueueEmbed:
    def test_has_both_sections(self) -> None:
        audio = _make_session(
            session_type="audio",
            queue=[_make_track(title="Audio Track")],
            current=_make_track(title="Audio Now"),
        )
        video = _make_session(
            session_type="video",
            queue=[_make_track(title="Video Track")],
            current=_make_track(title="Video Now"),
        )
        embed = build_dual_queue_embed(audio, video)
        field_names = [f.name for f in embed.fields]
        assert "🎵 Audio" in field_names
        assert "🎬 Video" in field_names

    def test_audio_section_content(self) -> None:
        audio = _make_session(
            session_type="audio",
            queue=[_make_track(title="AudioQ1")],
            current=_make_track(title="AudioNow"),
        )
        video = _make_session(session_type="video", queue=[], current=None)
        embed = build_dual_queue_embed(audio, video)
        audio_field = next(f for f in embed.fields if f.name == "🎵 Audio")
        assert "AudioNow" in audio_field.value
        assert "AudioQ1" in audio_field.value

    def test_video_section_content(self) -> None:
        audio = _make_session(session_type="audio", queue=[], current=None)
        video = _make_session(
            session_type="video",
            queue=[_make_track(title="VideoQ1")],
            current=_make_track(title="VideoNow"),
        )
        embed = build_dual_queue_embed(audio, video)
        video_field = next(f for f in embed.fields if f.name == "🎬 Video")
        assert "VideoNow" in video_field.value
        assert "VideoQ1" in video_field.value


# ---------------------------------------------------------------------------
# Unit Tests — QueuePaginationView
# ---------------------------------------------------------------------------


class TestQueuePaginationView:
    def test_prev_disabled_on_page_1(self) -> None:
        session = _make_session(queue=[_make_track() for _ in range(15)])
        view = QueuePaginationView(session, page=1)
        assert view.prev_button.disabled is True

    def test_next_enabled_when_more_pages(self) -> None:
        session = _make_session(queue=[_make_track() for _ in range(15)])
        view = QueuePaginationView(session, page=1)
        assert view.next_button.disabled is False

    def test_next_disabled_on_last_page(self) -> None:
        session = _make_session(queue=[_make_track() for _ in range(15)])
        view = QueuePaginationView(session, page=2)
        assert view.next_button.disabled is True

    def test_prev_enabled_on_page_2(self) -> None:
        session = _make_session(queue=[_make_track() for _ in range(15)])
        view = QueuePaginationView(session, page=2)
        assert view.prev_button.disabled is False

    def test_both_disabled_single_page(self) -> None:
        session = _make_session(queue=[_make_track() for _ in range(5)])
        view = QueuePaginationView(session, page=1)
        assert view.prev_button.disabled is True
        assert view.next_button.disabled is True

    def test_both_disabled_empty_queue(self) -> None:
        session = _make_session(queue=[])
        view = QueuePaginationView(session, page=1)
        assert view.prev_button.disabled is True
        assert view.next_button.disabled is True


# ---------------------------------------------------------------------------
# Property-Based Tests
# ---------------------------------------------------------------------------

# Feature: unified-playback, Property 14: Queue display formatting
# **Validates: Requirements 8.2, 8.3**

# Strategies
positive_durations = st.integers(min_value=1, max_value=100 * 3600 * 1000)  # up to 100 hours
live_durations = st.one_of(
    st.none(),
    st.just(0),
    st.integers(max_value=-1),
)
titles = st.text(min_size=0, max_size=300, alphabet=st.characters(categories=("L", "N", "P", "S", "Z")))
session_types_st = st.sampled_from(["audio", "video"])


@settings(max_examples=100)
@given(
    duration_ms=positive_durations,
)
def test_property_14_duration_format_valid(duration_ms: int) -> None:
    """For any positive duration in milliseconds, format_duration SHALL return
    a string in M:SS format for durations < 1 hour or H:MM:SS for >= 1 hour.

    **Validates: Requirements 8.2, 8.3**
    """
    result = format_duration(duration_ms)
    assert result != "Live"

    parts = result.split(":")
    if duration_ms >= 3600000:
        # H:MM:SS format
        assert len(parts) == 3
        hours, minutes, seconds = int(parts[0]), int(parts[1]), int(parts[2])
        assert hours >= 1
        assert 0 <= minutes <= 59
        assert 0 <= seconds <= 59
        # Verify round-trip (accounting for truncation in ms -> s conversion)
        total_seconds = hours * 3600 + minutes * 60 + seconds
        expected_seconds = duration_ms // 1000
        assert total_seconds == expected_seconds
    else:
        # M:SS format
        assert len(parts) == 2
        minutes, seconds = int(parts[0]), int(parts[1])
        assert minutes >= 0
        assert 0 <= seconds <= 59
        total_seconds = minutes * 60 + seconds
        expected_seconds = duration_ms // 1000
        assert total_seconds == expected_seconds


@settings(max_examples=100)
@given(
    duration=live_durations,
)
def test_property_14_live_duration(duration: int | None) -> None:
    """For any duration that is None, 0, or negative, format_duration SHALL
    return "Live".

    **Validates: Requirements 8.2, 8.3**
    """
    result = format_duration(duration)
    assert result == "Live"


@settings(max_examples=100)
@given(
    title=titles,
    session_type=session_types_st,
    index=st.integers(min_value=1, max_value=1000),
    duration=st.one_of(positive_durations, live_durations),
)
def test_property_14_queue_item_formatting(
    title: str, session_type: str, index: int, duration: int | None
) -> None:
    """For any queue item with arbitrary title and duration, format_queue_item
    SHALL (a) prefix with 🎵 for audio or 🎬 for video, (b) truncate title to
    100 chars if longer, and (c) include formatted duration.

    **Validates: Requirements 8.2, 8.3**
    """
    item = {"title": title, "duration": duration}
    result = format_queue_item(item, session_type, index)

    # (a) Correct prefix emoji
    if session_type == "audio":
        assert "🎵" in result
    else:
        assert "🎬" in result

    # (b) Title truncation — the displayed title is at most 100 chars
    # If the original title is > 100, "..." must appear
    if len(title) > MAX_TITLE_LENGTH:
        assert "..." in result
    # The result should not contain the full untruncated title if it was too long
    if len(title) > MAX_TITLE_LENGTH:
        assert title not in result

    # (c) Duration is included in brackets
    expected_duration = format_duration(duration)
    assert f"[{expected_duration}]" in result

    # Index is present
    assert f"`{index}.`" in result


# Feature: unified-playback, Property 15: Queue pagination
# **Validates: Requirements 8.4**


@settings(max_examples=100)
@given(
    queue_length=st.integers(min_value=0, max_value=200),
    page=st.integers(min_value=1, max_value=50),
)
def test_property_15_pagination(queue_length: int, page: int) -> None:
    """For any queue of length N, the display SHALL show ceil(N / 10) pages,
    with previous button disabled on page 1 and next button disabled on the
    last page.

    **Validates: Requirements 8.4**
    """
    queue = [_make_track(title=f"Track {i}") for i in range(queue_length)]
    session = _make_session(queue=queue, current=_make_track())

    expected_total_pages = max(1, math.ceil(queue_length / ITEMS_PER_PAGE))

    # Clamp page to valid range for the embed test
    clamped_page = max(1, min(page, expected_total_pages))

    embed = build_queue_embed(session, page=page)

    # Footer shows correct page info
    assert embed.footer is not None
    assert f"Page {clamped_page}/{expected_total_pages}" in embed.footer.text  # type: ignore[operator]

    # Pagination view button states
    view = QueuePaginationView(session, page=clamped_page)

    # Previous disabled on page 1
    if clamped_page <= 1:
        assert view.prev_button.disabled is True
    else:
        assert view.prev_button.disabled is False

    # Next disabled on last page
    if clamped_page >= expected_total_pages:
        assert view.next_button.disabled is True
    else:
        assert view.next_button.disabled is False
