"""Unit tests for the single-writer session/queue persistence layer."""

from __future__ import annotations

from hellodj_platform_logic.data_access import SessionTable

from playback_orchestrator.persistence import QueueItem, SessionState, SessionStore

from .fakes import FakeTable

GUILD = 333


def _store() -> SessionStore:
    return SessionStore(SessionTable(FakeTable()))


def test_session_round_trip() -> None:
    store = _store()
    assert store.get_session(GUILD) is None
    state = SessionState(voice_channel_id=10, text_channel_id=20, source_provider="tidal")
    store.save_session(GUILD, state)
    loaded = store.get_session(GUILD)
    assert loaded is not None
    assert loaded.voice_channel_id == 10
    assert loaded.source_provider == "tidal"


def test_update_session_read_modify_write() -> None:
    store = _store()
    store.save_session(GUILD, SessionState(repeat_mode="off"))

    def flip(state: SessionState) -> SessionState:
        state.repeat_mode = "all"
        return state

    result = store.update_session(GUILD, flip)
    assert result.repeat_mode == "all"
    assert store.get_session(GUILD).repeat_mode == "all"  # type: ignore[union-attr]


def test_enqueue_and_get_queue() -> None:
    store = _store()
    assert store.get_queue(GUILD) == []
    store.enqueue(GUILD, QueueItem(title="A", url="u1"))
    queue = store.enqueue(GUILD, QueueItem(title="B", url="u2"))
    assert [item.title for item in queue] == ["A", "B"]
    assert [item.title for item in store.get_queue(GUILD)] == ["A", "B"]


def test_dequeue_fifo() -> None:
    store = _store()
    store.enqueue(GUILD, QueueItem(title="first", url="u1"))
    store.enqueue(GUILD, QueueItem(title="second", url="u2"))
    popped = store.dequeue(GUILD)
    assert popped is not None
    assert popped.title == "first"
    assert [item.title for item in store.get_queue(GUILD)] == ["second"]


def test_dequeue_empty_returns_none() -> None:
    store = _store()
    assert store.dequeue(GUILD) is None


def test_set_and_clear_queue() -> None:
    store = _store()
    store.set_queue(GUILD, [QueueItem(title="X", url="ux")])
    assert len(store.get_queue(GUILD)) == 1
    store.clear_queue(GUILD)
    assert store.get_queue(GUILD) == []


def test_version_increments_on_write() -> None:
    fake = FakeTable()
    store = SessionStore(SessionTable(fake))
    store.enqueue(GUILD, QueueItem(title="A", url="u1"))
    store.enqueue(GUILD, QueueItem(title="B", url="u2"))
    # After two writes the stored version should be 2 (single-writer serialized).
    item = fake.get_item(Key={"PK": f"GUILD#{GUILD}", "SK": "QUEUE"})["Item"]
    assert item["version"] == 2
