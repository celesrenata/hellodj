"""Tests for the idempotent ``CONFIG#AIPRICING`` seeder (task 5).

The AI pricing configuration item stores per-model Bedrock unit prices plus a
``markup`` (default 1.0) as *data*, so a price change is an in-place data edit
requiring no code change or redeploy (R10.3). The seeder guarantees the item
exists on first run and must never clobber an existing item (so ops' edited
prices survive re-seeding).

Covers:

* ``seed_ai_pricing`` creates the item with the expected shape when absent
  (``models`` map + ``markup``) and reports ``True`` (R10.2, R10.3).
* Re-seeding is idempotent: a second call reports ``False`` and leaves the
  stored (possibly ops-edited) prices untouched (R10.3).
* The instance wrapper ``EntitlementService.seed_pricing`` behaves the same and
  the seeded values are readable via ``get_pricing``.
* A custom pricing payload can be supplied (ops override).

Uses an in-memory fake ``TableLike`` (PK get/put with the create guard) — no AWS.

Requirements: 10.2, 10.3
"""

from __future__ import annotations

from typing import Any

from hellodj_platform_logic.data_access import CoreTable

from entitlement_service import (
    AIPRICING_ENTITY_TYPE,
    AIPRICING_PK,
    AIPRICING_SK,
    DEFAULT_AI_PRICING,
    EntitlementService,
    seed_ai_pricing,
)


class _ClientError(Exception):
    """Minimal botocore-shaped client error for the fake table."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeTable:
    """In-memory ``TableLike`` supporting PK get/put with the create guard."""

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
        existing = self._items.get(key)
        if condition == "attribute_not_exists(PK)" and existing is not None:
            raise _ClientError("ConditionalCheckFailedException")
        self._items[key] = dict(item)
        return {}


def _core() -> CoreTable:
    return CoreTable(_FakeTable())


def test_seed_creates_pricing_item_when_absent() -> None:
    core = _core()

    created = seed_ai_pricing(core)

    assert created is True
    item = core.get(AIPRICING_PK, AIPRICING_SK)
    assert item is not None
    assert item["entityType"] == AIPRICING_ENTITY_TYPE
    data = item["data"]
    assert data["markup"] == 1.0
    assert isinstance(data["models"], dict) and data["models"]
    # Each model carries the documented per-1k + per-request price shape.
    for price in data["models"].values():
        assert {"input_per_1k", "output_per_1k", "request"} <= price.keys()


def test_seed_is_idempotent_and_preserves_edits() -> None:
    core = _core()
    assert seed_ai_pricing(core) is True

    # Simulate an ops in-place price edit (R10.3): prices are data.
    edited = dict(DEFAULT_AI_PRICING)
    edited["markup"] = 0.5

    def _mutate(data: dict[str, Any]) -> dict[str, Any]:
        data["markup"] = 0.5
        return data

    core.update_with_lock(
        AIPRICING_PK, AIPRICING_SK, _mutate, entity_type=AIPRICING_ENTITY_TYPE
    )

    # Re-seeding must not clobber the edited markup.
    created_again = seed_ai_pricing(core)

    assert created_again is False
    assert core.get(AIPRICING_PK, AIPRICING_SK)["data"]["markup"] == 0.5


def test_service_wrapper_seeds_and_is_readable() -> None:
    core = _core()
    svc = EntitlementService(core)

    assert svc.get_pricing() == {}
    assert svc.seed_pricing() is True

    pricing = svc.get_pricing()
    assert pricing["markup"] == 1.0
    assert pricing["models"]
    # Second call is a no-op.
    assert svc.seed_pricing() is False


def test_seed_accepts_custom_pricing_override() -> None:
    core = _core()
    custom = {
        "models": {"m": {"input_per_1k": 1.0, "output_per_1k": 2.0, "request": 0.0}},
        "markup": 2.0,
        "currency": "USD",
    }

    assert seed_ai_pricing(core, pricing=custom) is True
    assert core.get(AIPRICING_PK, AIPRICING_SK)["data"] == custom
