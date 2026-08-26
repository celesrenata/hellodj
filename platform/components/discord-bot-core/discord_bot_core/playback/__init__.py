"""Playback delegation for discord-bot-core.

bot-core owns no playback logic; it forwards every playback intent to the
``playback-orchestrator`` over a typed HTTP/JSON contract. See
:mod:`discord_bot_core.playback.client`.
"""

from __future__ import annotations

from .client import PlaybackClient, PlaybackError, PlaybackRequest, PlaybackResult

__all__ = [
    "PlaybackClient",
    "PlaybackError",
    "PlaybackRequest",
    "PlaybackResult",
]
