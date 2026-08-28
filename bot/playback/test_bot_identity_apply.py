"""Unit tests for the bot-side per-guild identity applier.

Task 7.3 (bot half) of the ``bot-identity-and-source-auth`` bugfix spec
(Change area F). Verifies ``bot/bot_identity_apply.py`` — the OTHER half of the
web-ui → bot cross-process identity handoff — using FAKE seams (no live Discord /
S3 / DynamoDB), matching the ``FakeSecrets`` style of ``test_guild_credentials``:

* **Nickname applied via a FAKE Discord client** (``guild.me.edit(nick=...)``)
  and **avatar applied via a FAKE REST route** (raw
  ``PATCH /guilds/{guild_id}/members/@me`` through ``bot.http.request``) — the
  base64 data-URI body carries exactly the S3 avatar bytes (R2.7, R2.8).
* **``discord.Forbidden`` → ``apply_status="error"`` + human-readable
  ``apply_error``** clear-error path, and the applied version is NOT advanced so
  a permission fix re-applies on the next poll (R2.9).
* **``apply_status`` flows back to the item the UI reads** — the applier writes
  ``applied`` / ``error`` onto the store the web-ui ``BotIdentityService`` reads.
* Change-only application (idempotent poll): an already-applied identity is a
  no-op.

``bot_identity_apply.py`` lives in ``bot/`` (the parent of ``bot/playback``), so
the parent dir is put on ``sys.path`` here (mirrors ``bot/cogs/test_admin_panel``);
the bot playback ``pytest`` is invoked from ``bot/playback``.

The real ``discord.Forbidden`` (discord.py 2.7.1 is installed) is used to prove
the applier resolves it lazily and catches it.

Requirements: 2.7, 2.8, 2.9
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
from typing import Any

# bot/playback/ -> bot/ so ``import bot_identity_apply`` resolves.
_BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BOT_DIR not in sys.path:
    sys.path.insert(0, _BOT_DIR)

import discord  # discord.py 2.7.1 is installed in the env
from bot_identity_apply import (
    BOTIDENTITY_SK,
    ApplyOutcome,
    DesiredIdentity,
    IdentityApplier,
    avatar_data_uri,
    filter_by_entitlements,
    plan_apply,
)

BUCKET = "hellodj-beta-assets"
_PNG = b"\x89PNG\r\n\x1a\n" + b"AVATARBYTES"


# ── fakes ──────────────────────────────────────────────────────────────────


class FakeStore:
    """In-memory identity store keyed by guild id (the DynamoDB stand-in).

    Holds the web-ui-written ``data`` mapping and captures the applier's
    writeback so tests can assert what the UI would later read.
    """

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
        record = {
            "guild_id": guild_id,
            "apply_status": status,
            "applied_at": applied_at,
            "apply_error": apply_error,
            "applied_version": applied_version,
        }
        self.writes.append(record)
        # Merge back onto the stored data so a re-read reflects the writeback
        # (models the DynamoDB item the UI reads via BotIdentityService).
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

    async def request(self, route: Any, *, json: dict[str, Any]) -> dict[str, Any]:
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


class FakeResolver:
    """Fake :class:`UserEntitlementResolver` for the identity gate (R4.3/R4.4).

    ``effective_for_sub`` returns a per-sub effective-entitlement mapping so a
    test can permit or withhold the ``custom_avatar`` / ``custom_name`` flags.
    Defaults to permissive (both flags on) so the pre-existing apply-path tests
    exercise application; the gate-specific tests pass a restrictive mapping.
    """

    def __init__(self, effective_by_sub: dict[str, dict[str, Any]] | None = None,
                 *, default: dict[str, Any] | None = None) -> None:
        self._by_sub = dict(effective_by_sub or {})
        self._default = default if default is not None else {
            "custom_avatar": True, "custom_name": True,
        }
        self.calls: list[str] = []

    def effective_for_sub(self, sub: str) -> dict[str, Any]:
        self.calls.append(sub)
        return dict(self._by_sub.get(sub, self._default))


class RaisingResolver:
    """Fake resolver whose ``effective_for_sub`` always raises.

    Models a datastore/lookup failure so the gate must fail safe and withhold
    BOTH gated fields (R4.3/R4.4, restrictive-default convention).
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def effective_for_sub(self, sub: str) -> dict[str, Any]:
        self.calls.append(sub)
        raise RuntimeError("datastore unavailable")


