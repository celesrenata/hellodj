"""Tests for guild ownership + Discord-id admin appointment (task 11).

Covers the ``guild_admin_service`` surface that every guild/source route relies
on (R4.1-R4.4, R5.2):

* ``appoint_admin`` persists a Guild_Admin edge keyed by Discord id with the
  GSI1 reverse index (``DISCORD#<id>`` / ``GUILDADMIN#<gid>``) and is
  idempotent; ``list_admins`` / ``remove_admin`` enumerate and delete edges
  (R4.1, R4.2).
* ``guilds_administered_by_discord`` resolves the guilds a Discord id
  administers via GSI1 (the reverse lookup a Discord login uses).
* ``claim_ownership`` / ``owner_of`` record and read a guild's owner.
* ``can_manage_guild`` is the single pure gate: it grants the recorded OWNER,
  an appointed Guild_Admin whose *linked* Discord id has the edge (R4.4), and
  the super-admin; it denies everyone else — including a non-owner user who
  owns no edge (R4.3) and an appointed admin who has not yet linked Discord.

Uses an in-memory fake ``TableLike`` supporting PK access, the GSI1 query, and
the base-table PK-prefix query (edge enumeration) — no AWS.

Requirements: 4.1, 4.2, 4.3, 4.4
"""

from __future__ import annotations

from typing import Any

from hellodj_platform_logic.data_access import CoreTable

from guild_admin_service import (
    GuildAdminService,
    admin_sk,
    can_manage_guild,
    guild_pk,
)


