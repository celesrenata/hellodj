"""Tests for the AWS multi-bot runtime credential source (pool ∩ claims).

Covers :class:`playback_orchestrator.instance_runtime.PoolCredentialSource`
against a fake Secrets Manager client + an in-memory ``CoreTable`` (the same
fake-table shape the web-ui bot-app-pool tests use) — no live AWS.

Asserts (Requirements 1.1, 1.2, 1.3, 1.5):

* ``instances_for_guild`` returns exactly the pool apps that are BOTH claimed by
  the guild AND hold a bot token (pool ∩ claims ∩ has-token), in pool order.
* A claimed-but-tokenless pool entry is skipped (R1.3).
* A pool app that is NOT claimed by the guild is excluded (claim intersection).
* An empty / absent pool secret yields no instances, and a guild with no claims
  yields no instances (degraded → empty).
* No bot token / client secret ever appears in a ``PoolApp`` repr or a log line
  (R1.5).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from hellodj_platform_logic.data_access import CoreTable

from playback_orchestrator.instance_runtime import (
    BOTAPP_SK_PREFIX,
    PoolCredentialSource,
    bot_app_pool_secret_name,
    guild_pk,
)

_GID = "42"
_SECRET = "super-secret-value-should-never-render"
_TOKEN_100 = "tok-100-should-never-render"
_TOKEN_102 = "tok-102-should-never-render"

_POOL = [
    {"label": "HelloDJ-00", "client_id": "100",
     "client_secret": _SECRET, "bot_token": _TOKEN_100},
    # 101 has NO bot token → tokenless, must be skipped even when claimed.
    {"label": "HelloDJ-01", "client_id": "101",
     "client_secret": _SECRET, "bot_token": ""},
    {"label": "HelloDJ-02", "client_id": "102",
     "client_secret": _SECRET, "bot_token": _TOKEN_102},
]


@dataclass
class _FakeSecrets:
    """Minimal Secrets Manager stub returning a fixed ``SecretString``.

    ``raise_on_get`` simulates an absent/denied secret (the source must degrade
    to an empty pool rather than propagate).
    """

    payload: str
    raise_on_get: bool = False
    requested: list[str] = field(default_factory=list)

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        self.requested.append(kwargs.get("SecretId", ""))
        if self.raise_on_get:
            raise RuntimeError("secret not found")
        return {"SecretString": self.payload}


@dataclass
class _FakeTable:
    """In-memory DynamoDB table supporting get/query/put keyed on PK/SK."""

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


def _source(
    pool_entries: Any = _POOL, *, raise_on_get: bool = False
) -> tuple[PoolCredentialSource, CoreTable]:
    core = CoreTable(_FakeTable())
    payload = "" if pool_entries is None else json.dumps(pool_entries)
    secrets = _FakeSecrets(payload, raise_on_get=raise_on_get)
    return PoolCredentialSource(secrets, core, stage="beta"), core


def _claim(core: CoreTable, guild_id: str, client_id: str) -> None:
    """Write a BotAppClaim item the way the web-ui assignment flow does."""
    core.put_new(
        guild_pk(guild_id),
        f"{BOTAPP_SK_PREFIX}{client_id}",
        "BotAppClaim",
        {"client_id": client_id, "label": f"L-{client_id}", "claimed_at": 1},
    )


# ── secret name resolution ───────────────────────────────────────────────────


def test_secret_name_resolves_by_stage():
    src, _core = _source()
    assert src.secret_name == "hellodj/beta/bot-app-pool"
    assert bot_app_pool_secret_name("production") == (
        "hellodj/production/bot-app-pool"
    )


def test_pool_reads_the_stage_secret():
    src, _core = _source()
    src.pool()
    # The source asked Secrets Manager for the stage-scoped pool secret.
    assert src._secrets.requested == ["hellodj/beta/bot-app-pool"]  # noqa: SLF001


def test_pool_excludes_primary_bot_client_id():
    """The Primary_Bot id is never a connectable secondary (no double identify).

    Even if the pool secret lists the Primary alongside the secondaries, the
    runtime filters it out via ``primary_client_id`` so it never opens a second
    gateway for the command-owner's application id.
    """
    core = CoreTable(_FakeTable())
    src = PoolCredentialSource(
        _FakeSecrets(json.dumps(_POOL)),
        core,
        stage="beta",
        primary_client_id="100",
    )
    assert [a.client_id for a in src.pool()] == ["101", "102"]


def test_excluded_primary_is_not_connectable_even_when_claimed():
    """A guild that claims the Primary id gets no instance for it.

    Belt-and-suspenders: excluding at the pool means the pool ∩ claims ∩ token
    intersection can never include the Primary, so a stray claim on it yields
    nothing.
    """
    core = CoreTable(_FakeTable())
    src = PoolCredentialSource(
        _FakeSecrets(json.dumps(_POOL)),
        core,
        stage="beta",
        primary_client_id="100",
    )
    _claim(core, _GID, "100")  # claim the Primary (should be ignored)
    _claim(core, _GID, "102")  # claim a real secondary
    assert [a.client_id for a in src.instances_for_guild(_GID)] == ["102"]


# ── claim intersection ───────────────────────────────────────────────────────


def test_instances_are_pool_intersect_claims_with_token():
    src, core = _source()
    # Claim 100 (token) and 102 (token); 101 is NOT claimed.
    _claim(core, _GID, "100")
    _claim(core, _GID, "102")
    instances = src.instances_for_guild(_GID)
    # Both claimed apps have tokens → both connectable, in pool order.
    assert [a.client_id for a in instances] == ["100", "102"]


def test_unclaimed_pool_app_is_excluded():
    src, core = _source()
    # Only 100 is claimed; 102 is in the pool but unclaimed.
    _claim(core, _GID, "100")
    instances = src.instances_for_guild(_GID)
    assert [a.client_id for a in instances] == ["100"]


def test_tokenless_claimed_app_is_skipped(caplog):
    src, core = _source()
    # Claim 100 (token) and 101 (NO token) → 101 must be skipped (R1.3).
    _claim(core, _GID, "100")
    _claim(core, _GID, "101")
    with caplog.at_level(logging.WARNING):
        instances = src.instances_for_guild(_GID)
    assert [a.client_id for a in instances] == ["100"]
    # The skip was logged, and the log carried no token material (R1.5).
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "101" in joined
    assert _SECRET not in joined
    assert _TOKEN_100 not in joined


def test_claim_for_app_not_in_pool_is_ignored():
    src, core = _source()
    # A stale claim for an app id that is no longer in the pool.
    _claim(core, _GID, "999")
    _claim(core, _GID, "100")
    instances = src.instances_for_guild(_GID)
    assert [a.client_id for a in instances] == ["100"]


# ── empty / absent pool → empty ─────────────────────────────────────────────


def test_no_claims_yields_no_instances():
    src, _core = _source()
    # Pool is populated but the guild has claimed nothing.
    assert src.instances_for_guild(_GID) == []


def test_empty_pool_secret_yields_no_instances():
    src, core = _source(pool_entries=None)  # empty SecretString
    _claim(core, _GID, "100")
    assert src.pool() == []
    assert src.instances_for_guild(_GID) == []


def test_absent_pool_secret_degrades_to_empty():
    src, core = _source(raise_on_get=True)
    _claim(core, _GID, "100")
    # get_secret_value raises → degrade to empty pool, never propagate.
    assert src.pool() == []
    assert src.instances_for_guild(_GID) == []


def test_malformed_pool_secret_degrades_to_empty():
    src, core = _source(pool_entries="not-a-json-array")
    _claim(core, _GID, "100")
    assert src.pool() == []
    assert src.instances_for_guild(_GID) == []


# ── claimed_client_ids ───────────────────────────────────────────────────────


def test_claimed_client_ids_reads_from_sort_key():
    src, core = _source()
    _claim(core, _GID, "100")
    _claim(core, _GID, "102")
    assert src.claimed_client_ids(_GID) == {"100", "102"}
    # A different guild's claims are isolated.
    assert src.claimed_client_ids("other") == set()


# ── credential safety (R1.5) ─────────────────────────────────────────────────


def test_pool_app_repr_never_leaks_secrets():
    src, core = _source()
    _claim(core, _GID, "100")
    (app,) = src.instances_for_guild(_GID)
    text = repr(app)
    assert _SECRET not in text
    assert _TOKEN_100 not in text
    # The public label + client id are still present for diagnostics.
    assert "HelloDJ-00" in text
    assert "100" in text