#: Cognito sub stamped onto the apply-path fixtures as the identity owner so the
#: gate resolves a (permissive) entitlement set for them.
_OWNER = "owner-sub"


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

    return discord.Forbidden(_Resp(), {"message": "Missing Permissions", "code": 50013})


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _applier(
    bot: FakeBot,
    store: FakeStore,
    s3: FakeS3,
    *,
    resolver: Any | None = None,
) -> IdentityApplier:
    return IdentityApplier(
        bot,
        store,
        s3,
        avatar_bucket=BUCKET,
        route_factory=_route_factory,
        entitlement_resolver=resolver if resolver is not None else FakeResolver(),
        time_fn=lambda: 1730000000,
    )


# ── pure diff/plan logic ────────────────────────────────────────────────────


class TestPlanApply:
    def test_no_change_when_already_applied(self):
        desired = DesiredIdentity(
            nickname="DJ", avatar_present=False, avatar_version=""
        )
        # applied_version equals the current desired version → no-op.
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
            DesiredIdentity(avatar_present=True, avatar_key="k.png",
                            avatar_version="v")
        )
        assert out.changed is True
        assert out.apply_avatar is True


# ── nickname application (fake Discord) ─────────────────────────────────────


class TestApplyNickname:
    def test_sets_nickname_and_writes_applied_status(self):
        store = FakeStore(
            {"1": {"nickname": "DJ Vinyl", "desired_at": 5,
                   "requested_by": _OWNER}}
        )
        guild = FakeGuild(1)
        bot = FakeBot({1: guild}, FakeHTTP())
        s3 = FakeS3()
        applier = _applier(bot, store, s3)

        outcome = _run(applier.apply_guild("1"))

        assert outcome.status == "applied"
        assert guild.me.nick == "DJ Vinyl"
        # apply_status flowed back to the item the UI reads.
        assert store.data["1"]["apply_status"] == "applied"
        assert store.data["1"]["apply_error"] == ""
        assert store.data["1"]["applied_at"] == 1730000000
        # applied_version advanced so a re-poll is a no-op (idempotent).
        assert store.data["1"]["applied_version"] == DesiredIdentity(
            nickname="DJ Vinyl"
        ).desired_version()

    def test_reapply_is_noop_once_applied(self):
        store = FakeStore({"1": {"nickname": "DJ Vinyl", "requested_by": _OWNER}})
        guild = FakeGuild(1)
        bot = FakeBot({1: guild}, FakeHTTP())
        applier = _applier(bot, store, FakeS3())

        _run(applier.apply_guild("1"))
        writes_after_first = len(store.writes)
        second = _run(applier.apply_guild("1"))

        assert second.changed is False
        # No new writeback on the idempotent second pass.
        assert len(store.writes) == writes_after_first


# ── avatar application (fake REST route + fake S3) ──────────────────────────


