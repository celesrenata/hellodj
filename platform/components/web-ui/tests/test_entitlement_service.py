"""Tests for :class:`EntitlementService` over a fake ``CoreTable`` (task 2.2).

Exercises the read surface (``get_raw`` vs ``get_effective`` defaults
indication, ``get_tally``, ``get_pricing``, ``history`` ordering) and the
audited write surface (``set_fields`` flip + persist, quota validation
rejection, ``reset_tally`` / ``add_cost`` tally math).

The write-before-apply semantics (design "Error Handling", Property 8) are
verified by injecting a failure into the fake table:

* an entitlement update/put failure raised *after* the audit write succeeds
  leaves the entitlement item unchanged (the change is not applied); and
* an audit-write failure leaves the entitlement item unchanged (nothing is
  written).

All tests use an in-memory fake ``TableLike`` implementing the small surface
``CoreTable`` calls (``get_item``, ``put_item`` with the create/version
``ConditionExpression`` guards, and ``query`` with ``begins_with``) plus a
programmable failure hook — no AWS. This extends the ``_FakeTable`` pattern from
``test_ai_pricing_seed.py``.

Requirements: 2.2, 2.3, 10.4, 10.6, 15.1, 15.2, 15.3
"""

from __future__ import annotations

from typing import Any

import pytest
from hellodj_platform_logic.data_access import CoreTable

import entitlements_core
from entitlement_service import (
    AITALLY_SK,
    AUDIT_SK_PREFIX,
    ENTITLEMENT_SK,
    EntitlementService,
    seed_ai_pricing,
)

_ADMIN = "admin-sub-1"
_USER = "user-sub-1"


