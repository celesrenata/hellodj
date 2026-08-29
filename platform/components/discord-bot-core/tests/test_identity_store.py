"""Tests for the CoreTable-backed identity store + identity config wiring.

Verifies the concrete
:class:`~discord_bot_core.identity.store.CoreTableIdentityStore` adapter against
an in-memory DynamoDB ``TableLike`` (mirrors the web-ui ``_FakeTable``): a
web-ui-shaped ``BOTIDENTITY`` item round-trips through ``get_identity_data`` and
``set_apply_status`` writes the applier's status fields while preserving all
other data fields. Also confirms ``BotConfig.from_env`` reads the new identity
envs and does NOT raise when they are absent.
"""

from __future__ import annotations

import asyncio
from typing import Any

from hellodj_platform_logic.data_access import CoreTable

from discord_bot_core.config import BotConfig
from discord_bot_core.identity.applier import BOTIDENTITY_SK, IdentityApplier
from discord_bot_core.identity.store import (
    CoreTableIdentityStore,
    build_identity_store,
)

BUCKET = "hellodj-beta-assets"


# -- in-memory DynamoDB TableLike -------------------------------------------


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeTable:
    """In-memory ``TableLike`` supporting PK/SK access + optimistic-lock puts."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        item = self._items.get(key)
        return {"Item": dict(item)} if item is not None else {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = kwargs["Item"]
        key = (item["PK"], item["SK"])
        condition = kwargs.get("ConditionExpression")
        values = kwargs.get("ExpressionAttributeValues", {})
        existing = self._items.get(key)
        if condition == "attribute_not_exists(PK)" and existing is not None:
            raise _ClientError("ConditionalCheckFailedException")
        if (
            condition == "attribute_not_exists(version)"
            and existing is not None
        ):
            raise _ClientError("ConditionalCheckFailedException")
        if condition == "version = :expected":
            expected = values[":expected"]
            if existing is None or existing.get("version") != expected:
                raise _ClientError("ConditionalCheckFailedException")
        self._items[key] = dict(item)
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        values = kwargs["ExpressionAttributeValues"]
        pk = values[":pk"]
        prefix = values.get(":skp")
        items = [
            dict(it)
            for key, it in self._items.items()
            if key[0] == pk
            and (prefix is None or str(key[1]).startswith(prefix))
        ]
        return {"Items": items}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        self._items.pop((kwargs["Key"]["PK"], kwargs["Key"]["SK"]), None)
        return {}


# -- minimal Discord fakes for the round-trip test --------------------------


class _FakeMember:
    def __init__(self) -> None:
        self.nick: str | None = None

    async def edit(self, *, nick: str) -> None:
        self.nick = nick


class _FakeGuild:
    def __init__(self, gid: int) -> None:
        self.id = gid
        self.me = _FakeMember()


class _FakeHTTP:
    async def request(self, route: Any, *, json: dict[str, Any]) -> dict[str, Any]:
        return {}


class _FakeBot:
    def __init__(self, guilds: dict[int, _FakeGuild]) -> None:
        self._guilds = guilds
        self.http = _FakeHTTP()

    def get_guild(self, gid: int) -> _FakeGuild | None:
        return self._guilds.get(gid)

    @property
    def guilds(self) -> list[_FakeGuild]:
        return list(self._guilds.values())


def _route_factory(method: str, path: str, **params: Any) -> dict[str, Any]:
    return {"method": method, "path": path, "params": params}


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# -- CoreTableIdentityStore adapter -----------------------------------------


class TestCoreTableIdentityStore:
    def test_get_returns_data_mapping_or_none(self):
        core = CoreTable(_FakeTable())
        store = CoreTableIdentityStore(core)

        assert store.get_identity_data("111") is None

        core.update_with_lock(
            "GUILD#111",
            BOTIDENTITY_SK,
            lambda d: {
                **d,
                "nickname": "DJ Vinyl",
                "avatar_present": True,
                "avatar_key": "guild/111/bot-avatar/abc.png",
                "avatar_version": "abc",
                "apply_status": "pending",
            },
            entity_type="GuildBotIdentity",
        )

        data = store.get_identity_data("111")
        assert data is not None
        assert data["nickname"] == "DJ Vinyl"
        assert data["avatar_key"] == "guild/111/bot-avatar/abc.png"

    def test_set_apply_status_writes_status_and_preserves_other_fields(self):
        core = CoreTable(_FakeTable())
        store = CoreTableIdentityStore(core)

        core.update_with_lock(
            "GUILD#111",
            BOTIDENTITY_SK,
            lambda d: {
                **d,
                "nickname": "DJ Vinyl",
                "avatar_present": True,
                "avatar_key": "guild/111/bot-avatar/abc.png",
                "avatar_version": "abc",
                "requested_by": "owner-sub",
                "desired_at": 42,
                "apply_status": "pending",
            },
            entity_type="GuildBotIdentity",
        )

        store.set_apply_status(
            "111",
            status="applied",
            applied_at=1730000000,
            apply_error="",
            applied_version="nick=DJ Vinyl\x1favatar=abc",
        )

        item = core.get("GUILD#111", BOTIDENTITY_SK)
        assert item is not None
        assert item["entityType"] == "GuildBotIdentity"
        data = item["data"]
        # Status fields written.
        assert data["apply_status"] == "applied"
        assert data["applied_at"] == 1730000000
        assert data["apply_error"] == ""
        assert data["applied_version"] == "nick=DJ Vinyl\x1favatar=abc"
        # Other fields preserved untouched.
        assert data["nickname"] == "DJ Vinyl"
        assert data["avatar_present"] is True
        assert data["avatar_key"] == "guild/111/bot-avatar/abc.png"
        assert data["avatar_version"] == "abc"
        assert data["requested_by"] == "owner-sub"
        assert data["desired_at"] == 42

    def test_store_and_applier_round_trip(self):
        """The adapter's get/set integrate with the applier end to end."""
        core = CoreTable(_FakeTable())
        store = CoreTableIdentityStore(core)
        core.update_with_lock(
            "GUILD#1",
            BOTIDENTITY_SK,
            lambda d: {**d, "nickname": "DJ Vinyl"},
            entity_type="GuildBotIdentity",
        )
        guild = _FakeGuild(1)
        applier = IdentityApplier(
            _FakeBot({1: guild}),
            store,
            _FakeS3(),
            avatar_bucket=BUCKET,
            route_factory=_route_factory,
            time_fn=lambda: 1730000000,
        )

        outcome = _run(applier.apply_guild("1"))

        assert outcome.status == "applied"
        assert guild.me.nick == "DJ Vinyl"
        # A re-poll is a no-op now that applied_version round-tripped through the
        # store into the item's data.
        second = _run(applier.apply_guild("1"))
        assert second.changed is False


