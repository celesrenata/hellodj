"""Bot default-source resolution (task 7, R7.1/R7.3).

The bot's source map must treat an unset default source as ``youtube`` so
playback works out of the box. ``player.DEFAULT_SOURCE`` is the single shared
constant and ``player.resolve_source`` is the resolver used before the source
map lookup.

Requirements: 7.1, 7.3
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

import player  # noqa: E402


def test_default_source_constant_is_youtube() -> None:
    assert player.DEFAULT_SOURCE == "youtube"


@pytest.mark.parametrize("provider", [None, "", "   "])
def test_resolve_source_unset_is_youtube(provider) -> None:
    """An unset/empty source provider resolves to youtube (R7.3)."""
    assert player.resolve_source(provider) == "youtube"


@pytest.mark.parametrize(
    "provider", ["youtube", "youtube_music", "soundcloud", "spotify", "tidal"]
)
def test_resolve_source_explicit_preserved(provider: str) -> None:
    assert player.resolve_source(provider) == provider


def test_source_map_unset_maps_to_youtube_entry() -> None:
    """The bot source map keyed by the resolved default yields the YouTube source.

    Mirrors ``player._resolve_and_play``'s lookup: an unset provider resolves to
    DEFAULT_SOURCE and ``source_map.get(sp, source_map[DEFAULT_SOURCE])`` returns
    the YouTube entry (R7.3).
    """
    from wavelink import TrackSource

    source_map = {
        "youtube": TrackSource.YouTube,
        "youtube_music": TrackSource.YouTubeMusic,
        "soundcloud": TrackSource.SoundCloud,
        "spotify": "spsearch",
        "tidal": "tidal",
    }

    sp = player.resolve_source(None)
    resolved = source_map.get(sp, source_map[player.DEFAULT_SOURCE])
    assert resolved is TrackSource.YouTube
    # An unknown provider also falls back to the YouTube entry via DEFAULT_SOURCE.
    unknown = source_map.get("bogus", source_map[player.DEFAULT_SOURCE])
    assert unknown is TrackSource.YouTube
