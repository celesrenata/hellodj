"""Property-based test for data-layer persistence round-trip and idempotence.

Feature: aws-saas-replatform, Property 10

Property 10 (Data-layer persistence round-trip and idempotence):
    For any valid session/queue record or search-cache record, writing the
    record to the DynamoDB data-access layer and then reading it back returns
    an equivalent record; and writing the same search-cache record more than
    once leaves the stored value identical to writing it once (idempotence).

This test runs the real ``SearchCacheTable`` and ``SessionTable`` repositories
against **moto**'s in-process DynamoDB (``mock_aws``), creating real boto3
resource ``Table`` objects (``hellodj-search-cache`` keyed by ``queryKey`` and
``hellodj-session`` keyed by ``PK``/``SK`` per the design schema) and injecting
them into the repositories. A fresh moto backend and fresh tables are created
inside each Hypothesis example so DynamoDB state never leaks across examples.

boto3's DynamoDB *resource* layer serializes numbers to ``decimal.Decimal`` on
the way out, so the generators here restrict numeric leaves to integers and the
equivalence check normalizes both sides through the same DynamoDB type mapping
(int -> Decimal). This models "an equivalent record" faithfully: the value read
back is the value written, modulo DynamoDB's canonical number representation.

Validates: Requirements 7.4, 7.5
"""

from __future__ import annotations

import contextlib
from decimal import Decimal
from typing import Any

import boto3
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from moto import mock_aws

from hellodj_platform_logic.data_access import (
    BackoffConfig,
    SearchCacheTable,
    SessionTable,
)

_REGION = "us-east-1"

# No-sleep backoff so a throttle (there won't be any under moto) never stalls
# a property example.
_FAST_BACKOFF = BackoffConfig(
    max_attempts=3,
    base_delay_seconds=0.0,
    max_delay_seconds=0.0,
    sleep=lambda _s: None,
)


# ---------------------------------------------------------------------------
# Generators for valid records
# ---------------------------------------------------------------------------
# DynamoDB (via the boto3 resource) accepts strings, numbers, bools, null,
# and nested maps/lists. Numbers come back as ``Decimal``. We keep numeric
# leaves as integers (which round-trip losslessly through Decimal) and keep
# map keys as non-empty strings (DynamoDB map attribute names must be
# non-empty). Empty strings are valid DynamoDB string values (since 2020) and
# are included to exercise that edge.

_int_values = st.integers(min_value=-(10**15), max_value=10**15)
_str_values = st.text(max_size=40)
_leaf = st.one_of(_str_values, _int_values, st.booleans(), st.none())

_map_keys = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=16,
)


def _payloads() -> st.SearchStrategy[dict[str, Any]]:
    """Generate arbitrary DynamoDB-storable map payloads (possibly nested)."""
    return st.dictionaries(
        keys=_map_keys,
        values=st.recursive(
            _leaf,
            lambda children: st.one_of(
                st.lists(children, max_size=4),
                st.dictionaries(_map_keys, children, max_size=4),
            ),
            max_leaves=8,
        ),
        max_size=6,
    )


_query_keys = st.text(min_size=1, max_size=64)
_guild_ids = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=20
)
_ttls = st.integers(min_value=0, max_value=2_000_000_000)


# ---------------------------------------------------------------------------
# DynamoDB-canonical normalization for the equivalence check
# ---------------------------------------------------------------------------
def _canonical(value: Any) -> Any:
    """Map a Python value to DynamoDB's canonical read-back representation.

    Numbers read back from the DynamoDB resource are ``Decimal``; bools stay
    bools; strings, ``None``, lists, and maps recurse. Applying this to both
    the written value and the read value lets us assert equivalence without
    being tripped by the int -> Decimal number mapping.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    return value


@contextlib.contextmanager
def _search_cache_table():
    """Yield a ``SearchCacheTable`` backed by a fresh moto DynamoDB table."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=_REGION)
        table = ddb.create_table(
            TableName="hellodj-search-cache",
            KeySchema=[{"AttributeName": "queryKey", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "queryKey", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        yield SearchCacheTable(table, None, backoff=_FAST_BACKOFF)


@contextlib.contextmanager
def _session_table():
    """Yield a ``SessionTable`` backed by a fresh moto DynamoDB table."""
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=_REGION)
        table = ddb.create_table(
            TableName="hellodj-session",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()
        yield SessionTable(table, None, backoff=_FAST_BACKOFF)


# ---------------------------------------------------------------------------
# Property 10a: search-cache write-then-read round-trip
# ---------------------------------------------------------------------------
@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(query_key=_query_keys, results=_payloads(), ttl=_ttls)
def test_search_cache_round_trip(
    query_key: str, results: dict[str, Any], ttl: int
) -> None:
    """A written search-cache record reads back equivalent.

    Feature: aws-saas-replatform, Property 10
    Validates: Requirements 7.4
    """
    with _search_cache_table() as cache:
        cache.put(query_key, results, ttl=ttl)

        stored = cache.get(query_key)
        assert stored is not None
        assert stored["queryKey"] == query_key
        assert stored["ttl"] == Decimal(ttl)
        assert _canonical(stored["results"]) == _canonical(results)
        assert _canonical(cache.get_results(query_key)) == _canonical(results)


# ---------------------------------------------------------------------------
# Property 10b: search-cache write idempotence
# ---------------------------------------------------------------------------
@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(query_key=_query_keys, results=_payloads(), ttl=_ttls)
def test_search_cache_write_is_idempotent(
    query_key: str, results: dict[str, Any], ttl: int
) -> None:
    """Writing the same search-cache record twice leaves the store identical.

    Writing (queryKey, results, ttl) once and then again yields the same
    returned item and the same stored item (idempotence).

    Feature: aws-saas-replatform, Property 10
    Validates: Requirements 7.5
    """
    with _search_cache_table() as cache:
        first = cache.put(query_key, results, ttl=ttl)
        after_first = cache.get(query_key)

        second = cache.put(query_key, results, ttl=ttl)
        after_second = cache.get(query_key)

        # The write is a pure function of its inputs: identical returns.
        assert first == second
        # The stored value is identical whether written once or twice.
        assert after_first == after_second
        assert _canonical(after_first["results"]) == _canonical(results)


# ---------------------------------------------------------------------------
# Property 10c: session/queue write-then-read round-trip
# ---------------------------------------------------------------------------
@settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(guild=_guild_ids, seq=st.integers(min_value=0, max_value=9999), state=_payloads())
def test_session_round_trip(guild: str, seq: int, state: dict[str, Any]) -> None:
    """A written session/queue record reads back equivalent.

    Feature: aws-saas-replatform, Property 10
    Validates: Requirements 7.4
    """
    pk = f"GUILD#{guild}"
    sort_key = f"QUEUE#{seq}"

    with _session_table() as session:
        committed = session.put_state(pk, sort_key, lambda _s: state)

        stored = session.get(pk, sort_key)
        assert stored is not None
        assert stored["PK"] == pk
        assert stored["SK"] == sort_key
        assert stored["version"] == Decimal(1)
        assert _canonical(stored["state"]) == _canonical(state)
        # The committed item the writer returned matches what was persisted.
        assert _canonical(committed["state"]) == _canonical(state)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