class TestApplyAvatar:
    def test_patches_member_avatar_with_data_uri_from_s3(self):
        key = "guild/1/bot-avatar/abc.png"
        store = FakeStore(
            {"1": {"avatar_present": True, "avatar_key": key,
                   "avatar_version": "abc", "requested_by": _OWNER}}
        )
        http = FakeHTTP()
        bot = FakeBot({1: FakeGuild(1)}, http)
        s3 = FakeS3({key: _PNG})
        applier = _applier(bot, store, s3)

        outcome = _run(applier.apply_guild("1"))

        assert outcome.status == "applied"
        # S3 read from the stage-scoped bucket at the stored key.
        assert s3.calls == [{"Bucket": BUCKET, "Key": key}]
        # Exactly one raw REST PATCH to the member-avatar endpoint.
        assert len(http.requests) == 1
        route, payload = http.requests[0]
        assert route.method == "PATCH"
        assert route.path == "/guilds/{guild_id}/members/@me"
        assert route.params == {"guild_id": "1"}
        # The data URI carries exactly the S3 bytes, base64-encoded.
        assert payload["avatar"] == avatar_data_uri(_PNG, key)
        b64 = base64.b64encode(_PNG).decode("ascii")
        assert payload["avatar"] == f"data:image/png;base64,{b64}"

    def test_applies_both_nickname_and_avatar(self):
        key = "guild/1/bot-avatar/abc.png"
        store = FakeStore(
            {"1": {"nickname": "DJ", "avatar_present": True,
                   "avatar_key": key, "avatar_version": "abc",
                   "requested_by": _OWNER}}
        )
        http = FakeHTTP()
        guild = FakeGuild(1)
        bot = FakeBot({1: guild}, http)
        applier = _applier(bot, store, FakeS3({key: _PNG}))

        outcome = _run(applier.apply_guild("1"))

        assert outcome.status == "applied"
        assert guild.me.nick == "DJ"
        assert len(http.requests) == 1


# ── discord.Forbidden → clear error (R2.9) ──────────────────────────────────


class TestForbiddenClearError:
    def test_nickname_forbidden_records_error_and_no_version_advance(self):
        store = FakeStore(
            {"1": {"nickname": "DJ Vinyl", "applied_version": "",
                   "requested_by": _OWNER}}
        )
        guild = FakeGuild(1, forbid_nick=True)
        bot = FakeBot({1: guild}, FakeHTTP())
        applier = _applier(bot, store, FakeS3())

        outcome = _run(applier.apply_guild("1"))

        assert outcome.status == "error"
        # Human-readable message, surfaced to the UI (R2.9).
        assert "Cannot set nickname" in store.data["1"]["apply_error"]
        assert store.data["1"]["apply_status"] == "error"
        # applied_version NOT advanced → a permission fix re-applies next poll.
        assert store.data["1"]["applied_version"] == ""
        assert store.data["1"]["applied_at"] == 0

    def test_avatar_forbidden_records_error(self):
        key = "guild/1/bot-avatar/abc.png"
        store = FakeStore(
            {"1": {"avatar_present": True, "avatar_key": key,
                   "avatar_version": "abc", "applied_version": "",
                   "requested_by": _OWNER}}
        )
        http = FakeHTTP(forbid=True)
        bot = FakeBot({1: FakeGuild(1)}, http)
        applier = _applier(bot, store, FakeS3({key: _PNG}))

        outcome = _run(applier.apply_guild("1"))

        assert outcome.status == "error"
        assert "Cannot set avatar" in store.data["1"]["apply_error"]
        assert store.data["1"]["applied_version"] == ""

    def test_forbidden_is_a_real_discord_forbidden(self):
        """Sanity: the applier catches the REAL discord.Forbidden lazily."""
        exc = _forbidden()
        assert isinstance(exc, discord.Forbidden)
        assert exc.status == 403


# ── guild-not-in-cache + apply_all_pending ──────────────────────────────────


class TestApplierPolling:
    def test_guild_not_in_cache_leaves_pending_no_error(self):
        """A guild the bot is not in is skipped (retry later), not an error."""
        store = FakeStore({"7": {"nickname": "DJ"}})
        bot = FakeBot({}, FakeHTTP())  # bot is in no guilds
        applier = _applier(bot, store, FakeS3())

        outcome = _run(applier.apply_guild("7"))

        assert outcome.changed is False
        # No status writeback (left pending for a later poll / on_guild_join).
        assert store.writes == []

    def test_apply_all_pending_iterates_guild_cache(self):
        store = FakeStore(
            {
                "1": {"nickname": "One", "requested_by": _OWNER},
                "2": {"nickname": "Two", "requested_by": _OWNER},
            }
        )
        bot = FakeBot({1: FakeGuild(1), 2: FakeGuild(2)}, FakeHTTP())
        applier = _applier(bot, store, FakeS3())

        results = _run(applier.apply_all_pending())

        assert set(results) == {"1", "2"}
        assert results["1"].status == "applied"
        assert results["2"].status == "applied"


