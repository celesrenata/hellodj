"""Unified queue display for both audio and video sessions.

Provides embed builders with 🎵/🎬 prefixes, title truncation, duration
formatting, pagination (10 items/page), and dual-queue mode for simultaneous
audio+video sessions in the same channel.

Validates: Requirements 8.1–8.6
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Literal

import discord

if TYPE_CHECKING:
    from bot.playback.session_registry import ChannelSession

__all__ = [
    "QueuePaginationView",
    "build_dual_queue_embed",
    "build_queue_embed",
    "format_duration",
    "format_queue_item",
]

ITEMS_PER_PAGE = 10
MAX_TITLE_LENGTH = 100


def format_duration(ms: int | None) -> str:
    """Format duration in milliseconds to human-readable string.

    Returns:
        - "Live" when duration is None, 0, or negative (live stream indicator)
        - "M:SS" for durations under 1 hour (e.g., "4:23")
        - "H:MM:SS" for durations of 1 hour or more (e.g., "1:05:30")
    """
    if ms is None or ms <= 0:
        return "Live"

    total_seconds = ms // 1000
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60

    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _truncate_title(title: str) -> str:
    """Truncate title to MAX_TITLE_LENGTH characters, appending '...' if truncated."""
    if len(title) <= MAX_TITLE_LENGTH:
        return title
    return title[: MAX_TITLE_LENGTH - 3] + "..."


def format_queue_item(item: dict, session_type: str, index: int) -> str:
    """Format a single queue item line.

    Parameters
    ----------
    item:
        Track dict with at minimum 'title' and 'duration' keys.
    session_type:
        Either "audio" or "video" — determines the prefix emoji.
    index:
        1-based position in the queue (for display numbering).

    Returns
    -------
    A formatted string like "1. 🎵 Song Title [3:45]"
    """
    prefix = "🎵" if session_type == "audio" else "🎬"
    title = _truncate_title(item.get("title", "Unknown"))
    duration = format_duration(item.get("duration"))
    return f"`{index}.` {prefix} {title} `[{duration}]`"


def _total_pages(queue_length: int) -> int:
    """Calculate total number of pages for a given queue length."""
    if queue_length == 0:
        return 1
    return math.ceil(queue_length / ITEMS_PER_PAGE)


def build_queue_embed(
    session: ChannelSession,
    page: int = 1,
) -> discord.Embed:
    """Build the queue embed for a single session.

    Parameters
    ----------
    session:
        The ChannelSession containing queue and current track info.
    page:
        1-based page number for pagination.

    Returns
    -------
    A discord.Embed showing the currently playing track and queued items
    for the requested page.
    """
    session_type = session.session_type
    type_emoji = "🎵" if session_type == "audio" else "🎬"
    type_label = "Audio" if session_type == "audio" else "Video"

    embed = discord.Embed(
        title=f"{type_emoji} {type_label} Queue",
        color=discord.Color.blurple() if session_type == "audio" else discord.Color.red(),
    )

    # Now Playing section
    if session.current:
        current_title = _truncate_title(session.current.get("title", "Unknown"))
        current_duration = format_duration(session.current.get("duration"))
        embed.add_field(
            name="Now Playing",
            value=f"{type_emoji} {current_title} `[{current_duration}]`",
            inline=False,
        )
    else:
        embed.add_field(name="Now Playing", value="Nothing playing", inline=False)

    # Queue section
    queue = session.queue
    total = _total_pages(len(queue))

    # Clamp page to valid range
    page = max(1, min(page, total))

    if not queue:
        embed.add_field(name="Up Next", value="Queue is empty", inline=False)
    else:
        start_idx = (page - 1) * ITEMS_PER_PAGE
        end_idx = start_idx + ITEMS_PER_PAGE
        page_items = queue[start_idx:end_idx]

        lines = []
        for i, item in enumerate(page_items, start=start_idx + 1):
            lines.append(format_queue_item(item, session_type, i))

        embed.add_field(
            name="Up Next",
            value="\n".join(lines),
            inline=False,
        )

    embed.set_footer(text=f"Page {page}/{total} • {len(queue)} track(s) in queue")
    return embed


def build_dual_queue_embed(
    audio_session: ChannelSession,
    video_session: ChannelSession,
    audio_page: int = 1,
    video_page: int = 1,
) -> discord.Embed:
    """Build a combined embed showing both audio and video queues.

    Used when both an audio AND video session are active in the same channel
    (dual-queue mode per Requirement 8.5).

    Parameters
    ----------
    audio_session:
        The audio ChannelSession.
    video_session:
        The video ChannelSession.
    audio_page:
        1-based page number for the audio queue section.
    video_page:
        1-based page number for the video queue section.

    Returns
    -------
    A discord.Embed with separate sections for audio and video queues.
    """
    embed = discord.Embed(
        title="🎶 Playback Queue",
        color=discord.Color.purple(),
    )

    # --- Audio section ---
    _add_session_section(embed, audio_session, "🎵 Audio", audio_page)

    # --- Video section ---
    _add_session_section(embed, video_session, "🎬 Video", video_page)

    return embed


def _add_session_section(
    embed: discord.Embed,
    session: ChannelSession,
    header: str,
    page: int,
) -> None:
    """Add a session's queue as a section in an embed."""
    session_type = session.session_type

    # Now Playing for this section
    if session.current:
        current_title = _truncate_title(session.current.get("title", "Unknown"))
        current_duration = format_duration(session.current.get("duration"))
        type_emoji = "🎵" if session_type == "audio" else "🎬"
        now_playing = f"{type_emoji} {current_title} `[{current_duration}]`"
    else:
        now_playing = "Nothing playing"

    # Queue items for this section
    queue = session.queue
    total = _total_pages(len(queue))
    page = max(1, min(page, total))

    if not queue:
        queue_text = "Queue is empty"
    else:
        start_idx = (page - 1) * ITEMS_PER_PAGE
        end_idx = start_idx + ITEMS_PER_PAGE
        page_items = queue[start_idx:end_idx]

        lines = []
        for i, item in enumerate(page_items, start=start_idx + 1):
            lines.append(format_queue_item(item, session_type, i))
        queue_text = "\n".join(lines)

    section_value = f"**Now:** {now_playing}\n\n{queue_text}\n*Page {page}/{total} • {len(queue)} track(s)*"
    embed.add_field(name=header, value=section_value, inline=False)


