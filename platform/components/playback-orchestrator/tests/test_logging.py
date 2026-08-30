"""Verify the orchestrator emits the operational + debug logs we expect.

Untested log statements silently rot; these assert the KEY signals actually
fire at the right level: the playback-api terminal outcome (INFO), the router
pipeline decisions (DEBUG), and the bad-request rejection (WARNING).
"""

from __future__ import annotations

import logging

from hellodj_platform_logic.data_access import SessionTable

from playback_orchestrator.content_filter import ContentFilter
from playback_orchestrator.persistence import SessionStore
from playback_orchestrator.playback_api import PlaybackService
from playback_orchestrator.router import PlaybackRouter
from playback_orchestrator.user_bans import UserBans

from .fakes import FakeTable

_GID = 42
_UID = 7


def _service(**kw) -> PlaybackService:
    store = SessionStore(SessionTable(FakeTable()))
    router = PlaybackRouter(store, **kw)
    return PlaybackService(router, store)


def _body(action: str, **extra):
    base = {
        "action": action,
        "guildId": str(_GID),
        "channelId": "1000",
        "requestedBy": str(_UID),
    }
    base.update(extra)
    return base


def test_enqueue_logs_outcome_at_info(caplog) -> None:
    svc = _service()
    with caplog.at_level(logging.INFO, logger="playback_orchestrator.playback_api"):
        svc.handle(_body("play", query="a song"))
    text = caplog.text
    assert "enqueued" in text
    assert f"guild={_GID}" in text
    assert f"user={_UID}" in text


def test_bad_ids_log_warning(caplog) -> None:
    svc = _service()
    with caplog.at_level(logging.WARNING, logger="playback_orchestrator.playback_api"):
        svc.handle({"action": "play", "guildId": "not-an-int"})
    assert "bad ids" in caplog.text


def test_router_logs_classification_and_enqueue_at_debug(caplog) -> None:
    svc = _service()
    with caplog.at_level(logging.DEBUG, logger="playback_orchestrator.router"):
        svc.handle(_body("play", query="https://youtu.be/xyz"))
    text = caplog.text
    assert "classified" in text
    assert "ENQUEUED" in text


def test_router_logs_filtered_at_debug(caplog) -> None:
    cf = ContentFilter()
    # A keyword rule blocks any title containing "blocked".
    cf.add_rule(_GID, "keyword", "blocked", added_by=1)
    svc = _service(content_filter=cf)
    with caplog.at_level(logging.DEBUG, logger="playback_orchestrator.router"):
        svc.handle(_body("play", query="a blocked track"))
    assert "FILTERED" in caplog.text


def test_router_logs_banned_at_debug(caplog) -> None:
    bans = UserBans()
    bans.ban_user(_GID, _UID, banned_by=1)
    svc = _service(user_bans=bans)
    with caplog.at_level(logging.DEBUG, logger="playback_orchestrator.router"):
        svc.handle(_body("play", query="anything"))
    assert "BANNED" in caplog.text


def test_skip_and_stop_log_at_info(caplog) -> None:
    svc = _service()
    with caplog.at_level(logging.INFO, logger="playback_orchestrator.playback_api"):
        svc.handle(_body("play", query="one"))
        svc.handle(_body("skip"))
        svc.handle(_body("stop"))
    assert "skip guild=" in caplog.text
    assert "stop guild=" in caplog.text
