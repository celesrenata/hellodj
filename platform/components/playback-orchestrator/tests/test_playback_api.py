"""Tests for the orchestrator's playback HTTP API mapping (pure, no HTTP).

Drives :class:`PlaybackService` / :func:`handle_playback` against an in-memory
``SessionStore`` and asserts the request→response mapping matches what the bot's
``PlaybackClient.PlaybackResult.from_payload`` expects (``{ok, message, data}``)
for each action, plus the degraded (unconfigured) path.
"""

from __future__ import annotations

from hellodj_platform_logic.data_access import SessionTable

from playback_orchestrator.content_filter import ContentFilter
from playback_orchestrator.persistence import QueueItem, SessionState, SessionStore
from playback_orchestrator.playback_api import (
    PLAYBACK_ROUTE,
    PlaybackService,
    handle_playback,
)
from playback_orchestrator.router import PlaybackRouter
from playback_orchestrator.user_bans import UserBans

from .fakes import FakeTable

_GID = 42
_UID = 7


def _service(
    *, content_filter: ContentFilter | None = None, user_bans: UserBans | None = None
) -> tuple[PlaybackService, SessionStore]:
    store = SessionStore(SessionTable(FakeTable()))
    router = PlaybackRouter(store, content_filter=content_filter, user_bans=user_bans)
    return PlaybackService(router, store), store


def _body(action: str, **extra: object) -> dict[str, object]:
    base: dict[str, object] = {
        "action": action,
        "guildId": str(_GID),
        "channelId": "1000",
        "requestedBy": str(_UID),
    }
    base.update(extra)
    return base


def test_route_constant_matches_bot_client_path():
    # The bot's PlaybackClient posts to {base}/v1/playback.
    assert PLAYBACK_ROUTE == "/v1/playback"


def test_play_enqueues_and_returns_ok():
    svc, store = _service()
    resp = svc.handle(_body("play", query="never gonna give you up"))
    assert resp["ok"] is True
    assert resp["data"]["outcome"] == "enqueued"
    assert resp["data"]["queueLength"] == 1
    # Actually persisted.
    assert [i.title for i in store.get_queue(_GID)] == ["never gonna give you up"]


def test_play_without_query_fails():
    svc, _store = _service()
    resp = svc.handle(_body("play", query="   "))
    assert resp["ok"] is False
    assert "query" in resp["message"].lower() or "nothing" in resp["message"].lower()


def test_play_passes_source_hint_through():
    svc, _store = _service()
    resp = svc.handle(_body("play", query="song", source="spotify"))
    assert resp["data"]["source"] == "spotify"


def test_banned_user_is_rejected():
    bans = UserBans()
    bans.ban_user(_GID, _UID, banned_by=1)
    svc, store = _service(user_bans=bans)
    resp = svc.handle(_body("play", query="song"))
    assert resp["ok"] is False
    assert store.get_queue(_GID) == []  # nothing enqueued


def test_content_filter_blocks():
    cf = ContentFilter()
    cf.add_rule(_GID, "keyword", "blocked", added_by=1)
    svc, store = _service(content_filter=cf)
    resp = svc.handle(_body("play", query="a blocked song"))
    assert resp["ok"] is False
    assert store.get_queue(_GID) == []


def test_skip_pops_head():
    svc, store = _service()
    store.enqueue(_GID, QueueItem(title="A", url="u1"))
    store.enqueue(_GID, QueueItem(title="B", url="u2"))
    resp = svc.handle(_body("skip"))
    assert resp["ok"] is True
    assert resp["data"]["skipped"] == "A"
    assert [i.title for i in store.get_queue(_GID)] == ["B"]


def test_skip_empty_queue_is_ok_message():
    svc, _store = _service()
    resp = svc.handle(_body("skip"))
    assert resp["ok"] is True
    assert "empty" in resp["message"].lower()


def test_stop_clears_queue():
    svc, store = _service()
    store.enqueue(_GID, QueueItem(title="A", url="u1"))
    resp = svc.handle(_body("stop"))
    assert resp["ok"] is True
    assert store.get_queue(_GID) == []


def test_queue_snapshot():
    svc, store = _service()
    store.enqueue(_GID, QueueItem(title="A", url="u1"))
    store.enqueue(_GID, QueueItem(title="B", url="u2"))
    resp = svc.handle(_body("queue"))
    assert resp["ok"] is True
    assert resp["data"]["queue"] == ["A", "B"]


def test_now_playing_reads_session_current():
    svc, store = _service()
    store.save_session(
        _GID, SessionState(current={"title": "Song X"})
    )
    resp = svc.handle(_body("now_playing"))
    assert resp["ok"] is True
    assert resp["data"]["current"]["title"] == "Song X"


def test_now_playing_when_idle():
    svc, _store = _service()
    resp = svc.handle(_body("now_playing"))
    assert resp["ok"] is True
    assert "nothing" in resp["message"].lower()


def test_pause_resume_acknowledged():
    svc, _store = _service()
    assert svc.handle(_body("pause"))["ok"] is True
    assert svc.handle(_body("resume"))["ok"] is True


def test_unknown_action_fails():
    svc, _store = _service()
    resp = svc.handle(_body("teleport"))
    assert resp["ok"] is False
    assert "unsupported" in resp["message"].lower()


def test_bad_ids_fail_cleanly():
    svc, _store = _service()
    resp = svc.handle({"action": "play", "guildId": "nope", "requestedBy": "x"})
    assert resp["ok"] is False


def test_handle_playback_degraded_when_service_none():
    resp = handle_playback(None, _body("play", query="song"))
    assert resp["ok"] is False
    assert "not configured" in resp["message"].lower()


def test_handle_playback_catches_service_errors():
    class _Boom:
        def handle(self, body: dict[str, object]) -> dict[str, object]:
            raise RuntimeError("kaboom")

    resp = handle_playback(_Boom(), _body("play", query="song"))
    assert resp["ok"] is False
    assert resp["data"] == {}
