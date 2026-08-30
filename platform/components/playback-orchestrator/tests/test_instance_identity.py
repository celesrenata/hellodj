"""Tests for the per-bot identity applier (name + avatar) for pool bots.

Covers the pure diff/version logic and the side-effecting applier against fakes
(a fake identity store, a fake S3 reader, and a fake connected discord client
whose ``user.edit`` records the applied kwargs). No real discord.py / boto3.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from playback_orchestrator.instance_identity import (
    DesiredBotIdentity,
    InstanceIdentityApplier,
    botidentity_sk,
    plan_bot_identity_apply,
)


# ── pure logic ───────────────────────────────────────────────────────────────


def test_botidentity_sk_legacy_and_per_bot():
    assert botidentity_sk() == "BOTIDENTITY"
    assert botidentity_sk("") == "BOTIDENTITY"
    assert botidentity_sk("999") == "BOTIDENTITY#999"


def test_plan_no_change_when_versions_match():
    desired = DesiredBotIdentity(
        nickname="DJ Cat",
        avatar_present=True,
        avatar_key="guild/1/bot/9/bot-avatar/abc.png",
        avatar_version="abc",
        applied_version="name=DJ Cat\x1favatar=abc",
    )
    outcome = plan_bot_identity_apply(desired)
    assert outcome.changed is False
    assert outcome.apply_name is False
    assert outcome.apply_avatar is False


def test_plan_applies_name_and_avatar_on_change():
    desired = DesiredBotIdentity(
        nickname="DJ Cat",
        avatar_present=True,
        avatar_key="guild/1/bot/9/bot-avatar/abc.png",
        avatar_version="abc",
        applied_version="",  # nothing applied yet
    )
    outcome = plan_bot_identity_apply(desired)
    assert outcome.changed is True
    assert outcome.apply_name is True
    assert outcome.apply_avatar is True
    assert outcome.applied_version == "name=DJ Cat\x1favatar=abc"


def test_plan_name_only_when_no_avatar():
    desired = DesiredBotIdentity(nickname="DJ Cat", applied_version="")
    outcome = plan_bot_identity_apply(desired)
    assert outcome.changed is True
    assert outcome.apply_name is True
    assert outcome.apply_avatar is False


# ── fakes ─────────────────────────────────────────────────────────────────────


class _FakeStore:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = data
        self.writes: list[dict[str, Any]] = []

    def get_identity_data(self, guild_id: str, *, client_id: str):
        return self._data

    def set_apply_status(self, guild_id: str, *, client_id: str, **kwargs: Any):
        self.writes.append({"guild_id": guild_id, "client_id": client_id, **kwargs})


class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeS3:
    def __init__(self, data: bytes = b"\x89PNG\r\n\x1a\n", *, fail: bool = False):
        self._data = data
        self._fail = fail
        self.requested_key: str | None = None

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.requested_key = kwargs.get("Key")
        if self._fail:
            raise RuntimeError("s3 boom")
        return {"Body": _FakeBody(self._data)}


class _FakeUser:
    def __init__(self, *, fail: bool = False) -> None:
        self.edits: list[dict[str, Any]] = []
        self._fail = fail

    async def edit(self, **kwargs: Any) -> None:
        if self._fail:
            raise RuntimeError("discord rejected")
        self.edits.append(kwargs)


class _FakeClient:
    def __init__(self, user: Any) -> None:
        self.user = user


# ── applier ───────────────────────────────────────────────────────────────────


def _applier(store: _FakeStore, s3: _FakeS3) -> InstanceIdentityApplier:
    return InstanceIdentityApplier(
        store, s3, avatar_bucket="assets-bucket", time_fn=lambda: 1234
    )


def test_apply_instance_sets_username_and_avatar_and_writes_applied():
    store = _FakeStore(
        {
            "nickname": "DJ Cat",
            "avatar_present": True,
            "avatar_key": "guild/1/bot/9/bot-avatar/abc.png",
            "avatar_version": "abc",
            "applied_version": "",
        }
    )
    s3 = _FakeS3(b"\x89PNG\r\n\x1a\n-bytes")
    user = _FakeUser()
    applier = _applier(store, s3)

    outcome = asyncio.run(
        applier.apply_instance(_FakeClient(user), "1", "9")
    )

    assert outcome.changed is True
    assert user.edits == [{"username": "DJ Cat", "avatar": b"\x89PNG\r\n\x1a\n-bytes"}]
    assert s3.requested_key == "guild/1/bot/9/bot-avatar/abc.png"
    assert store.writes[-1]["status"] == "applied"
    assert store.writes[-1]["applied_at"] == 1234
    assert store.writes[-1]["applied_version"] == "name=DJ Cat\x1favatar=abc"
    assert store.writes[-1]["client_id"] == "9"


def test_apply_instance_noop_when_already_applied():
    store = _FakeStore(
        {
            "nickname": "DJ Cat",
            "avatar_present": False,
            "avatar_version": "",
            "applied_version": "name=DJ Cat\x1favatar=",
        }
    )
    user = _FakeUser()
    applier = _applier(store, _FakeS3())

    outcome = asyncio.run(applier.apply_instance(_FakeClient(user), "1", "9"))

    assert outcome.changed is False
    assert user.edits == []
    assert store.writes == []  # nothing written when unchanged


def test_apply_instance_skips_when_client_not_ready():
    store = _FakeStore({"nickname": "DJ Cat", "applied_version": ""})
    applier = _applier(store, _FakeS3())

    outcome = asyncio.run(applier.apply_instance(_FakeClient(None), "1", "9"))

    assert outcome.changed is False
    assert store.writes == []  # left pending for a later pass


def test_apply_instance_records_error_and_does_not_advance_version():
    store = _FakeStore(
        {"nickname": "DJ Cat", "avatar_present": False, "applied_version": ""}
    )
    user = _FakeUser(fail=True)
    applier = _applier(store, _FakeS3())

    outcome = asyncio.run(applier.apply_instance(_FakeClient(user), "1", "9"))

    assert outcome.status == "error"
    assert store.writes[-1]["status"] == "error"
    assert store.writes[-1]["applied_version"] == ""  # not advanced → retry
    assert "identity" in store.writes[-1]["apply_error"].lower()


def test_apply_instance_avatar_read_failure_is_recorded():
    store = _FakeStore(
        {
            "nickname": "DJ Cat",
            "avatar_present": True,
            "avatar_key": "guild/1/bot/9/bot-avatar/abc.png",
            "avatar_version": "abc",
            "applied_version": "",
        }
    )
    user = _FakeUser()
    applier = _applier(store, _FakeS3(fail=True))

    outcome = asyncio.run(applier.apply_instance(_FakeClient(user), "1", "9"))

    # Name still applied (avatar read failed), but overall marked error so the
    # version is not advanced and a corrected retry re-applies.
    assert user.edits == [{"username": "DJ Cat"}]
    assert outcome.status == "error"
    assert "avatar" in store.writes[-1]["apply_error"].lower()