class _ClientError(Exception):
    """Minimal botocore-shaped client error for the fake table."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _InjectedError(Exception):
    """A non-conditional datastore failure injected to exercise Property 8."""


class _FakeTable:
    """In-memory ``TableLike``: PK/SK get/put/query with create+version guards.

    Supports a programmable ``fail_put`` hook so tests can inject a datastore
    failure on the write matching a predicate (used to make the entitlement
    update or the audit put fail while the rest of the flow proceeds normally).
    """

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}
        #: Optional predicate ``(pk, sk, item) -> bool``; when it returns True
        #: the matching ``put_item`` raises :class:`_InjectedError`.
        self.fail_put: Any = None

    # -- reads --
    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        item = self._items.get(key)
        return {"Item": dict(item)} if item is not None else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        values = kwargs.get("ExpressionAttributeValues", {})
        pk = values[":pk"]
        prefix = values.get(":skp")
        items = [
            dict(item)
            for (item_pk, item_sk), item in self._items.items()
            if item_pk == pk and (prefix is None or item_sk.startswith(prefix))
        ]
        return {"Items": items}

    # -- writes --
    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = kwargs["Item"]
        key = (item["PK"], item["SK"])
        condition = kwargs.get("ConditionExpression")
        existing = self._items.get(key)

        if condition == "attribute_not_exists(PK)" and existing is not None:
            raise _ClientError("ConditionalCheckFailedException")
        if condition == "attribute_not_exists(version)" and existing is not None:
            raise _ClientError("ConditionalCheckFailedException")
        if condition == "version = :expected":
            expected = kwargs["ExpressionAttributeValues"][":expected"]
            if existing is None or existing.get("version") != expected:
                raise _ClientError("ConditionalCheckFailedException")

        if self.fail_put is not None and self.fail_put(item["PK"], item["SK"], item):
            raise _InjectedError("injected datastore failure")

        self._items[key] = dict(item)
        return {}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        self._items.pop(key, None)
        return {}


def _make() -> tuple[EntitlementService, _FakeTable]:
    fake = _FakeTable()
    return EntitlementService(CoreTable(fake)), fake


# -- reads ------------------------------------------------------------------


def test_get_raw_none_but_effective_is_defaults() -> None:
    """No stored record: ``get_raw`` is None (defaults indication) yet
    ``get_effective`` resolves the secure defaults (R2.2)."""
    svc, _ = _make()

    assert svc.get_raw(_USER) is None
    assert svc.get_effective(_USER) == entitlements_core.DEFAULT_ENTITLEMENTS


def test_get_raw_reflects_only_explicit_fields_effective_merges() -> None:
    """After a save, ``get_raw`` holds only the explicit field while
    ``get_effective`` merges it over defaults (R2.1, R2.2)."""
    svc, _ = _make()

    svc.set_fields(_USER, {"video_activities": True}, admin_sub=_ADMIN)

    assert svc.get_raw(_USER) == {"video_activities": True}
    effective = svc.get_effective(_USER)
    assert effective["video_activities"] is True
    # Untouched fields still resolve to their defaults.
    assert effective["ai_integration"] is False
    assert effective["sources"]["soundcloud"] is True


# -- flip + persist ---------------------------------------------------------


def test_set_fields_flip_persists_and_audits() -> None:
    """A flag flip persists to the entitlement item and writes one audit row
    with old/new values (R2.3, R15.1, R15.3)."""
    svc, _ = _make()

    svc.set_fields(_USER, {"ai_integration": True}, admin_sub=_ADMIN)

    assert svc.get_effective(_USER)["ai_integration"] is True
    history = svc.history(_USER)
    assert len(history) == 1
    entry = history[0]
    assert entry["field"] == "ai_integration"
    assert entry["old"] is False
    assert entry["new"] is True
    assert entry["admin_sub"] == _ADMIN
    assert entry["apply_status"] == "applied"


def test_set_fields_noop_writes_nothing() -> None:
    """Setting a field to its current effective value is a no-op: no audit
    entry is written."""
    svc, _ = _make()

    # video_activities defaults to False; setting it False again is a no-op.
    svc.set_fields(_USER, {"video_activities": False}, admin_sub=_ADMIN)

    assert svc.get_raw(_USER) is None
    assert svc.history(_USER) == []


# -- quota validation -------------------------------------------------------


def test_set_fields_rejects_quota_below_one() -> None:
    """A quota field < 1 is rejected with ValueError and nothing is written
    (R12.2)."""
    svc, _ = _make()

    with pytest.raises(ValueError):
        svc.set_fields(_USER, {"max_guilds": 0}, admin_sub=_ADMIN)

    assert svc.get_raw(_USER) is None
    assert svc.history(_USER) == []


def test_set_fields_accepts_valid_quota() -> None:
    """A quota >= 1 is applied and audited (R12.2)."""
    svc, _ = _make()

    svc.set_fields(_USER, {"max_guilds": 5}, admin_sub=_ADMIN)

    assert svc.get_effective(_USER)["max_guilds"] == 5


# -- tally increment / reset ------------------------------------------------


def test_add_cost_increments_tally() -> None:
    """``add_cost`` accumulates cost across calls (R10.4)."""
    svc, _ = _make()

    svc.add_cost(_USER, 1.5)
    svc.add_cost(_USER, 2.25)

    assert svc.get_tally(_USER)["accumulated_cost"] == pytest.approx(3.75)


def test_reset_tally_zeros_and_audits() -> None:
    """``reset_tally`` zeros the accumulated cost and records an audit entry
    (R10.6)."""
    svc, _ = _make()
    svc.add_cost(_USER, 4.0)

    svc.reset_tally(_USER, admin_sub=_ADMIN)

    assert svc.get_tally(_USER)["accumulated_cost"] == 0.0
    reset_entries = [h for h in svc.history(_USER) if h["field"] == "ai_tally"]
    assert len(reset_entries) == 1
    assert reset_entries[0]["old"] == pytest.approx(4.0)
    assert reset_entries[0]["new"] == 0.0


# -- pricing read -----------------------------------------------------------


def test_get_pricing_empty_then_seeded() -> None:
    """``get_pricing`` returns {} until seeded, then the seeded table (R10.3)."""
    svc, fake = _make()

    assert svc.get_pricing() == {}
    seed_ai_pricing(CoreTable(fake))
    pricing = svc.get_pricing()
    assert pricing["markup"] == 1.0
    assert pricing["models"]


# -- history ordering -------------------------------------------------------


def test_history_is_newest_first() -> None:
    """``history`` returns audit entries newest-first (R15.3)."""
    svc, _ = _make()

    svc.set_fields(_USER, {"video_activities": True}, admin_sub=_ADMIN)
    svc.set_fields(_USER, {"visualizations": True}, admin_sub=_ADMIN)
    svc.set_fields(_USER, {"wakeword": True}, admin_sub=_ADMIN)

    fields = [h["field"] for h in svc.history(_USER)]
    ats = [h["at"] for h in svc.history(_USER)]
    assert fields[0] == "wakeword"  # most recent first
    assert ats == sorted(ats, reverse=True)


# -- Property 8: change requires audit --------------------------------------


def test_entitlement_update_failure_leaves_item_unchanged() -> None:
    """Property 8: an entitlement update failure *after* the audit write
    succeeds leaves the entitlement item unchanged (change not applied).

    Requirements: 15.1, 15.2
    """
    svc, fake = _make()
    # Establish a stored baseline the failed update must not alter.
    svc.set_fields(_USER, {"ai_integration": False}, admin_sub=_ADMIN)
    baseline_effective = svc.get_effective(_USER)
    assert baseline_effective["ai_integration"] is False

    # Fail only the entitlement item write; audit puts still succeed.
    fake.fail_put = lambda pk, sk, item: sk == ENTITLEMENT_SK

    with pytest.raises(_InjectedError):
        svc.set_fields(_USER, {"ai_integration": True}, admin_sub=_ADMIN)

    # The entitlement record is unchanged.
    assert svc.get_effective(_USER)["ai_integration"] is False

    # The audit row that was written first is marked orphaned (never applied).
    fake.fail_put = None
    orphaned = [
        h
        for h in svc.history(_USER)
        if h["field"] == "ai_integration" and h["new"] is True
    ]
    assert orphaned and orphaned[0]["apply_status"] == "orphaned"


def test_audit_write_failure_leaves_item_unchanged() -> None:
    """Property 8: an audit-write failure leaves the entitlement item
    unchanged (nothing is applied).

    Requirements: 15.1, 15.2
    """
    svc, fake = _make()
    svc.set_fields(_USER, {"video_activities": False}, admin_sub=_ADMIN)
    assert svc.get_effective(_USER)["video_activities"] is False

    # Fail the audit write (write-before-apply: it happens first).
    fake.fail_put = lambda pk, sk, item: sk.startswith(AUDIT_SK_PREFIX)

    with pytest.raises(_InjectedError):
        svc.set_fields(_USER, {"video_activities": True}, admin_sub=_ADMIN)

    # Entitlement unchanged and no audit entry persisted.
    assert svc.get_effective(_USER)["video_activities"] is False
    fake.fail_put = None
    assert all(
        not (h["field"] == "video_activities" and h["new"] is True)
        for h in svc.history(_USER)
    )


def test_reset_tally_apply_failure_leaves_tally_unchanged() -> None:
    """Property 8 (tally variant): if the tally reset write fails after the
    audit write, the tally is unchanged (R10.6, R15.2)."""
    svc, fake = _make()
    svc.add_cost(_USER, 7.0)

    fake.fail_put = lambda pk, sk, item: sk == AITALLY_SK

    with pytest.raises(_InjectedError):
        svc.reset_tally(_USER, admin_sub=_ADMIN)

    fake.fail_put = None
    assert svc.get_tally(_USER)["accumulated_cost"] == pytest.approx(7.0)
