"""Unit / example tests for the ephemeral-builder fallback wiring tool (task 16.3).

Feature: hellodj-nix-native-delivery, Requirements 6.6, 6.7, 6.8, 6.9, 7.5, 7.6.

These tests exercise ``tools/ephemeral_builder.py`` — the executable the GitHub
Actions Nix build workflow (``.github/workflows/nix-build.yml``) invokes to wire
the FALLBACK on-demand ephemeral aarch64 builder's safety guarantees and the
rebuild paths. It is a thin wrapper around two pure, property-tested decision
functions, so here we assert the *workflow-level* behaviour:

* ``teardown`` — the fallback builder is torn down within the <=300 s deadline
  and <=10800 s hard cap; a CONFIRMED stop records the resource id + teardown
  timestamp (R6.9); an UNCONFIRMED stop exits non-zero and emits an alert naming
  the still-running compute (R6.8).
* ``rebuild-decision`` — a rebuild + re-push is permitted iff an explicit rebuild
  was requested (R7.5) OR the cache was unreachable (timeout / exhausted
  retries) (R7.6); a healthy cache with no explicit request forces no rebuild.

The pure decision functions themselves are covered by the Property 4
(``ephemeral_teardown``) and Property 7 (``cache_fetch_policy``) Hypothesis
tests; these are the tool/workflow-level example tests.

Validates: Requirements 6.6, 6.7, 6.8, 6.9, 7.5, 7.6
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# The tools live under platform/tools/ (a sibling of components/). Load them by
# path so the tests do not depend on tools/ being an installed package.
_PLATFORM_ROOT = Path(__file__).resolve().parents[3]
_COMPONENTS_ROOT = _PLATFORM_ROOT / "components"
if str(_COMPONENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_COMPONENTS_ROOT))


def _load(mod_name: str) -> object:
    path = _PLATFORM_ROOT / "tools" / f"{mod_name}.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass field-type resolution can find the
    # module's namespace (the tool defines a frozen dataclass).
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


tool = _load("ephemeral_builder")


# ---------------------------------------------------------------------------
# teardown decision (R6.6-6.9)
# ---------------------------------------------------------------------------


def test_confirmed_stop_records_id_and_timestamp_no_alert() -> None:
    """A confirmed stop retains id + timestamp and emits no alert (R6.9).

    Validates: Requirements 6.6, 6.7, 6.9
    """
    result = tool.teardown_decision("i-0abc", True, "2026-01-02T03:04:05Z")
    assert result.confirmed_stopped is True
    assert result.alert_emitted is False
    assert result.resource_id == "i-0abc"
    assert result.teardown_timestamp == "2026-01-02T03:04:05Z"


def test_unconfirmed_stop_emits_alert_naming_resource() -> None:
    """An unconfirmed stop emits an alert naming the still-running compute (R6.8).

    Validates: Requirements 6.8
    """
    result = tool.teardown_decision("i-runaway", False, "2026-01-02T03:04:05Z")
    assert result.alert_emitted is True
    assert result.resource_id == "i-runaway"


def test_teardown_cli_confirmed_exits_zero(capsys) -> None:
    """`teardown --stopped-confirmed true` exits 0 and records the outcome (R6.9)."""
    rc = tool.main(
        [
            "teardown",
            "--resource-id",
            "i-0abc",
            "--stopped-confirmed",
            "true",
            "--timestamp",
            "2026-01-02T03:04:05Z",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "i-0abc" in out
    assert "confirmed stopped" in out


def test_teardown_cli_unconfirmed_exits_nonzero_with_alert(capsys) -> None:
    """`teardown --stopped-confirmed false` exits non-zero + alerts (R6.8).

    Validates: Requirements 6.8
    """
    rc = tool.main(
        [
            "teardown",
            "--resource-id",
            "i-runaway",
            "--stopped-confirmed",
            "false",
            "--timestamp",
            "2026-01-02T03:04:05Z",
        ]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "::error::ALERT" in out
    assert "i-runaway" in out


def test_teardown_over_cap_deadline_rejected() -> None:
    """A teardown deadline over the 300 s cap is rejected (R6.6)."""
    rc = tool.main(
        [
            "teardown",
            "--resource-id",
            "i-0abc",
            "--stopped-confirmed",
            "true",
            "--timestamp",
            "t",
            "--teardown-deadline-seconds",
            "301",
        ]
    )
    assert rc == 2


def test_teardown_over_cap_lifetime_rejected() -> None:
    """A max lifetime over the 10800 s cap is rejected (R6.7)."""
    rc = tool.main(
        [
            "teardown",
            "--resource-id",
            "i-0abc",
            "--stopped-confirmed",
            "true",
            "--timestamp",
            "t",
            "--max-lifetime-seconds",
            "10801",
        ]
    )
    assert rc == 2


# ---------------------------------------------------------------------------
# rebuild decision (R7.5 explicit / R7.6 cache-unreachable)
# ---------------------------------------------------------------------------


def test_healthy_cache_no_explicit_reuses() -> None:
    """A healthy cache with no explicit request forces no rebuild (R7.2/7.3)."""
    decision = tool.rebuild_decision(
        explicit_rebuild=False, cache_responded=True, retries=0
    )
    assert decision.rebuild is False
    assert decision.cache_outcome.rebuilt_locally is False


def test_explicit_rebuild_permits_rebuild_even_when_cache_healthy() -> None:
    """An explicit rebuild permits rebuild + re-push despite a healthy cache (R7.5).

    Validates: Requirements 7.5
    """
    decision = tool.rebuild_decision(
        explicit_rebuild=True, cache_responded=True, retries=0
    )
    assert decision.rebuild is True
    assert decision.explicit is True


def test_cache_timeout_permits_recorded_local_rebuild() -> None:
    """A cache timeout permits a recorded local rebuild (R7.6).

    Validates: Requirements 7.6
    """
    decision = tool.rebuild_decision(
        explicit_rebuild=False, cache_responded=False, retries=1
    )
    assert decision.rebuild is True
    assert decision.cache_outcome.rebuilt_locally is True
    assert "unreachable" in decision.reason


def test_exhausted_retries_permits_recorded_local_rebuild() -> None:
    """Exhausting the 3 consecutive retries permits a recorded rebuild (R7.6).

    Validates: Requirements 7.6
    """
    from hellodj_platform_logic.binary_cache import CACHE_RETRY_LIMIT

    decision = tool.rebuild_decision(
        explicit_rebuild=False, cache_responded=True, retries=CACHE_RETRY_LIMIT
    )
    assert decision.rebuild is True
    assert decision.cache_outcome.retries_exhausted is True


def test_rebuild_decision_cli_emits_machine_readable_flag(capsys) -> None:
    """`rebuild-decision` prints a `rebuild=<bool>` line the workflow consumes."""
    rc = tool.main(["rebuild-decision", "--cache-responded", "false", "--retries", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "rebuild=true" in out


def test_rebuild_decision_cli_default_reuse(capsys) -> None:
    """`rebuild-decision` with no flags (healthy cache) yields reuse."""
    rc = tool.main(["rebuild-decision"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "rebuild=false" in out


# ---------------------------------------------------------------------------
# self-test + usage
# ---------------------------------------------------------------------------


def test_self_test_passes() -> None:
    """The tool's built-in --self-test smoke check passes."""
    assert tool.main(["--self-test"]) == 0


def test_no_args_shows_usage() -> None:
    """No subcommand is an operational error (exit 2)."""
    assert tool.main([]) == 2


def test_unknown_command_shows_usage() -> None:
    """An unknown subcommand is an operational error (exit 2)."""
    assert tool.main(["frobnicate"]) == 2
