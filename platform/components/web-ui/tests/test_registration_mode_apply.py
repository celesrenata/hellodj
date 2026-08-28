"""Helper-level tests for ``registration_mode.apply_mode_change`` (task 2.1).

Exercises the audit-then-persist apply helper directly against an in-memory
fake ``ConfigStore`` plus a spy ``CoreTable``, with no AWS/Flask/Cognito. The
one named property here is the idempotent no-op:

Feature: registration-mode-control
Property 8: Unchanged submission is idempotent (R5.2).

For each current mode, calling ``apply_mode_change`` with ``requested ==
current`` must return the current mode, must not persist a change via
``set_global``, and must never write an audit row via ``put_new``.

Validates: Requirements 5.2
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from config_store import CONFIG_SK, GLOBAL_CONFIG_PK
from registration_mode import (
    CLOSED,
    CONFIG_KEY,
    OPEN,
    VALID_MODES,
    apply_mode_change,
)


class _SpyConfigStore:
    """In-memory fake ``ConfigStore`` that records ``set_global`` calls.

    Holds a mutable global-config payload seeded with a starting mode and spies
    on ``set_global`` so a test can assert the mode was (or was not) persisted.
    Only the surface ``apply_mode_change`` touches — ``get_global`` /
    ``set_global`` plus the ``core_table`` accessor — is implemented.
    """

    def __init__(self, core_table: Any, initial: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = dict(initial or {})
        self._core = core_table
        self.set_global_calls: list[dict[str, Any]] = []

    @property
    def core_table(self) -> Any:
        return self._core

    def get_global(self) -> dict[str, Any]:
        return dict(self._data)

    def set_global(self, values: dict[str, Any]) -> dict[str, Any]:
        self.set_global_calls.append(dict(values))
        self._data.update(values)
        return dict(self._data)


class _SpyCoreTable:
    """Spy ``CoreTable`` that records every ``put_new`` (audit-write) call."""

    def __init__(self) -> None:
        self.put_new_calls: list[dict[str, Any]] = []

    def put_new(
        self,
        pk: str,
        sk: str,
        entity_type: str,
        data: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.put_new_calls.append(
            {"pk": pk, "sk": sk, "entity_type": entity_type, "data": dict(data)}
        )
        return {"PK": pk, "SK": sk}


@settings(max_examples=100)
@given(current=st.sampled_from(VALID_MODES))
def test_property8_unchanged_submission_is_idempotent(current: str) -> None:
    """Property 8 — submitting the current mode returns it, persists nothing,
    and writes no audit row.

    Feature: registration-mode-control, Property 8: Unchanged submission is
    idempotent.

    Validates: Requirements 5.2
    """
    core = _SpyCoreTable()
    store = _SpyConfigStore(core, {CONFIG_KEY: current})

    result = apply_mode_change(
        store, core, requested=current, admin_sub="admin-sub-1"
    )

    assert result == current
    # No change was persisted (set_global never called).
    assert store.set_global_calls == []
    # No audit row was written.
    assert core.put_new_calls == []
    # The stored mode is still the original value.
    assert store.get_global()[CONFIG_KEY] == current


@settings(max_examples=100)
@given(current=st.sampled_from(VALID_MODES), pad=st.text(alphabet=" \t", max_size=3))
def test_property8_noop_holds_across_casing_and_whitespace(
    current: str, pad: str
) -> None:
    """Property 8 — a request that *normalizes* to the current mode (mixed
    casing / surrounding whitespace) is still a no-op.

    Feature: registration-mode-control, Property 8: Unchanged submission is
    idempotent.

    Validates: Requirements 5.2
    """
    core = _SpyCoreTable()
    store = _SpyConfigStore(core, {CONFIG_KEY: current})
    requested = f"{pad}{current.lower()}{pad}"

    result = apply_mode_change(
        store, core, requested=requested, admin_sub="admin-sub-1"
    )

    assert result == current
    assert store.set_global_calls == []
    assert core.put_new_calls == []


def test_noop_from_default_closed_when_unset() -> None:
    """A CLOSED submission against an *unset* config (which resolves to the
    secure default CLOSED) is a no-op — no persist, no audit."""
    core = _SpyCoreTable()
    store = _SpyConfigStore(core, {})  # empty payload -> current_mode == CLOSED

    result = apply_mode_change(
        store, core, requested=CLOSED, admin_sub="admin-sub-1"
    )

    assert result == CLOSED
    assert store.set_global_calls == []
    assert core.put_new_calls == []


def test_change_writes_audit_before_persist() -> None:
    """A real change (OPEN from the default CLOSED) writes exactly one audit row
    to CONFIG#GLOBAL and then persists the new mode (sanity check around the
    no-op property)."""
    core = _SpyCoreTable()
    store = _SpyConfigStore(core, {CONFIG_KEY: CLOSED})

    result = apply_mode_change(
        store, core, requested=OPEN, admin_sub="admin-sub-1", now="2026-01-01T00:00:00+00:00"
    )

    assert result == OPEN
    assert len(core.put_new_calls) == 1
    audit = core.put_new_calls[0]
    assert audit["pk"] == GLOBAL_CONFIG_PK
    assert audit["sk"].startswith("REGMODEAUDIT#2026-01-01T00:00:00+00:00#")
    assert audit["entity_type"] == "RegistrationModeAudit"
    assert audit["data"] == {
        "admin_sub": "admin-sub-1",
        "old": CLOSED,
        "new": OPEN,
        "at": "2026-01-01T00:00:00+00:00",
    }
    assert store.set_global_calls == [{CONFIG_KEY: OPEN}]
    # SK constants referenced to keep the import meaningful / documented.
    assert CONFIG_SK == "CONFIG"
