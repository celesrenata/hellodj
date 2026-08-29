"""Unit tests for the discord-bot-core per-guild identity applier + store.

Verifies the bot-side half of the web-ui -> bot cross-process identity handoff
using FAKE seams (no live Discord / S3 / DynamoDB):

* **Nickname applied via a FAKE Discord client** (``guild.me.edit(nick=...)``)
  and **avatar applied via a FAKE REST route** (raw
  ``PATCH /guilds/{guild_id}/members/@me`` through ``bot.http.request``) — the
  base64 data-URI body carries exactly the S3 avatar bytes.
* **``discord.Forbidden`` -> ``apply_status="error"`` + human-readable
  ``apply_error``**, with the applied version NOT advanced so a permission fix
  re-applies on the next poll.
* **``apply_all_pending`` iterates the guild cache.**

The ``CoreTableIdentityStore`` adapter and ``BotConfig`` identity-env wiring are
covered in ``test_identity_store.py``.

discord.py 2.7.1 is installed in the env, so the REAL ``discord.Forbidden`` is
used to prove the applier resolves it lazily and catches it.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import discord  # discord.py 2.7.1 is installed in the env

from discord_bot_core.identity.applier import (
    BOTIDENTITY_SK,
    DesiredIdentity,
    IdentityApplier,
    avatar_data_uri,
    plan_apply,
)

BUCKET = "hellodj-beta-assets"
_PNG = b"\x89PNG\r\n\x1a\n" + b"AVATARBYTES"


# -- fakes ------------------------------------------------------------------


class FakeStore:
    """In-memory identity store keyed by guild id (the DynamoDB stand-in)."""

    def __init__(self, data_by_guild: dict[str, dict[str, Any]] | None = None):
        self.data: dict[str, dict[str, Any]] = dict(data_by_guild or {})
        self.writes: list[dict[str, Any]] = []

    def get_identity_data(self, guild_id: str) -> dict[str, Any] | None:
        d = self.data.get(guild_id)
        return dict(d) if d is not None else None

    def set_apply_status(
        self,
        guild_id: str,
        *,
        status: str,
        applied_at: int,
        apply_error: str,
        applied_version: str,
    ) -> None:
        self.writes.append(
            {
                "guild_id": guild_id,
                "apply_status": status,
                "applied_at": applied_at,
                "apply_error": apply_error,
                "applied_version": applied_version,
            }
        )
        current = self.data.setdefault(guild_id, {})
        current.update(
            apply_status=status,
            applied_at=applied_at,
            apply_error=apply_error,
            applied_version=applied_version,
        )


class FakeS3:
    """FAKE boto3 ``s3`` client returning avatar bytes for ``get_object``."""

    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects = dict(objects or {})
        self.calls: list[dict[str, Any]] = []

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        key = kwargs["Key"]
        if key not in self.objects:
            raise KeyError(key)
        return {"Body": _Body(self.objects[key])}


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeMember:
    """The bot's own ``guild.me`` — records nick edits, or raises Forbidden."""

    def __init__(self, *, forbid: bool = False) -> None:
        self.forbid = forbid
        self.nick: str | None = None

    async def edit(self, *, nick: str) -> None:
        if self.forbid:
            raise _forbidden()
        self.nick = nick


class FakeGuild:
    def __init__(self, guild_id: int, *, forbid_nick: bool = False) -> None:
        self.id = guild_id
        self.me = FakeMember(forbid=forbid_nick)


class FakeHTTP:
    """Fake ``bot.http`` capturing the raw member-avatar PATCH request."""

    def __init__(self, *, forbid: bool = False) -> None:
        self.forbid = forbid
        self.requests: list[tuple[Any, dict[str, Any]]] = []

    async def request(
        self, route: Any, *, json: dict[str, Any]
    ) -> dict[str, Any]:
        self.requests.append((route, json))
        if self.forbid:
            raise _forbidden()
        return {}