class _ClientError(Exception):
    """Minimal botocore-shaped client error for the fake table."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeTable:
    """In-memory ``TableLike`` supporting PK access, GSI1 + base-PK queries."""

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
        if condition == "attribute_not_exists(version)" and existing is not None:
            raise _ClientError("ConditionalCheckFailedException")
        if condition == "version = :expected":
            expected = values[":expected"]
            if existing is None or existing.get("version") != expected:
                raise _ClientError("ConditionalCheckFailedException")
        self._items[key] = dict(item)
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        expr = kwargs["KeyConditionExpression"]
        values = kwargs["ExpressionAttributeValues"]
        pk = values[":pk"]
        prefix = values.get(":skp")
        if kwargs.get("IndexName") == "GSI1":
            items = [
                dict(it)
                for it in self._items.values()
                if it.get("GSI1PK") == pk
                and (prefix is None or str(it.get("GSI1SK", "")).startswith(prefix))
            ]
            return {"Items": items}
        assert expr.startswith("PK = :pk")
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


def _service() -> tuple[GuildAdminService, CoreTable]:
    table = _FakeTable()
    core = CoreTable(table)
    return GuildAdminService(core), core


# -- ownership -------------------------------------------------------------- #


def test_claim_ownership_records_owner_and_owner_of_reads_it() -> None:
    svc, _ = _service()

    assert svc.owner_of("g1") is None
    svc.claim_ownership("g1", "owner-sub")

    assert svc.owner_of("g1") == "owner-sub"


def test_claim_ownership_is_idempotent_and_does_not_overwrite() -> None:
    svc, _ = _service()
    svc.claim_ownership("g1", "first-owner")

    # A second claim (even by a different subject) must not steal ownership.
    svc.claim_ownership("g1", "second-owner")

    assert svc.owner_of("g1") == "first-owner"


# -- appoint / list / remove admin edges (R4.1, R4.2) ----------------------- #


def test_appoint_admin_persists_edge_with_gsi1_reverse_index() -> None:
    svc, core = _service()

    svc.appoint_admin("g1", "1234567890", appointed_by="owner-sub")

    stored = core.get(guild_pk("g1"), admin_sk("1234567890"))
    assert stored is not None
    assert stored["data"]["appointed_by"] == "owner-sub"
    # GSI1 reverse index: DISCORD#<id> / GUILDADMIN#<gid> (R4.1).
    assert stored["GSI1PK"] == "DISCORD#1234567890"
    assert stored["GSI1SK"] == "GUILDADMIN#g1"


def test_appoint_admin_is_idempotent() -> None:
    svc, _ = _service()

    svc.appoint_admin("g1", "111", appointed_by="owner-sub")
    # A repeat appointment must not raise or duplicate the edge.
    svc.appoint_admin("g1", "111", appointed_by="someone-else")

    admins = svc.list_admins("g1")
    assert len(admins) == 1
    # The original appointer is preserved (no overwrite).
    assert admins[0]["appointed_by"] == "owner-sub"


def test_list_admins_enumerates_edges_and_exposes_discord_ids() -> None:
    svc, _ = _service()
    svc.appoint_admin("g1", "111", appointed_by="owner-sub")
    svc.appoint_admin("g1", "222", appointed_by="owner-sub")
    # An edge on a different guild must not leak into g1's listing.
    svc.appoint_admin("g2", "333", appointed_by="owner-sub")

    ids = svc.admin_discord_ids("g1")

    assert ids == {"111", "222"}
    assert {a["discord_id"] for a in svc.list_admins("g1")} == {"111", "222"}


def test_remove_admin_deletes_the_edge() -> None:
    svc, _ = _service()
    svc.appoint_admin("g1", "111", appointed_by="owner-sub")

    svc.remove_admin("g1", "111")

    assert svc.admin_discord_ids("g1") == set()


def test_remove_admin_is_a_noop_for_unknown_edge() -> None:
    svc, _ = _service()

    # Removing an edge that was never appointed must not raise.
    svc.remove_admin("g1", "does-not-exist")

    assert svc.list_admins("g1") == []


def test_guilds_administered_by_discord_resolves_via_gsi1() -> None:
    svc, _ = _service()
    svc.appoint_admin("g1", "111", appointed_by="owner-sub")
    svc.appoint_admin("g2", "111", appointed_by="owner-sub")
    svc.appoint_admin("g3", "222", appointed_by="owner-sub")

    guilds = svc.guilds_administered_by_discord("111")

    assert set(guilds) == {"g1", "g2"}


# -- can_manage_guild: the single authorization gate (R4.3, R4.4) ----------- #


def test_super_admin_can_manage_any_guild() -> None:
    assert (
        can_manage_guild(
            guild_id="g1",
            user_sub="whoever",
            discord_id=None,
            is_super_admin=True,
            owner_sub="someone-else",
            admin_discord_ids=set(),
        )
        is True
    )


def test_owner_can_manage_their_guild() -> None:
    assert (
        can_manage_guild(
            guild_id="g1",
            user_sub="owner-sub",
            discord_id=None,
            is_super_admin=False,
            owner_sub="owner-sub",
            admin_discord_ids=set(),
        )
        is True
    )


def test_appointed_admin_with_linked_discord_can_manage_sources() -> None:
    # R4.4: an appointed admin gains source management once their Discord
    # account is linked — i.e. the caller carries a linked discord_id that
    # matches an appointed edge.
    assert (
        can_manage_guild(
            guild_id="g1",
            user_sub="admin-sub",
            discord_id="111",
            is_super_admin=False,
            owner_sub="owner-sub",
            admin_discord_ids={"111", "222"},
        )
        is True
    )


def test_appointed_admin_without_linked_discord_is_denied() -> None:
    # Before linking Discord the caller has no discord_id, so even though a
    # Discord id was appointed, this session cannot manage the guild (R4.4).
    assert (
        can_manage_guild(
            guild_id="g1",
            user_sub="admin-sub",
            discord_id=None,
            is_super_admin=False,
            owner_sub="owner-sub",
            admin_discord_ids={"111"},
        )
        is False
    )


def test_non_owner_non_admin_user_is_denied() -> None:
    # R4.3: a user who is neither the owner nor an appointed admin (and not a
    # super-admin) cannot manage the guild — the gate a route uses to refuse an
    # appoint/remove for a guild the caller does not control.
    assert (
        can_manage_guild(
            guild_id="g1",
            user_sub="stranger-sub",
            discord_id="999",
            is_super_admin=False,
            owner_sub="owner-sub",
            admin_discord_ids={"111", "222"},
        )
        is False
    )


def test_end_to_end_appointment_grants_management_via_service_facts() -> None:
    """Appoint by Discord id, then the same id (once linked) passes the gate.

    Wires the real service to resolve the facts ``can_manage_guild`` consumes:
    a stranger is denied, and the appointed Discord id — presented as the
    caller's linked ``discord_id`` — is granted source management (R4.1->R4.4).
    """
    svc, _ = _service()
    svc.claim_ownership("g1", "owner-sub")
    svc.appoint_admin("g1", "111", appointed_by="owner-sub")

    owner_sub = svc.owner_of("g1")
    admin_ids = svc.admin_discord_ids("g1")

    # The appointed admin, now with a linked Discord id, is granted access.
    assert can_manage_guild(
        guild_id="g1",
        user_sub="admin-sub",
        discord_id="111",
        is_super_admin=False,
        owner_sub=owner_sub,
        admin_discord_ids=admin_ids,
    )
    # A stranger (no ownership, no edge, unlinked to any appointed id) is denied.
    assert not can_manage_guild(
        guild_id="g1",
        user_sub="stranger-sub",
        discord_id="999",
        is_super_admin=False,
        owner_sub=owner_sub,
        admin_discord_ids=admin_ids,
    )