# ── entitlement gating (admin-entitlements-panel R4.3, R4.4) ────────────────


class TestFilterByEntitlementsPure:
    """The pure gate: withhold gated fields per the owner's effective flags."""

    def test_both_applied_when_both_flags_on(self):
        out = ApplyOutcome(changed=True, apply_nickname=True, apply_avatar=True,
                           applied_version="v")
        gated = filter_by_entitlements(
            out, {"custom_avatar": True, "custom_name": True}
        )
        assert gated.apply_nickname is True
        assert gated.apply_avatar is True
        # Input is not mutated (side-effect free).
        assert out.apply_nickname is True and out.apply_avatar is True

    def test_avatar_withheld_when_custom_avatar_off(self):
        out = ApplyOutcome(changed=True, apply_nickname=True, apply_avatar=True)
        gated = filter_by_entitlements(
            out, {"custom_avatar": False, "custom_name": True}
        )
        assert gated.apply_avatar is False  # R4.3
        assert gated.apply_nickname is True

    def test_nickname_withheld_when_custom_name_off(self):
        out = ApplyOutcome(changed=True, apply_nickname=True, apply_avatar=True)
        gated = filter_by_entitlements(
            out, {"custom_avatar": True, "custom_name": False}
        )
        assert gated.apply_nickname is False  # R4.4
        assert gated.apply_avatar is True

    def test_both_withheld_when_effective_none(self):
        """Fail-safe: no resolvable owner → withhold BOTH gated fields."""
        out = ApplyOutcome(changed=True, apply_nickname=True, apply_avatar=True)
        gated = filter_by_entitlements(out, None)
        assert gated.apply_nickname is False
        assert gated.apply_avatar is False

    def test_missing_flags_default_restricted(self):
        """Absent flags in the effective map default to restricted (False)."""
        out = ApplyOutcome(changed=True, apply_nickname=True, apply_avatar=True)
        gated = filter_by_entitlements(out, {})
        assert gated.apply_nickname is False
        assert gated.apply_avatar is False


