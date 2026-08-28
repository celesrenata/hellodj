"""Unit test for the read-only ``ConfigStore.core_table`` accessor (task 3).

The registration-mode audit write reuses the same ``CoreTable`` the config is
stored on. Exposing it via a narrow property avoids reaching into the private
``_core`` attribute; this test pins that the accessor returns the exact instance
passed to the constructor.

Requirements: 5.1
"""

from __future__ import annotations

from typing import Any

from hellodj_platform_logic.data_access import CoreTable

from config_store import ConfigStore


class _FakeTable:
    """Minimal ``TableLike`` stand-in (no AWS); identity is all that matters."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}


def test_core_table_returns_injected_instance() -> None:
    core = CoreTable(_FakeTable())
    assert ConfigStore(core).core_table is core
