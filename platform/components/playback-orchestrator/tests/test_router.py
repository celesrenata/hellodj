"""Unit tests for the PlaybackRouter pipeline."""

from __future__ import annotations

from hellodj_platform_logic.data_access import SessionTable

from playback_orchestrator.content_filter import ContentFilter
from playback_orchestrator.persistence import SessionStore
from playback_orchestrator.router import (
    PlaybackRouter,
    PlayRequest,
    RouteOutcome,
)
from playback_orchestrator.user_bans import UserBans

from .fakes import FakeTable

GUILD = 444
USER = 777


def _store() -> SessionStore:
    return SessionStore(SessionTable(FakeTable()))


def test_enqueue_happy_path() -> None:
    store = _store()
    router = PlaybackRouter(store)
    decision = router.route(
        PlayRequest(guild_id=GUILD, user_id=USER, query="never gonna give you up")
    )
    assert decision.outcome is RouteOutcome.ENQUEUED
    assert decision.queue_length == 1
    assert decision.classification is not None
    assert len(store.get_queue(GUILD)) == 1


def test_banned_user_rejected_before_enqueue() -> None:
    store = _store()
    bans = UserBans()
    bans.ban_user(GUILD, USER, banned_by=1)
    router = PlaybackRouter(store, user_bans=bans)
    decision = router.route(PlayRequest(guild_id=GUILD, user_id=USER, query="song"))
    assert decision.outcome is RouteOutcome.BANNED
    assert decision.classification is None
    assert store.get_queue(GUILD) == []


def test_filtered_content_rejected() -> None:
    store = _store()
    cf = ContentFilter()
    cf.add_rule(GUILD, "keyword", "blocked", added_by=1)
    router = PlaybackRouter(store, content_filter=cf)
    decision = router.route(
        PlayRequest(guild_id=GUILD, user_id=USER, query="a blocked song")
    )
    assert decision.outcome is RouteOutcome.FILTERED
    assert decision.blocked_rule is not None
    assert store.get_queue(GUILD) == []


def test_video_request_routes_to_video_queue() -> None:
    store = _store()
    router = PlaybackRouter(store)
    decision = router.route(
        PlayRequest(
            guild_id=GUILD,
            user_id=USER,
            query="https://cdn.example.com/clip.mp4",
        )
    )
    assert decision.outcome is RouteOutcome.ENQUEUED
    item = store.get_queue(GUILD)[0]
    assert item.content_type == "video"
    assert item.url == "https://cdn.example.com/clip.mp4"


def test_no_ban_no_filter_allows_all() -> None:
    store = _store()
    router = PlaybackRouter(store)
    for i in range(3):
        router.route(PlayRequest(guild_id=GUILD, user_id=USER, query=f"track {i}"))
    assert len(store.get_queue(GUILD)) == 3