class TestApplyGuildEntitlementGate:
    """End-to-end gate through ``apply_guild`` with a fake resolver."""

    def test_avatar_rejected_when_custom_avatar_off(self):
        """A custom avatar set is rejected when ``custom_avatar`` is off (R4.3)."""
        key = "guild/1/bot-avatar/abc.png"
        store = FakeStore(
            {"1": {"nickname": "DJ", "avatar_present": True, "avatar_key": key,
                   "avatar_version": "abc", "requested_by": _OWNER}}
        )
        http = FakeHTTP()
        guild = FakeGuild(1)
        bot = FakeBot({1: guild}, http)
        resolver = FakeResolver(
            {_OWNER: {"custom_avatar": False, "custom_name": True}}
        )
        applier = _applier(bot, store, FakeS3({key: _PNG}), resolver=resolver)

        outcome = _run(applier.apply_guild("1"))

        # Avatar withheld — no REST PATCH issued.
        assert outcome.apply_avatar is False
        assert http.requests == []
        # Nickname still applied (custom_name on).
        assert guild.me.nick == "DJ"
        assert outcome.status == "applied"

    def test_name_rejected_when_custom_name_off(self):
        """A custom name set is rejected when ``custom_name`` is off (R4.4)."""
        key = "guild/1/bot-avatar/abc.png"
        store = FakeStore(
            {"1": {"nickname": "DJ", "avatar_present": True, "avatar_key": key,
                   "avatar_version": "abc", "requested_by": _OWNER}}
        )
        http = FakeHTTP()
        guild = FakeGuild(1)
        bot = FakeBot({1: guild}, http)
        resolver = FakeResolver(
            {_OWNER: {"custom_avatar": True, "custom_name": False}}
        )
        applier = _applier(bot, store, FakeS3({key: _PNG}), resolver=resolver)

        outcome = _run(applier.apply_guild("1"))

        # Nickname withheld — guild.me.edit never called.
        assert outcome.apply_nickname is False
        assert guild.me.nick is None
        # Avatar still applied (custom_avatar on).
        assert len(http.requests) == 1
        assert outcome.status == "applied"

    def test_both_withheld_marks_applied_and_no_side_effects(self):
        """Both flags off → nothing applied, item marked applied (no re-poll)."""
        key = "guild/1/bot-avatar/abc.png"
        store = FakeStore(
            {"1": {"nickname": "DJ", "avatar_present": True, "avatar_key": key,
                   "avatar_version": "abc", "requested_by": _OWNER}}
        )
        http = FakeHTTP()
        guild = FakeGuild(1)
        bot = FakeBot({1: guild}, http)
        resolver = FakeResolver(
            {_OWNER: {"custom_avatar": False, "custom_name": False}}
        )
        applier = _applier(bot, store, FakeS3({key: _PNG}), resolver=resolver)

        outcome = _run(applier.apply_guild("1"))

        assert outcome.apply_nickname is False
        assert outcome.apply_avatar is False
        assert guild.me.nick is None
        assert http.requests == []
        # Marked applied + version advanced so a later poll is a no-op.
        assert store.data["1"]["apply_status"] == "applied"
        assert store.data["1"]["applied_version"] == DesiredIdentity(
            nickname="DJ", avatar_present=True, avatar_version="abc"
        ).desired_version()

    def test_no_requested_by_fails_safe_withholds_both(self):
        """An item without ``requested_by`` withholds both (fail-safe deny)."""
        key = "guild/1/bot-avatar/abc.png"
        store = FakeStore(
            {"1": {"nickname": "DJ", "avatar_present": True, "avatar_key": key,
                   "avatar_version": "abc"}}  # no requested_by
        )
        http = FakeHTTP()
        guild = FakeGuild(1)
        bot = FakeBot({1: guild}, http)
        # Even a permissive resolver must not be consulted without an owner sub.
        resolver = FakeResolver(default={"custom_avatar": True,
                                          "custom_name": True})
        applier = _applier(bot, store, FakeS3({key: _PNG}), resolver=resolver)

        outcome = _run(applier.apply_guild("1"))

        assert outcome.apply_nickname is False
        assert outcome.apply_avatar is False
        assert resolver.calls == []  # no owner → resolver never called
        assert guild.me.nick is None
        assert http.requests == []
        assert store.data["1"]["apply_status"] == "applied"

    def test_resolution_failure_fails_safe_withholds_both(self):
        """A resolver failure withholds both gated fields (R14.3 fail-safe)."""
        key = "guild/1/bot-avatar/abc.png"
        store = FakeStore(
            {"1": {"nickname": "DJ", "avatar_present": True, "avatar_key": key,
                   "avatar_version": "abc", "requested_by": _OWNER}}
        )
        http = FakeHTTP()
        guild = FakeGuild(1)
        bot = FakeBot({1: guild}, http)
        resolver = RaisingResolver()
        applier = _applier(bot, store, FakeS3({key: _PNG}), resolver=resolver)

        outcome = _run(applier.apply_guild("1"))

        assert resolver.calls == [_OWNER]  # attempted, then failed → deny
        assert outcome.apply_nickname is False
        assert outcome.apply_avatar is False
        assert guild.me.nick is None
        assert http.requests == []
        assert store.data["1"]["apply_status"] == "applied"


def test_botidentity_sk_matches_web_ui_writer():
    """The applier and the web-ui writer must agree on the sort key."""
    assert BOTIDENTITY_SK == "BOTIDENTITY"
