"""Unit tests for the DynamoDB data-access layer (task 7.1).

These tests exercise the data-access logic against lightweight in-memory fake
tables that mimic the subset of the boto3 resource ``Table`` interface the
layer uses (``get_item``/``put_item``/``update_item``/``query`` with native
Python types and ``ConditionExpression`` support for the version/create
guards). Using fakes keeps these unit tests dependency-free and deterministic;
the moto/DynamoDB Local round-trip property test is task 7.2.

Covered behavior:
    * DAX-fronted read path with fall-through to DynamoDB on a DAX miss/error.
    * Exponential-backoff retry on throttling, then a typed ``ThrottledError``.
    * ``hellodj-core`` single-table create + optimistic-lock read-modify-write,
      including retry-on-conflict and ``OptimisticLockError`` when exhausted.
    * ``hellodj-search-cache`` idempotent writes and GSI1 query.
    * ``hellodj-session`` optimistic-lock read-modify-write.
"""

from __future__ import annotations

from typing import Any

import pytest

from hellodj_platform_logic.data_access import (
    BackoffConfig,
    CoreTable,
    ItemNotFoundError,
    OptimisticLockError,
    ReadThroughTable,
    SearchCacheTable,
    SessionTable,
    ThrottledError,
)


class _ClientError(Exception):
    """Minimal stand-in for botocore's ClientError with a ``response`` dict."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def _matches_condition(condition: str, item: dict[str, Any], existing) -> bool:
    """Evaluate the tiny subset of ConditionExpressions the layer emits."""
    if condition == "attribute_not_exists(PK)":
        return existing is None
    if condition == "attribute_not_exists(version)":
        return existing is None
    if condition == "version = :expected":
        return existing is not None
    raise AssertionError(f"unexpected condition: {condition}")


class FakeTable:
    """In-memory fake implementing the injected ``TableLike`` interface.

    Supports the primary-key access patterns used by the layer plus a simple
    GSI1 query. Optional ``throttle_first`` makes the first N calls raise a
    throttling error so backoff/retry can be exercised.
    """

    def __init__(
        self,
        key_attrs: tuple[str, ...],
        throttle_first: int = 0,
        *,
        scan_page_size: int | None = None,
    ) -> None:
        self._key_attrs = key_attrs
        self._items: dict[tuple, dict[str, Any]] = {}
        self._throttle_remaining = throttle_first
        # When set, ``scan`` returns at most this many items per call and a
        # ``LastEvaluatedKey`` so pagination is exercised. Insertion order is
        # used as the deterministic scan order.
        self._scan_page_size = scan_page_size

    def _pk(self, source: dict[str, Any]) -> tuple:
        return tuple(source[attr] for attr in self._key_attrs)

    def _maybe_throttle(self) -> None:
        if self._throttle_remaining > 0:
            self._throttle_remaining -= 1
            raise _ClientError("ProvisionedThroughputExceededException")

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_throttle()
        key = self._pk(kwargs["Key"])
        item = self._items.get(key)
        return {"Item": dict(item)} if item is not None else {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_throttle()
        item = kwargs["Item"]
        key = self._pk(item)
        existing = self._items.get(key)
        condition = kwargs.get("ConditionExpression")
        if condition is not None:
            values = kwargs.get("ExpressionAttributeValues", {})
            if not _matches_condition(condition, item, existing):
                raise _ClientError("ConditionalCheckFailedException")
            if condition == "version = :expected":
                if int(existing["version"]) != int(values[":expected"]):
                    raise _ClientError("ConditionalCheckFailedException")
        self._items[key] = dict(item)
        return {}

    def update_item(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        self._maybe_throttle()
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_throttle()
        values = kwargs["ExpressionAttributeValues"]
        gsi1pk = values[":pk"]
        prefix = values.get(":skp")
        items = [
            dict(it)
            for it in self._items.values()
            if it.get("GSI1PK") == gsi1pk
            and (prefix is None or str(it.get("GSI1SK", "")).startswith(prefix))
        ]
        return {"Items": items}

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        self._maybe_throttle()
        names = kwargs.get("ExpressionAttributeNames", {})
        values = kwargs.get("ExpressionAttributeValues", {})
        # Apply the tiny ``#et = :et`` entityType filter the layer emits.
        filter_expr = kwargs.get("FilterExpression")
        ordered = list(self._items.values())
        if filter_expr is not None:
            assert filter_expr == "#et = :et"
            wanted = values[":et"]
            ordered = [it for it in ordered if it.get("entityType") == wanted]
        # Paginate deterministically using the key tuple as the cursor.
        start = kwargs.get("ExclusiveStartKey")
        if start is not None:
            start_pk = self._pk(start)
            cursor = [self._pk(it) for it in ordered].index(start_pk) + 1
            ordered = ordered[cursor:]
        last_key: dict[str, Any] | None = None
        if self._scan_page_size is not None and len(ordered) > self._scan_page_size:
            ordered = ordered[: self._scan_page_size]
            tail = ordered[-1]
            last_key = {attr: tail[attr] for attr in self._key_attrs}
        projection = kwargs.get("ProjectionExpression")
        items = [self._project(it, projection, names) for it in ordered]
        response: dict[str, Any] = {"Items": items}
        if last_key is not None:
            response["LastEvaluatedKey"] = last_key
        return response

    @staticmethod
    def _project(
        item: dict[str, Any],
        projection: str | None,
        names: dict[str, str],
    ) -> dict[str, Any]:
        """Return only the attributes named by a ProjectionExpression."""
        if not projection:
            return dict(item)
        out: dict[str, Any] = {}
        for raw in (part.strip() for part in projection.split(",")):
            resolved = names.get(raw, raw)
            if "." in raw:
                # Nested "#d.expires_at" style: resolve each segment.
                top_alias, sub = raw.split(".", 1)
                top = names.get(top_alias, top_alias)
                sub_name = names.get(sub, sub)
                nested = item.get(top)
                if isinstance(nested, dict) and sub_name in nested:
                    out.setdefault(top, {})[sub_name] = nested[sub_name]
            elif resolved in item:
                out[resolved] = item[resolved]
        return out


class ExplodingTable(FakeTable):
    """A table whose reads always fail (used to test DAX fall-through)."""

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        raise _ClientError("ItemNotFoundInDax")

    def query(self, **kwargs: Any) -> dict[str, Any]:
        raise _ClientError("ItemNotFoundInDax")


_FAST_BACKOFF = BackoffConfig(
    max_attempts=4,
    base_delay_seconds=0.0,
    max_delay_seconds=0.0,
    sleep=lambda _s: None,
)


# -- ReadThroughTable / DAX fall-through ------------------------------------


def test_dax_read_then_fall_through_to_dynamodb() -> None:
    ddb = FakeTable(("PK", "SK"))
    ddb.put_item(Item={"PK": "USER#1", "SK": "META", "data": {"x": 1}, "version": 1})
    dax = ExplodingTable(("PK", "SK"))
    table = ReadThroughTable(ddb, dax, backoff=_FAST_BACKOFF)

    got = table.get_item(Key={"PK": "USER#1", "SK": "META"})

    assert got["Item"]["data"] == {"x": 1}


def test_reads_served_by_dax_when_available() -> None:
    ddb = FakeTable(("queryKey",))
    dax = FakeTable(("queryKey",))
    dax.put_item(Item={"queryKey": "q", "results": {"hit": True}})
    table = ReadThroughTable(ddb, dax, backoff=_FAST_BACKOFF)

    got = table.get_item(Key={"queryKey": "q"})

    assert got["Item"]["results"] == {"hit": True}


# -- backoff / throttling ----------------------------------------------------


def test_backoff_retries_then_succeeds() -> None:
    ddb = FakeTable(("PK", "SK"), throttle_first=2)
    ddb._items[("USER#1", "META")] = {"PK": "USER#1", "SK": "META", "v": 1}
    table = ReadThroughTable(ddb, None, backoff=_FAST_BACKOFF)

    got = table.get_item(Key={"PK": "USER#1", "SK": "META"})

    assert got["Item"]["v"] == 1


def test_backoff_surfaces_typed_throttled_error() -> None:
    ddb = FakeTable(("PK", "SK"), throttle_first=99)
    table = ReadThroughTable(ddb, None, backoff=_FAST_BACKOFF)

    with pytest.raises(ThrottledError):
        table.get_item(Key={"PK": "USER#1", "SK": "META"})


# -- CoreTable ---------------------------------------------------------------


def _core(clock: list[int] | None = None) -> tuple[CoreTable, FakeTable]:
    ddb = FakeTable(("PK", "SK"))
    ticks = iter(clock or range(1, 10_000))
    core = CoreTable(ddb, None, backoff=_FAST_BACKOFF, clock_ms=lambda: next(ticks))
    return core, ddb


def test_core_put_new_and_get_round_trip() -> None:
    core, _ = _core()
    core.put_new("GUILD#1", "META", "Guild", {"name": "hd"})

    item = core.require("GUILD#1", "META")

    assert item["entityType"] == "Guild"
    assert item["data"] == {"name": "hd"}
    assert item["version"] == 1


def test_core_require_missing_raises() -> None:
    core, _ = _core()
    with pytest.raises(ItemNotFoundError):
        core.require("GUILD#1", "META")


def test_core_optimistic_update_increments_version() -> None:
    core, _ = _core()
    core.put_new("GUILD#1", "CONFIG", "Config", {"vol": 1})

    updated = core.update_with_lock(
        "GUILD#1", "CONFIG", lambda d: {**d, "vol": 2}
    )

    assert updated["version"] == 2
    assert updated["data"] == {"vol": 2}
    assert core.require("GUILD#1", "CONFIG")["data"] == {"vol": 2}


def test_core_update_creates_when_absent_with_entity_type() -> None:
    core, _ = _core()
    created = core.update_with_lock(
        "USER#9", "META", lambda d: {**d, "seen": True}, entity_type="User"
    )
    assert created["version"] == 1
    assert created["entityType"] == "User"


def test_core_update_missing_entity_type_raises() -> None:
    core, _ = _core()
    with pytest.raises(ValueError):
        core.update_with_lock("USER#9", "META", lambda d: d)


def test_core_optimistic_lock_conflict_raises_after_retries() -> None:
    """A mutator that mutates the row underneath forces perpetual conflict."""
    ddb = FakeTable(("PK", "SK"))
    core = CoreTable(ddb, None, backoff=_FAST_BACKOFF, lock_retries=2)
    core.put_new("GUILD#1", "META", "Guild", {"n": 0})

    def racing_mutator(data: dict[str, Any]) -> dict[str, Any]:
        # Simulate a concurrent writer bumping the version between our read
        # and our conditional put.
        stored = ddb._items[("GUILD#1", "META")]
        stored["version"] = int(stored["version"]) + 1
        return {**data, "n": data.get("n", 0) + 1}

    with pytest.raises(OptimisticLockError):
        core.update_with_lock("GUILD#1", "META", racing_mutator)


def test_core_query_gsi1_with_prefix() -> None:
    core, _ = _core()
    core.put_new(
        "USER#1", "META", "User", {"n": "a"}, gsi1pk="DISCORD#42", gsi1sk="USER#1"
    )
    core.put_new(
        "APPT#1", "META", "Appointment", {}, gsi1pk="DISCORD#42", gsi1sk="APPT#1"
    )

    users = core.query_gsi1("DISCORD#42", sk_prefix="USER#")

    assert len(users) == 1
    assert users[0]["PK"] == "USER#1"


def _seed_source_credential(
    ddb: FakeTable,
    sub: str,
    provider: str,
    *,
    expires_at: int,
    refresh_status: str,
) -> None:
    """Insert a SourceCredential item with an encrypted blob into the fake."""
    ddb._items[(f"USER#{sub}", f"SOURCECRED#{provider}")] = {
        "PK": f"USER#{sub}",
        "SK": f"SOURCECRED#{provider}",
        "entityType": "SourceCredential",
        "data": {
            "connected": True,
            "expires_at": expires_at,
            "refresh_status": refresh_status,
            "scope": "read",
            "enc_blob": "SECRET-CIPHERTEXT",
            "enc_key": "WRAPPED-KEY",
            "kms_key_id": "cmk-1",
        },
        "version": 1,
    }


def test_core_scan_entity_projects_status_and_excludes_enc_blob() -> None:
    core, ddb = _core()
    _seed_source_credential(ddb, "1", "spotify", expires_at=100, refresh_status="ok")
    # A non-matching entityType must be filtered out of the enumeration.
    core.put_new("GUILD#1", "META", "Guild", {"name": "hd"})

    items = list(core.scan_entity("SourceCredential"))

    assert len(items) == 1
    item = items[0]
    assert item["PK"] == "USER#1"
    assert item["SK"] == "SOURCECRED#spotify"
    assert item["entityType"] == "SourceCredential"
    # Only the two status fields are projected out of ``data``.
    assert item["data"] == {"expires_at": 100, "refresh_status": "ok"}
    # The token material is never returned by the enumeration projection.
    assert "enc_blob" not in item["data"]
    assert "enc_key" not in item["data"]
    assert "kms_key_id" not in item["data"]


def test_core_scan_entity_paginates_across_pages() -> None:
    ddb = FakeTable(("PK", "SK"), scan_page_size=2)
    core = CoreTable(ddb, None, backoff=_FAST_BACKOFF)
    for index in range(5):
        _seed_source_credential(
            ddb, str(index), "tidal", expires_at=index, refresh_status="ok"
        )

    items = list(core.scan_entity("SourceCredential"))

    # All five items are yielded despite the 2-per-page scan limit.
    assert len(items) == 5
    assert {item["PK"] for item in items} == {f"USER#{i}" for i in range(5)}
    # Every yielded item is key-projected (no ciphertext leaked across pages).
    assert all("enc_blob" not in item["data"] for item in items)


def test_core_scan_entity_empty_when_no_matches() -> None:
    core, ddb = _core()
    core.put_new("GUILD#1", "META", "Guild", {"name": "hd"})

    assert list(core.scan_entity("SourceCredential")) == []


# -- SearchCacheTable --------------------------------------------------------


def test_search_cache_write_read_round_trip() -> None:
    ddb = FakeTable(("queryKey",))
    cache = SearchCacheTable(ddb, None, backoff=_FAST_BACKOFF, clock_s=lambda: 100)
    cache.put("q1", {"tracks": [1, 2]}, ttl=200)

    assert cache.get_results("q1") == {"tracks": [1, 2]}
    assert cache.get("q1")["ttl"] == 200


def test_search_cache_write_is_idempotent() -> None:
    ddb = FakeTable(("queryKey",))
    cache = SearchCacheTable(ddb, None, backoff=_FAST_BACKOFF)
    first = cache.put("q1", {"tracks": [1]}, ttl=500)
    second = cache.put("q1", {"tracks": [1]}, ttl=500)

    assert first == second
    assert ddb._items[("q1",)] == first


# -- SessionTable ------------------------------------------------------------


def test_session_put_state_round_trip_and_version() -> None:
    ddb = FakeTable(("PK", "SK"))
    session = SessionTable(ddb, None, backoff=_FAST_BACKOFF)

    created = session.put_state(
        "GUILD#1", "SESSION", lambda s: {**s, "channel": 111}
    )
    assert created["version"] == 1

    updated = session.put_state(
        "GUILD#1", "SESSION", lambda s: {**s, "channel": 222}
    )
    assert updated["version"] == 2
    assert session.get("GUILD#1", "SESSION")["state"]["channel"] == 222


def test_session_optimistic_lock_conflict_raises() -> None:
    ddb = FakeTable(("PK", "SK"))
    session = SessionTable(ddb, None, backoff=_FAST_BACKOFF, lock_retries=1)
    session.put_state("GUILD#1", "SESSION", lambda s: {**s, "n": 0})

    def racing(state: dict[str, Any]) -> dict[str, Any]:
        stored = ddb._items[("GUILD#1", "SESSION")]
        stored["version"] = int(stored["version"]) + 1
        return {**state, "n": state.get("n", 0) + 1}

    with pytest.raises(OptimisticLockError):
        session.put_state("GUILD#1", "SESSION", racing)
