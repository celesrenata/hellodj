"""Stale-pin report decision (R6.1).

This module holds the pure decision function that produces the stale-pin report
for the dependency-bump mechanism. Per the design (Components "Bump outdated
dependencies through the existing pin workflow"), the report enumerates every
``pins.toml`` entry whose pinned identifier does not equal the current upstream
identifier resolved for that entry, listing for each such entry its pinned
identifier and its current upstream identifier (R6.1).

"Stale" is defined by *exactly* the same ``pinned != upstream`` comparison
:func:`hellodj_platform_logic.pinning.verify_pin` performs -- a stale entry is
one ``verify_pin`` would reject. Concretely, ``verify_pin`` accepts iff the
resolved upstream identifier equals the pinned identifier, rejects when a
resolved upstream identifier differs from the pinned identifier, and *fails*
(rather than rejects) when the upstream cannot be resolved (``None``). So the
stale set is precisely the reject set: entries whose upstream is resolved
(non-``None``) *and* differs from the pinned identifier. An entry whose upstream
cannot be resolved is **not** stale here -- it is a resolution failure surfaced
separately -- so it is excluded from the report.

Like the other decision modules in this package (``pinning``,
``python_migration``, ``binary_cache``), this function is pure: the upstream
identifiers are resolved by the caller (reusing the same ``pins.upstream.toml``
resolution the pin gate uses) and injected, so it performs no live network or
git calls and the correctness property (P6) can exercise it directly.

Design references:
    * Components -- "Stale-pin detection/report mechanism (R6.1)": enumerate
      entries where a resolved upstream identifier differs from the pinned
      identifier (exactly the set ``verify_pin`` would reject), listing both
      identifiers; unresolved upstream is excluded (resolution failure).
    * Data Models -- ``StalePin``.
    * Correctness Property 6: The stale-pin report lists exactly the pins whose
      pinned identifier differs from upstream.

Requirements: 6.1
"""

from __future__ import annotations

from collections.abc import Mapping

from hellodj_platform_logic.pinning import verify_pin
from hellodj_platform_logic.types import FlakeInputPin, StalePin

__all__ = ["stale_pins"]


def stale_pins(
    pins: Mapping[str, FlakeInputPin],
    upstream: Mapping[str, str | None],
) -> list[StalePin]:
    """Enumerate the pins whose pinned identifier differs from upstream (R6.1).

    Implements Property 6 / R6.1. Produces the stale-pin report: one
    :class:`~hellodj_platform_logic.types.StalePin` for every entry whose
    pinned identifier does not equal the current upstream identifier resolved
    for that entry, carrying that entry's pinned identifier and its current
    upstream identifier.

    "Stale" is the exact ``pinned != upstream`` comparison
    :func:`hellodj_platform_logic.pinning.verify_pin` performs -- an entry is
    stale iff ``verify_pin`` would *reject* it. This function therefore defers
    the per-entry decision to ``verify_pin`` verbatim rather than re-deriving
    the comparison, so the two can never drift:

    * When the resolved upstream identifier is present and **equals** the
      pinned identifier, ``verify_pin`` accepts -- the entry is up to date and
      is omitted from the report.
    * When the resolved upstream identifier is present and **differs** from the
      pinned identifier, ``verify_pin`` rejects -- the entry is stale and is
      reported, listing both the pinned identifier and the current upstream
      identifier it differs from.
    * When the upstream identifier is unresolved (``None``), ``verify_pin``
      *fails* (a distinct outcome from reject) -- this is a resolution failure,
      not a stale pin, and is **excluded** from the report. Such an entry is
      also skipped when it has no ``upstream`` mapping entry at all.

    A stale entry always has a resolved, non-``None`` upstream identifier, so
    the ``StalePin.upstream_identifier`` field is always a concrete identifier.

    The report is emitted in the iteration order of ``pins`` so it is
    deterministic for a given input mapping.

    Args:
        pins: The pinned flake inputs keyed by ``input_name`` (the ``pins.toml``
            entries), each carrying its ``pinned_identifier``.
        upstream: The current upstream identifier resolved for each entry, keyed
            by the same ``input_name``. A value of ``None`` -- or a missing key
            -- means the upstream could not be resolved for that entry and the
            entry is excluded from the stale report (surfaced separately as a
            resolution failure).

    Returns:
        The list of :class:`~hellodj_platform_logic.types.StalePin` entries --
        exactly the pins ``verify_pin`` would reject -- each listing its pinned
        identifier and the current upstream identifier it differs from (R6.1).

    Requirements: 6.1
    """
    report: list[StalePin] = []
    for input_name, pin in pins.items():
        upstream_identifier = upstream.get(input_name)
        verification = verify_pin(pin, upstream_identifier)
        # A stale entry is exactly one verify_pin would reject: upstream is
        # resolved (non-None) and differs from the pinned identifier. verify_pin
        # returns accepted=False for BOTH a mismatch (reject) and an unresolved
        # upstream (fail); the unresolved case carries upstream_identifier=None,
        # so guarding on a non-None upstream_identifier isolates the reject set.
        if not verification.accepted and verification.upstream_identifier is not None:
            report.append(
                StalePin(
                    input_name=pin.input_name,
                    pinned_identifier=pin.pinned_identifier,
                    upstream_identifier=verification.upstream_identifier,
                )
            )
    return report