class FakeBot:
    """Minimal discord.py client stand-in: ``get_guild`` + ``http`` + guilds."""

    def __init__(self, guilds: dict[int, FakeGuild], http: FakeHTTP) -> None:
        self._guilds = guilds
        self.http = http

    def get_guild(self, gid: int) -> FakeGuild | None:
        return self._guilds.get(gid)

    @property
    def guilds(self) -> list[FakeGuild]:
        return list(self._guilds.values())


class FakeRoute:
    """Captured raw REST route (stands in for ``discord.http.Route``)."""

    def __init__(self, method: str, path: str, **params: Any) -> None:
        self.method = method
        self.path = path
        self.params = params


def _route_factory(method: str, path: str, **params: Any) -> FakeRoute:
    return FakeRoute(method, path, **params)


def _forbidden() -> discord.Forbidden:
    """Build a real ``discord.Forbidden`` with a minimal fake HTTP response."""

    class _Resp:
        status = 403
        reason = "Forbidden"

    return discord.Forbidden(
        _Resp(), {"message": "Missing Permissions", "code": 50013}
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _applier(bot: FakeBot, store: FakeStore, s3: FakeS3) -> IdentityApplier:
    return IdentityApplier(
        bot,
        store,
        s3,
        avatar_bucket=BUCKET,
        route_factory=_route_factory,
        time_fn=lambda: 1730000000,
    )


# -- pure diff/plan logic ---------------------------------------------------


class TestPlanApply:
    def test_no_change_when_already_applied(self):
        desired = DesiredIdentity(
            nickname="DJ", avatar_present=False, avatar_version=""
        )
        applied = DesiredIdentity(
            nickname="DJ",
            avatar_present=False,
            avatar_version="",
            applied_version=desired.desired_version(),
        )
        assert plan_apply(applied).changed is False

    def test_change_fires_on_new_nickname(self):
        out = plan_apply(DesiredIdentity(nickname="DJ Vinyl"))
        assert out.changed is True
        assert out.apply_nickname is True
        assert out.apply_avatar is False

    def test_change_fires_on_avatar(self):
        out = plan_apply(
            DesiredIdentity(
                avatar_present=True, avatar_key="k.png", avatar_version="v"
            )
        )
        assert out.changed is True
        assert out.apply_avatar is True


# -- nickname application (fake Discord) ------------------------------------


class TestApplyNickname:
    def test_sets_nickname_and_writes_applied_status(self):
        store = FakeStore({"1": {"nickname": "DJ Vinyl", "desired_at": 5}})
        guild = FakeGuild(1)
        bot = FakeBot({1: guild}, FakeHTTP())
        applier = _applier(bot, store, FakeS3())

        outcome = _run(applier.apply_guild("1"))

        assert outcome.status == "applied"
        assert guild.me.nick == "DJ Vinyl"
        assert store.data["1"]["apply_status"] == "applied"
        assert store.data["1"]["apply_error"] == ""
        assert store.data["1"]["applied_at"] == 1730000000
        assert store.data["1"]["applied_version"] == DesiredIdentity(
            nickname="DJ Vinyl"
        ).desired_version()

    def test_reapply_is_noop_once_applied(self):
        store = FakeStore({"1": {"nickname": "DJ Vinyl"}})
        bot = FakeBot({1: FakeGuild(1)}, FakeHTTP())
        applier = _applier(bot, store, FakeS3())

        _run(applier.apply_guild("1"))
        writes_after_first = len(store.writes)
        second = _run(applier.apply_guild("1"))

        assert second.changed is False
        assert len(store.writes) == writes_after_first


# -- avatar application (fake REST route + fake S3) -------------------------


class TestApplyAvatar:
    def test_patches_member_avatar_with_data_uri_from_s3(self):
        key = "guild/1/bot-avatar/abc.png"
        store = FakeStore(
            {
                "1": {
                    "avatar_present": True,
                    "avatar_key": key,
                    "avatar_version": "abc",
                }
            }
        )
        http = FakeHTTP()
        bot = FakeBot({1: FakeGuild(1)}, http)
        applier = _applier(bot, store, FakeS3({key: _PNG}))

        outcome = _run(applier.apply_guild("1"))

        assert outcome.status == "applied"
        assert len(http.requests) == 1
        route, payload = http.requests[0]
        assert route.method == "PATCH"
        assert route.path == "/guilds/{guild_id}/members/@me"
        assert route.params == {"guild_id": "1"}
        assert payload["avatar"] == avatar_data_uri(_PNG, key)
        b64 = base64.b64encode(_PNG).decode("ascii")
        assert payload["avatar"] == f"data:image/png;base64,{b64}"

    def test_applies_both_nickname_and_avatar(self):
        key = "guild/1/bot-avatar/abc.png"
        store = FakeStore(
            {
                "1": {
                    "nickname": "DJ",
                    "avatar_present": True,
                    "avatar_key": key,
                    "avatar_version": "abc",
                }
            }
        )
        http = FakeHTTP()
        guild = FakeGuild(1)
        bot = FakeBot({1: guild}, http)
        applier = _applier(bot, store, FakeS3({key: _PNG}))

        outcome = _run(applier.apply_guild("1"))

        assert outcome.status == "applied"
        assert guild.me.nick == "DJ"
        assert len(http.requests) == 1


# -- discord.Forbidden -> clear error ---------------------------------------


class TestForbiddenClearError:
    def test_nickname_forbidden_records_error_and_no_version_advance(self):
        store = FakeStore(
            {"1": {"nickname": "DJ Vinyl", "applied_version": ""}}
        )
        guild = FakeGuild(1, forbid_nick=True)
        bot = FakeBot({1: guild}, FakeHTTP())
        applier = _applier(bot, store, FakeS3())

        outcome = _run(applier.apply_guild("1"))

        assert outcome.status == "error"
        assert "Cannot set nickname" in store.data["1"]["apply_error"]
        assert store.data["1"]["apply_status"] == "error"
        # applied_version NOT advanced -> a permission fix re-applies next poll.
        assert store.data["1"]["applied_version"] == ""
        assert store.data["1"]["applied_at"] == 0

    def test_avatar_forbidden_records_error(self):
        key = "guild/1/bot-avatar/abc.png"
        store = FakeStore(
            {
                "1": {
                    "avatar_present": True,
                    "avatar_key": key,
                    "avatar_version": "abc",
                    "applied_version": "",
                }
            }
        )
        http = FakeHTTP(forbid=True)
        bot = FakeBot({1: FakeGuild(1)}, http)
        applier = _applier(bot, store, FakeS3({key: _PNG}))

        outcome = _run(applier.apply_guild("1"))

        assert outcome.status == "error"
        assert "Cannot set avatar" in store.data["1"]["apply_error"]
        assert store.data["1"]["applied_version"] == ""

    def test_forbidden_is_a_real_discord_forbidden(self):
        exc = _forbidden()
        assert isinstance(exc, discord.Forbidden)
        assert exc.status == 403


# -- guild-not-in-cache + apply_all_pending ---------------------------------


class TestApplierPolling:
    def test_guild_not_in_cache_leaves_pending_no_error(self):
        store = FakeStore({"7": {"nickname": "DJ"}})
        bot = FakeBot({}, FakeHTTP())  # bot is in no guilds
        applier = _applier(bot, store, FakeS3())

        outcome = _run(applier.apply_guild("7"))

        assert outcome.changed is False
        assert store.writes == []

    def test_apply_all_pending_iterates_guild_cache(self):
        store = FakeStore(
            {
                "1": {"nickname": "One"},
                "2": {"nickname": "Two"},
            }
        )
        bot = FakeBot({1: FakeGuild(1), 2: FakeGuild(2)}, FakeHTTP())
        applier = _applier(bot, store, FakeS3())

        results = _run(applier.apply_all_pending())

        assert set(results) == {"1", "2"}
        assert results["1"].status == "applied"
        assert results["2"].status == "applied"


def test_botidentity_sk_matches_web_ui_writer():
    """The applier and the web-ui writer must agree on the sort key."""
    assert BOTIDENTITY_SK == "BOTIDENTITY"
