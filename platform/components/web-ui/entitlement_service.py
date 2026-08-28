"""Per-user entitlement storage over the ``hellodj-core`` single table.

:class:`EntitlementService` is the web-ui service that reads and writes a user's
entitlement record, AI cost tally, audit history, and the shared AI pricing
configuration on the shared
:class:`hellodj_platform_logic.data_access.CoreTable` repository. It mirrors the
single-table conventions already used by :mod:`config_store` and
:mod:`guild_admin_service`: one item per concern under a ``USER#<sub>`` (or
``CONFIG#AIPRICING``) partition, an ``entityType`` discriminator, and the
optimistic-lock ``put_new`` / ``update_with_lock`` upsert pattern for writes.

Items (hellodj-core single table):

* Entitlement:  ``PK=USER#<sub>``            ``SK=ENTITLEMENT``     data=flags+quotas
* AI tally:     ``PK=USER#<sub>``            ``SK=AITALLY``         data=accumulated cost
* Audit entry:  ``PK=USER#<sub>``            ``SK=AUDIT#<ts>#<rand>`` data=change record
* AI pricing:   ``PK=CONFIG#AIPRICING``      ``SK=CONFIG``          data=models+markup

The effective entitlement resolution (explicit record merged over the secure
defaults) is delegated to the shared, side-effect-free :mod:`entitlements_core`
module so the web-ui and the bot agree exactly.

This module implements the read surface (``get_raw``, ``get_effective``,
``get_tally``, ``get_pricing``, ``history``) plus the audited write surface
(``set_fields``, ``reset_tally``, ``add_cost``).

Audited writes follow **write-before-apply** semantics (design "Error Handling",
Property 8). ``CoreTable`` exposes no ``TransactWriteItems`` helper, so the
service uses the design's documented fallback ordering: write the audit
entry(ies) with ``put_new`` first, and only then apply the entitlement change
via ``update_with_lock``. If the audit write fails, the entitlement record is
never touched (the change is not applied). If the audit succeeded but the
entitlement update then fails, the audit row is marked ``apply_status`` =
``"orphaned"`` and the failure is surfaced — the user's effective entitlements
are unchanged either way.

Requirements: 2.1, 2.2, 2.3, 10.4, 10.6, 15.1, 15.2, 15.3
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from hellodj_platform_logic.data_access import CoreTable

import entitlements_core

#: Quota fields validated (>= 1) before an entitlement change is applied (R12.2).
_QUOTA_FIELDS = ("max_bots_per_guild", "max_guilds")

#: ``apply_status`` recorded on an audit entry whose paired entitlement update
#: could not be committed (the fallback path when no transaction helper exists).
#: The entitlement record is left unchanged, so the audit row is orphaned.
_AUDIT_STATUS_APPLIED = "applied"
_AUDIT_STATUS_ORPHANED = "orphaned"

__all__ = [
    "ENTITLEMENT_SK",
    "AITALLY_SK",
    "AIPRICING_PK",
    "AIPRICING_SK",
    "AUDIT_SK_PREFIX",
    "ENTITLEMENT_ENTITY_TYPE",
    "AITALLY_ENTITY_TYPE",
    "AUDIT_ENTITY_TYPE",
    "AIPRICING_ENTITY_TYPE",
    "DEFAULT_AI_PRICING",
    "user_pk",
    "audit_sk",
    "seed_ai_pricing",
    "EntitlementService",
]

#: Sort key for a user's single entitlement item.
ENTITLEMENT_SK = "ENTITLEMENT"

#: Sort key for a user's AI cost tally item.
AITALLY_SK = "AITALLY"

#: Sort-key prefix shared by every audit entry under a user's partition.
AUDIT_SK_PREFIX = "AUDIT#"

#: Partition key for the single shared AI pricing configuration item.
AIPRICING_PK = "CONFIG#AIPRICING"

#: Sort key of the AI pricing configuration item (mirrors ``config_store``).
AIPRICING_SK = "CONFIG"

#: ``entityType`` discriminators for the items this service owns.
ENTITLEMENT_ENTITY_TYPE = "Entitlement"
AITALLY_ENTITY_TYPE = "AiTally"
AUDIT_ENTITY_TYPE = "EntitlementAudit"
AIPRICING_ENTITY_TYPE = "AiPricing"

#: Seed payload for the ``CONFIG#AIPRICING`` item (R10.2, R10.3).
#:
#: **Prices are data, not code.** These are per-model Amazon Bedrock unit prices
#: (USD per 1K input tokens, per 1K output tokens, and per request) plus the
#: baseline ``markup`` (1.0 == 100% == the tally charges 2x Bedrock cost, per
#: R10.2). This constant only supplies the *initial* values written when the item
#: is absent; **ops updates prices by editing the ``CONFIG#AIPRICING`` item's
#: ``data`` in place** (a data edit that takes effect at runtime with no code
#: change and no redeploy, per R10.3). Model ids match the voice-pipeline's
#: Bedrock defaults; add or adjust models by editing the item, not this file.
DEFAULT_AI_PRICING: dict[str, Any] = {
    "models": {
        "anthropic.claude-3-haiku-20240307-v1:0": {
            "input_per_1k": 0.00025,
            "output_per_1k": 0.00125,
            "request": 0.0,
        },
        "anthropic.claude-3-5-sonnet-20240620-v1:0": {
            "input_per_1k": 0.003,
            "output_per_1k": 0.015,
            "request": 0.0,
        },
    },
    "markup": 1.0,
    "currency": "USD",
}


def user_pk(sub: str) -> str:
    """Return the ``hellodj-core`` partition key for a user's items.

    Entitlements are keyed by the stable Cognito subject (``sub``), not the
    username, so a single identity spans the web-ui and the bot.
    """
    return f"USER#{sub}"


def audit_sk(ts: str, rand: str) -> str:
    """Return the sort key for an audit entry.

    Audit entries sort chronologically by their ISO-8601 timestamp prefix; the
    trailing ``rand`` disambiguates two changes written in the same instant so
    neither is overwritten. Newest-first history reverses the ascending
    key order (see :meth:`EntitlementService.history`).

    Args:
        ts: An ISO-8601 timestamp (lexicographically sortable).
        rand: A short random suffix to disambiguate same-instant writes.
    """
    return f"{AUDIT_SK_PREFIX}{ts}#{rand}"


def _now_iso() -> str:
    """Return the current UTC time as a sortable ISO-8601 string.

    Used as an audit entry's timestamp and as the ``AUDIT#<ts>#<rand>`` sort-key
    prefix so audit rows sort chronologically (reversed to newest-first in
    :meth:`EntitlementService.history`), and as the tally's ``updated_at``.
    """
    return datetime.now(UTC).isoformat()


def seed_ai_pricing(
    core_table: CoreTable,
    *,
    pricing: dict[str, Any] | None = None,
) -> bool:
    """Idempotently seed the ``CONFIG#AIPRICING`` item if it is absent (R10.2/R10.3).

    Writes the shared AI pricing configuration item — a ``models`` map of
    per-model Bedrock unit prices plus a ``markup`` (default 1.0) — **only when
    the item does not already exist**, so re-running this is safe and never
    clobbers ops' edits. Because prices live in this data item, updating a price
    is a *data edit* (edit the ``CONFIG#AIPRICING`` item's ``data`` in place) that
    takes effect at runtime with no code change and no redeploy (R10.3); this
    seeder never overwrites an existing item.

    Intended to be called once at startup/bootstrap (or from an ops one-shot) to
    guarantee the pricing item exists so :meth:`EntitlementService.get_pricing`
    and the bot's cost meter have a table to read. Runtime price changes are made
    by editing the item, not by re-seeding.

    Args:
        core_table: The :class:`CoreTable` repository bound to ``hellodj-core``.
        pricing: Optional pricing payload to seed instead of
            :data:`DEFAULT_AI_PRICING` (used by tests / ops overrides).

    Returns:
        ``True`` if the pricing item was created by this call; ``False`` if it
        already existed (nothing was written).
    """
    if core_table.get(AIPRICING_PK, AIPRICING_SK) is not None:
        return False
    payload = dict(pricing) if pricing is not None else dict(DEFAULT_AI_PRICING)
    core_table.put_new(
        AIPRICING_PK, AIPRICING_SK, AIPRICING_ENTITY_TYPE, payload
    )
    return True


class EntitlementService:
    """Read/write per-user entitlements on the ``hellodj-core`` table.

    Args:
        core_table: An initialized :class:`CoreTable` repository bound to the
            ``hellodj-core`` DynamoDB (optionally DAX-fronted) resource table.
    """

    def __init__(self, core_table: CoreTable) -> None:
        self._core = core_table

    # -- reads --------------------------------------------------------------

    def get_raw(self, sub: str) -> dict[str, Any] | None:
        """Return the explicit entitlement ``data`` map, or ``None`` if unset.

        A ``None`` return lets the admin UI indicate the displayed values are
        defaults (not explicitly set) per R2.2.
        """
        item = self._core.get(user_pk(sub), ENTITLEMENT_SK)
        if item is None:
            return None
        return dict(item.get("data", {}))

    def get_effective(self, sub: str) -> dict[str, Any]:
        """Return the effective entitlements for a user (R2.1, R2.2).

        The explicit stored record (if any) merged over the secure defaults via
        :func:`entitlements_core.merge_effective`. A user with no stored record
        resolves to :data:`entitlements_core.DEFAULT_ENTITLEMENTS`.
        """
        return entitlements_core.merge_effective(self.get_raw(sub))

    def get_tally(self, sub: str) -> dict[str, Any]:
        """Return a user's accumulated AI cost tally (R10.4).

        Returns an empty mapping when the user has no tally item yet; callers
        treat an absent tally as a zero accumulated cost.
        """
        item = self._core.get(user_pk(sub), AITALLY_SK)
        if item is None:
            return {}
        return dict(item.get("data", {}))

    def get_pricing(self) -> dict[str, Any]:
        """Return the shared AI pricing table + markup (R10.3).

        Bedrock per-model unit prices and the configured markup live in a single
        data item so a price change is a data edit, never a code change. Returns
        an empty mapping when the pricing item has not been seeded yet.
        """
        item = self._core.get(AIPRICING_PK, AIPRICING_SK)
        if item is None:
            return {}
        return dict(item.get("data", {}))

    def seed_pricing(self, pricing: dict[str, Any] | None = None) -> bool:
        """Seed the AI pricing item if absent, returning ``True`` if created.

        Thin instance-level wrapper over :func:`seed_ai_pricing` for callers that
        already hold a service. Idempotent: it never overwrites an existing
        pricing item, so ops' in-place price edits are preserved (R10.3).
        """
        return seed_ai_pricing(self._core, pricing=pricing)

    def history(self, sub: str) -> list[dict[str, Any]]:
        """Return a user's entitlement-change audit entries, newest-first (R15.3).

        Audit items share the ``AUDIT#`` sort-key prefix under the user's
        partition and sort ascending by their ISO-8601 timestamp; reversing the
        prefix query yields reverse-chronological order.
        """
        rows = self._core.query_pk_prefix(
            user_pk(sub), sk_prefix=AUDIT_SK_PREFIX
        )
        rows.sort(key=lambda r: r.get("SK", ""), reverse=True)
        return [dict(r.get("data", {})) for r in rows]

    # -- audited writes -----------------------------------------------------

    def set_fields(
        self, sub: str, changes: dict[str, Any], *, admin_sub: str
    ) -> dict[str, Any]:
        """Validate and apply an entitlement change, audited (R2.3, R15).

        Validates any quota fields (``max_bots_per_guild`` / ``max_guilds``)
        against :func:`entitlements_core.validate_quota` (R12.2), then applies
        the change using **write-before-apply** ordering: one audit entry per
        changed field is written first, and only then is the entitlement item
        updated. If the audit write fails, the entitlement record is left
        unchanged (R15.2, Property 8). If the audit succeeded but the
        entitlement update then fails, each audit entry is marked
        ``apply_status="orphaned"`` and the failure is re-raised.

        Only fields whose new value differs from the current effective value are
        recorded and applied, so a no-op save writes nothing.

        Args:
            sub: The governed user's Cognito subject.
            changes: The field -> new value map to apply.
            admin_sub: The acting administrator's Cognito subject (audited).

        Returns:
            The full entitlement ``data`` payload after the write.

        Raises:
            ValueError: If a quota field is present and less than 1 (R12.2).
            Exception: Any datastore failure from the audit or entitlement
                write. On an entitlement-write failure the change is not applied.
        """
        for field in _QUOTA_FIELDS:
            if field in changes:
                changes[field] = entitlements_core.validate_quota(
                    int(changes[field])
                )

        effective = self.get_effective(sub)
        applied: dict[str, Any] = {
            field: value
            for field, value in changes.items()
            if effective.get(field) != value
        }
        if not applied:
            return self.get_raw(sub) or {}

        audit_keys = self._write_audit_entries(
            sub, applied, effective, admin_sub=admin_sub
        )

        try:
            return self._apply_entitlement_changes(sub, applied)
        except Exception:
            # The entitlement update failed after the audit was written; the
            # record is unchanged (Property 8). Mark the audit rows orphaned so
            # the history reflects that the change was never applied.
            self._mark_audit_orphaned(sub, audit_keys)
            raise

    def reset_tally(self, sub: str, *, admin_sub: str) -> None:
        """Zero a user's AI cost tally, audited (R10.6).

        Uses the same write-before-apply ordering as :meth:`set_fields`: the
        audit entry is written first, then the tally is set back to zero. If the
        audit write fails the tally is unchanged; if the tally reset fails the
        audit row is marked orphaned and the failure is re-raised.
        """
        # An already-zero tally is still audited: R10.6 records the explicit
        # reset action regardless of the prior value.
        old_cost = float(self.get_tally(sub).get("accumulated_cost", 0.0))

        audit_keys = self._write_audit_entries(
            sub,
            {"ai_tally": 0.0},
            {"ai_tally": old_cost},
            admin_sub=admin_sub,
        )

        def _zero(data: dict[str, Any]) -> dict[str, Any]:
            data["accumulated_cost"] = 0.0
            data["updated_at"] = _now_iso()
            return data

        try:
            self._core.update_with_lock(
                user_pk(sub),
                AITALLY_SK,
                _zero,
                entity_type=AITALLY_ENTITY_TYPE,
            )
        except Exception:
            self._mark_audit_orphaned(sub, audit_keys)
            raise

    def add_cost(self, sub: str, effective_cost: float) -> dict[str, Any]:
        """Increment a user's AI cost tally by ``effective_cost`` (R10.1).

        This is runtime metering (invoked by the bot when it permits an AI
        request), not an administrator change, so it is *not* audited. The
        increment is an optimistic-lock read-modify-write so concurrent meters
        accumulate rather than clobber one another.

        Args:
            sub: The governed user's Cognito subject.
            effective_cost: The marked-up cost to add to the tally.

        Returns:
            The tally ``data`` payload after the increment.
        """

        def _increment(data: dict[str, Any]) -> dict[str, Any]:
            current = float(data.get("accumulated_cost", 0.0))
            data["accumulated_cost"] = current + effective_cost
            data["updated_at"] = _now_iso()
            data.setdefault("currency", "USD")
            return data

        updated = self._core.update_with_lock(
            user_pk(sub),
            AITALLY_SK,
            _increment,
            entity_type=AITALLY_ENTITY_TYPE,
        )
        return dict(updated.get("data", {}))

    # -- internal write helpers --------------------------------------------

    def _write_audit_entries(
        self,
        sub: str,
        applied: dict[str, Any],
        effective: dict[str, Any],
        *,
        admin_sub: str,
    ) -> list[str]:
        """Write one audit entry per changed field, returning their sort keys.

        Each entry is created with ``put_new`` (a fresh, unique sort key), so a
        failure here raises before any entitlement change is applied
        (write-before-apply). The returned sort keys let the caller mark the
        rows orphaned if the subsequent entitlement write fails.
        """
        at = _now_iso()
        pk = user_pk(sub)
        keys: list[str] = []
        for field, new_value in applied.items():
            sk = audit_sk(at, secrets.token_hex(4))
            self._core.put_new(
                pk,
                sk,
                AUDIT_ENTITY_TYPE,
                {
                    "admin_sub": admin_sub,
                    "field": field,
                    "old": effective.get(field),
                    "new": new_value,
                    "at": at,
                    "apply_status": _AUDIT_STATUS_APPLIED,
                },
            )
            keys.append(sk)
        return keys

    def _apply_entitlement_changes(
        self, sub: str, applied: dict[str, Any]
    ) -> dict[str, Any]:
        """Apply ``applied`` to the entitlement item's ``data`` (create/merge).

        Merges the changed fields over the existing payload with the CoreTable
        optimistic-lock read-modify-write (``put_new`` on first write). Only the
        submitted fields are touched.
        """
        pk = user_pk(sub)
        existing = self._core.get(pk, ENTITLEMENT_SK)
        if existing is None:
            self._core.put_new(
                pk, ENTITLEMENT_SK, ENTITLEMENT_ENTITY_TYPE, dict(applied)
            )
            return dict(applied)

        def _merge(data: dict[str, Any]) -> dict[str, Any]:
            data.update(applied)
            return data

        updated = self._core.update_with_lock(
            pk, ENTITLEMENT_SK, _merge, entity_type=ENTITLEMENT_ENTITY_TYPE
        )
        return dict(updated.get("data", {}))

    def _mark_audit_orphaned(self, sub: str, audit_keys: list[str]) -> None:
        """Best-effort mark of audit rows as orphaned after an apply failure.

        The entitlement change was not applied, so the audit entries no longer
        correspond to a real change. Marking them ``orphaned`` keeps the history
        truthful. Marking is best-effort: a failure here must not mask the
        original apply error the caller is propagating.
        """
        def _orphan(data: dict[str, Any]) -> dict[str, Any]:
            data["apply_status"] = _AUDIT_STATUS_ORPHANED
            return data

        pk = user_pk(sub)
        for sk in audit_keys:
            try:
                self._core.update_with_lock(
                    pk, sk, _orphan, entity_type=AUDIT_ENTITY_TYPE
                )
            except Exception:  # noqa: BLE001 - best-effort; preserve apply error
                continue