class QueuePaginationView(discord.ui.View):
    """Pagination buttons for queue display.

    Provides Previous/Next buttons with proper disabled states at boundaries.
    Supports both single-session and dual-queue modes.
    """

    def __init__(
        self,
        session: ChannelSession,
        *,
        second_session: ChannelSession | None = None,
        page: int = 1,
        audio_page: int = 1,
        video_page: int = 1,
        timeout: float = 120.0,
    ) -> None:
        """Initialize pagination view.

        Parameters
        ----------
        session:
            The primary session to display.
        second_session:
            Optional second session for dual-queue mode.
        page:
            Current page for single-session mode.
        audio_page:
            Current audio page for dual-queue mode.
        video_page:
            Current video page for dual-queue mode.
        timeout:
            How long the view remains interactive (seconds).
        """
        super().__init__(timeout=timeout)
        self._session = session
        self._second_session = second_session
        self._page = page
        self._audio_page = audio_page
        self._video_page = video_page
        self._dual_mode = second_session is not None

        self._update_buttons()

    @property
    def _current_page(self) -> int:
        """Current page in single-session mode."""
        return self._page

    @property
    def _total_pages(self) -> int:
        """Total pages in single-session mode."""
        return _total_pages(len(self._session.queue))

    def _update_buttons(self) -> None:
        """Update button disabled states based on current page."""
        if self._dual_mode:
            # In dual mode, use the combined page logic
            # For simplicity, paginate the session with more items
            audio_total = _total_pages(len(self._session.queue)) if self._session.session_type == "audio" else _total_pages(len(self._second_session.queue))  # type: ignore[union-attr]
            video_total = _total_pages(len(self._second_session.queue)) if self._second_session and self._second_session.session_type == "video" else _total_pages(len(self._session.queue))  # type: ignore[union-attr]

            # Disable prev if both are at page 1
            self.prev_button.disabled = self._audio_page <= 1 and self._video_page <= 1
            # Disable next if both are at last page
            self.next_button.disabled = (
                self._audio_page >= audio_total and self._video_page >= video_total
            )
        else:
            self.prev_button.disabled = self._page <= 1
            self.next_button.disabled = self._page >= self._total_pages

    def _build_embed(self) -> discord.Embed:
        """Build the embed for the current state."""
        if self._dual_mode and self._second_session is not None:
            # Determine which is audio and which is video
            if self._session.session_type == "audio":
                return build_dual_queue_embed(
                    self._session,
                    self._second_session,
                    audio_page=self._audio_page,
                    video_page=self._video_page,
                )
            else:
                return build_dual_queue_embed(
                    self._second_session,
                    self._session,
                    audio_page=self._audio_page,
                    video_page=self._video_page,
                )
        return build_queue_embed(self._session, page=self._page)

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button  # type: ignore[type-arg]
    ) -> None:
        """Navigate to the previous page."""
        if self._dual_mode:
            if self._audio_page > 1:
                self._audio_page -= 1
            if self._video_page > 1:
                self._video_page -= 1
        else:
            self._page = max(1, self._page - 1)

        self._update_buttons()
        await interaction.response.edit_message(
            embed=self._build_embed(), view=self
        )

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button  # type: ignore[type-arg]
    ) -> None:
        """Navigate to the next page."""
        if self._dual_mode:
            audio_total = _total_pages(len(self._session.queue)) if self._session.session_type == "audio" else _total_pages(len(self._second_session.queue))  # type: ignore[union-attr]
            video_total = _total_pages(len(self._second_session.queue)) if self._second_session and self._second_session.session_type == "video" else _total_pages(len(self._session.queue))  # type: ignore[union-attr]
            if self._audio_page < audio_total:
                self._audio_page += 1
            if self._video_page < video_total:
                self._video_page += 1
        else:
            self._page = min(self._total_pages, self._page + 1)

        self._update_buttons()
        await interaction.response.edit_message(
            embed=self._build_embed(), view=self
        )
