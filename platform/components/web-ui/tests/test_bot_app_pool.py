"""Tests for the global bot-application pool + per-guild claim/invite links.

Covers :mod:`bot_app_pool` (pool reader, assignment service, invite URL) and
the guild-detail add/remove-bot routes, against an in-memory ``CoreTable`` +
a fake Secrets Manager client — no live AWS.

Asserts:

* The invite URL carries only the public ``client_id`` + scopes + permissions,
  never a secret/token.
* Assignment hands out distinct pool apps, is quota-capped, and refuses when
  the guild already holds every pool app (pool exhausted).
* A guild can hold each app at most once (Discord per-guild dedupe); the same
  app may serve different guilds.
* Release deletes only the calling guild's claim.
* The add/remove routes are ownership-gated and quota-enforced end-to-end.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from hellodj_platform_logic.data_access import CoreTable

from app import create_app
from bot_app_pool import (
    BotAppAssignmentService,
    BotAppPool,
    PoolExhaustedError,
    QuotaReachedError,
    bot_invite_url,
)
from guild_admin_service import guild_pk

_GID = "42"
_SUB = "owner-sub-1"
_SECRET = "super-secret-value-should-never-render"

_POOL = [
    {"label": "HelloDJ-00", "client_id": "100", "client_secret": _SECRET, "bot_token": _SECRET},
    {"label": "HelloDJ-01", "client_id": "101", "client_secret": _SECRET, "bot_token": _SECRET},
    {"label": "HelloDJ-02", "client_id": "102", "client_secret": _SECRET, "bot_token": _SECRET},
]


@dataclass
class _FakeSecrets:
    payload: str

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        return {"SecretString": self.payload}


@dataclass
class _FakeTable:
    _items: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        item = self._items.get((kwargs["Key"]["PK"], kwargs["Key"]["SK"]))
        return {"Item": dict(item)} if item is not None else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        values = kwargs.get("ExpressionAttributeValues", {})
        pk = values[":pk"]
        prefix = values.get(":skp")
        items = [
            dict(it)
            for (ipk, isk), it in self._items.items()
            if ipk == pk and (prefix is None or isk.startswith(prefix))
        ]
        return {"Items": items}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = kwargs["Item"]
        self._items[(item["PK"], item["SK"])] = dict(item)
        return {}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        self._items.pop((kwargs["Key"]["PK"], kwargs["Key"]["SK"]), None)
        return {}


def _service(pool_entries=_POOL) -> tuple[BotAppAssignmentService, CoreTable]:
    core = CoreTable(_FakeTable())
    pool = BotAppPool(_FakeSecrets(json.dumps(pool_entries)), stage="beta")
    return BotAppAssignmentService(core, pool), core


# ── invite url ─────────────────────────────────────────────────────────────


def test_invite_url_carries_only_public_client_id():
    url = bot_invite_url("123456")
    assert url.startswith("https://discord.com/oauth2/authorize?")
    assert "client_id=123456" in url
    assert "scope=bot" in url
    assert "permissions=" in url


# ── pool reader ──────────────────────────────────────────────────────────────


def test_pool_exposes_only_label_and_client_id():
    pool = BotAppPool(_FakeSecrets(json.dumps(_POOL)), stage="beta")
    assert pool.size() == 3
    assert pool.client_ids() == ["100", "101", "102"]
    assert pool.label_for("101") == "HelloDJ-01"
    # The secret/token never surface through the public reader surface.
    assert _SECRET not in json.dumps(
        [{"id": c, "label": pool.label_for(c)} for c in pool.client_ids()]
    )


def test_missing_pool_secret_degrades_to_empty():
    pool = BotAppPool(_FakeSecrets(""), stage="beta")
    assert pool.size() == 0
    assert pool.client_ids() == []


# ── assignment ───────────────────────────────────────────────────────────────


def test_assign_next_hands_out_distinct_apps_then_hits_quota():
    svc, _core = _service()
    first = svc.assign_next(_GID, max_bots=2, claimed_by=_SUB)
    second = svc.assign_next(_GID, max_bots=2, claimed_by=_SUB)
    assert first["client_id"] == "100"
    assert second["client_id"] == "101"
    # Third exceeds the max_bots=2 quota.
    try:
        svc.assign_next(_GID, max_bots=2, claimed_by=_SUB)
        raise AssertionError("expected QuotaReachedError")
    except QuotaReachedError:
        pass
    assert svc.claim_count(_GID) == 2


def test_assign_refuses_when_pool_exhausted():
    svc, _core = _service()
    # max_bots above pool size; claim all 3, then the 4th exhausts the pool.
    for _ in range(3):
        svc.assign_next(_GID, max_bots=10, claimed_by=_SUB)
    try:
        svc.assign_next(_GID, max_bots=10, claimed_by=_SUB)
        raise AssertionError("expected PoolExhaustedError")
    except PoolExhaustedError:
        pass


def test_same_app_serves_two_guilds():
    svc, _core = _service()
    a = svc.assign_next("guildA", max_bots=1, claimed_by=_SUB)
    b = svc.assign_next("guildB", max_bots=1, claimed_by=_SUB)
    # Each guild independently gets the first pool app (global, multi-guild).
    assert a["client_id"] == "100"
    assert b["client_id"] == "100"


def test_release_only_affects_calling_guild():
    svc, core = _service()
    svc.assign_next("guildA", max_bots=1, claimed_by=_SUB)
    svc.assign_next("guildB", max_bots=1, claimed_by=_SUB)
    svc.release("guildA", "100")
    assert svc.claim_count("guildA") == 0
    assert svc.claim_count("guildB") == 1


# ── routes (ownership-gated, quota-enforced end-to-end) ─────────────────────


def _app_with_owner() -> tuple[Any, BotAppAssignmentService, CoreTable]:
    core = CoreTable(_FakeTable())
    pool = BotAppPool(_FakeSecrets(json.dumps(_POOL)), stage="beta")
    assign = BotAppAssignmentService(core, pool)

    class _Ent:
        def get_effective(self, sub: str) -> dict[str, Any]:
            return {"max_bots_per_guild": 2, "max_bots_per_guild_enabled": True}

    class _GuildAdmin:
        def owner_of(self, gid: str) -> str:
            return _SUB

        def admin_discord_ids(self, gid: str) -> set[str]:
            return set()

    app = create_app(overrides={"TESTING": True, "SECRET_KEY": "k"})
    app.extensions["bot_app_assignment"] = assign
    app.extensions["entitlement_service"] = _Ent()
    app.extensions["guild_admin"] = _GuildAdmin()
    return app, assign, core


def _client(app: Any):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = {"is_admin": False, "sub": _SUB}
    return client


def test_add_bot_route_assigns_and_renders_invite():
    app, _assign, _core = _app_with_owner()
    client = _client(app)

    resp = client.post(f"/guilds/{_GID}/bots", headers={"HX-Request": "true"})
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    # An invite link for the assigned app is rendered; no secret leaks.
    assert "discord.com/oauth2/authorize" in body
    assert "client_id=100" in body
    assert _SECRET not in body


def test_add_bot_route_enforces_quota():
    app, _assign, _core = _app_with_owner()
    client = _client(app)
    client.post(f"/guilds/{_GID}/bots", headers={"HX-Request": "true"})
    client.post(f"/guilds/{_GID}/bots", headers={"HX-Request": "true"})
    # Third exceeds max_bots_per_guild=2 → surfaced as an error, not a 3rd claim.
    resp = client.post(f"/guilds/{_GID}/bots", headers={"HX-Request": "true"})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "maximum" in body.lower()


def test_remove_bot_route_releases_claim():
    app, assign, _core = _app_with_owner()
    client = _client(app)
    client.post(f"/guilds/{_GID}/bots", headers={"HX-Request": "true"})
    assert assign.claim_count(_GID) == 1

    resp = client.post(
        f"/guilds/{_GID}/bots/100/remove", headers={"HX-Request": "true"}
    )
    assert resp.status_code == 200
    assert assign.claim_count(_GID) == 0


def test_add_bot_route_requires_manage_permission():
    app, _assign, _core = _app_with_owner()
    client = app.test_client()
    with client.session_transaction() as sess:
        # A non-owner, non-admin user cannot manage this guild.
        sess["user"] = {"is_admin": False, "sub": "someone-else"}

    resp = client.post(f"/guilds/{_GID}/bots")
    # Bounced to the guilds list (redirect), no assignment made.
    assert resp.status_code == 302
    assert "/guilds" in resp.headers["Location"]


# ── default names iterate by claim index ────────────────────────────────────


def test_default_bot_names_iterate():
    from bot_identity import default_bot_name

    assert default_bot_name(0) == "HelloDJ"
    assert default_bot_name(1) == "HelloDJ#1"
    assert default_bot_name(2) == "HelloDJ#2"


def test_list_claims_carries_index_in_pool_order():
    svc, _core = _service()
    svc.assign_next(_GID, max_bots=3, claimed_by=_SUB)
    svc.assign_next(_GID, max_bots=3, claimed_by=_SUB)
    claims = svc.list_claims(_GID)
    assert [c["index"] for c in claims] == [0, 1]
    assert [c["client_id"] for c in claims] == ["100", "101"]


# ── per-bot identity keying (BOTIDENTITY#<client_id>) ───────────────────────


def test_identity_is_keyed_per_bot():
    from bot_identity import BotIdentityService, botidentity_sk

    core = CoreTable(_FakeTable())

    class _S3:
        def put_object(self, **kwargs):
            return {}

    svc = BotIdentityService(core, _S3(), stage="beta", avatar_bucket="b")
    svc.set_nickname("42", "DJ A", requested_by="o", client_id="100")
    svc.set_nickname("42", "DJ B", requested_by="o", client_id="101")

    # Each bot has its own identity item; no cross-contamination.
    assert svc.get_identity("42", client_id="100")["nickname"] == "DJ A"
    assert svc.get_identity("42", client_id="101")["nickname"] == "DJ B"
    assert core.get(guild_pk("42"), botidentity_sk("100")) is not None
    assert core.get(guild_pk("42"), botidentity_sk("101")) is not None
    # The legacy per-guild key is untouched.
    assert core.get(guild_pk("42"), botidentity_sk()) is None


# ── entitlement gating on rename ────────────────────────────────────────────


def _app_without_custom_name():
    core = CoreTable(_FakeTable())
    pool = BotAppPool(_FakeSecrets(json.dumps(_POOL)), stage="beta")
    assign = BotAppAssignmentService(core, pool)

    class _Ent:
        def get_effective(self, sub: str):
            # Owner has bots but NOT the custom_name/custom_avatar entitlement.
            return {
                "max_bots_per_guild": 3,
                "max_bots_per_guild_enabled": True,
                "custom_name": False,
                "custom_avatar": False,
            }

    class _GuildAdmin:
        def owner_of(self, gid):
            return _SUB

        def admin_discord_ids(self, gid):
            return set()

    class _Identity:
        def __init__(self):
            self.calls = 0

        def get_identity(self, gid, *, client_id=""):
            return {}

        def set_nickname(self, *a, **k):
            self.calls += 1

    identity = _Identity()
    app = create_app(overrides={"TESTING": True, "SECRET_KEY": "k"})
    app.extensions["bot_app_assignment"] = assign
    app.extensions["entitlement_service"] = _Ent()
    app.extensions["guild_admin"] = _GuildAdmin()
    app.extensions["guild_identity_service"] = identity
    return app, assign, identity


def test_rename_rejected_without_custom_name_entitlement():
    app, assign, identity = _app_without_custom_name()
    assign.assign_next(_GID, max_bots=3, claimed_by=_SUB)
    client = _client(app)

    resp = client.post(
        f"/guilds/{_GID}/bots/100/name",
        data={"nickname": "Custom"},
        headers={"HX-Request": "true"},
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    # The rename was refused and the service was never asked to persist a name.
    assert identity.calls == 0
    assert "not enabled" in body.lower()


def test_bots_partial_shows_default_name_without_entitlement():
    app, assign, _identity = _app_without_custom_name()
    assign.assign_next(_GID, max_bots=3, claimed_by=_SUB)
    assign.assign_next(_GID, max_bots=3, claimed_by=_SUB)
    client = _client(app)

    resp = client.post(f"/guilds/{_GID}/bots", headers={"HX-Request": "true"})
    body = resp.get_data(as_text=True)
    # Default iterated names are shown; no editable name input.
    assert "HelloDJ" in body
    assert "HelloDJ#" in body