class _FakeS3:
    def get_object(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise KeyError(kwargs.get("Key"))


def test_build_identity_store_returns_none_without_table_name():
    """Empty table name disables the store without touching boto3/AWS."""
    assert build_identity_store("", "us-east-1") is None


# -- config: new identity envs, no raise when absent ------------------------


class TestBotConfigIdentityEnv:
    def test_reads_identity_envs(self):
        cfg = BotConfig.from_env(
            {
                "HELLODJ_DISCORD_TOKEN_SECRET_ID": "arn:secret:discord",
                "HELLODJ_CORE_TABLE": "hellodj-core",
                "HELLODJ_ASSETS_BUCKET": "hellodj-beta-assets",
                "HELLODJ_IDENTITY_APPLY_INTERVAL_S": "120",
            }
        )
        assert cfg.core_table_name == "hellodj-core"
        assert cfg.assets_bucket == "hellodj-beta-assets"
        assert cfg.identity_apply_interval_s == 120.0

    def test_does_not_raise_when_identity_envs_absent(self):
        cfg = BotConfig.from_env(
            {"HELLODJ_DISCORD_TOKEN_SECRET_ID": "arn:secret:discord"}
        )
        assert cfg.core_table_name == ""
        assert cfg.assets_bucket == ""
        assert cfg.identity_apply_interval_s == 300.0
