"""Registration-mode normalization for the web-ui (pure, side-effect-free).

Mirrors the entitlements_core / register_policy split: no boto3, no Flask, no
I/O — just the secure-by-default decision that maps a raw stored config value to
the two-valued Registration_Mode. Imported unchanged by the auth enforcement
route and the login banner so display and enforcement never drift.

Secure default (R1): an absent OR invalid stored value resolves to CLOSED, so
the platform stays invite-only unless an admin deliberately opens it.

Requirements: 1.1, 1.2, 1.3, 3.1, 3.2
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from config_store import GLOBAL_CONFIG_PK

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime import cycle
    from hellodj_platform_logic.data_access import CoreTable

    from config_store import ConfigStore

__all__ = [
    "OPEN",
    "CLOSED",
    "VALID_MODES",
    "CONFIG_KEY",
    "BANNER_OPEN",
    "BANNER_CLOSED",
    "AUDIT_SK_PREFIX",
    "AUDIT_ENTITY_TYPE",
    "normalize_mode",
    "current_mode",
    "banner_text",
    "is_open",
    "apply_mode_change",
]

OPEN: Final = "OPEN"
CLOSED: Final = "CLOSED"
VALID_MODES: Final = (OPEN, CLOSED)

#: Field name for the mode inside the global config payload (ConfigStore).
CONFIG_KEY: Final = "registration_mode"

#: Fixed login-page banner copy (R3.1 / R3.2).
BANNER_OPEN: Final = "Registration is open — create an account"
BANNER_CLOSED: Final = "Registration is currently closed — invite only"

#: Sort-key prefix for a Mode_Change_Audit_Record under ``CONFIG#GLOBAL``.
#: Audit rows co-locate with the config item they describe and sort
#: chronologically by their ISO-8601 timestamp (R5.1).
AUDIT_SK_PREFIX: Final = "REGMODEAUDIT#"

#: ``entityType`` discriminator for a registration-mode audit item.
AUDIT_ENTITY_TYPE: Final = "RegistrationModeAudit"


def normalize_mode(raw: Any) -> str:
    """Return CLOSED unless ``raw`` is exactly a valid mode (R1.1, R1.3).

    Secure by default: ``None``, missing, non-string, or any string that is not
    ``OPEN``/``CLOSED`` (after upper-casing a trimmed string) resolves to
    ``CLOSED``. A valid stored value passes through unchanged (R1.2).
    """
    if isinstance(raw, str):
        candidate = raw.strip().upper()
        if candidate in VALID_MODES:
            return candidate
    return CLOSED


def current_mode(config: dict[str, Any] | None) -> str:
    """Return the effective mode from a global-config payload (R1.1-R1.3).

    Reads :data:`CONFIG_KEY` out of ``config`` (an empty/``None`` payload has no
    key) and normalizes it. Any absent or invalid value yields ``CLOSED``.
    """
    value = (config or {}).get(CONFIG_KEY)
    return normalize_mode(value)


def is_open(config: dict[str, Any] | None) -> bool:
    """Return whether self-registration is currently permitted."""
    return current_mode(config) == OPEN


def banner_text(mode: str) -> str:
    """Return the fixed login banner copy for ``mode`` (R3.1, R3.2)."""
    return BANNER_OPEN if mode == OPEN else BANNER_CLOSED


def _now_iso() -> str:
    """Return the current UTC time as a sortable ISO-8601 string.

    Used as the audit record's ``at`` timestamp and as the
    ``REGMODEAUDIT#<ts>#<rand>`` sort-key prefix so audit rows sort
    chronologically. Injectable via ``apply_mode_change``'s ``now`` parameter so
    tests can pin the timestamp without patching the clock.
    """
    return datetime.now(UTC).isoformat()


def apply_mode_change(
    config_store: ConfigStore,
    core_table: CoreTable,
    *,
    requested: str,
    admin_sub: str,
    now: Any = None,
) -> str:
    """Audit-then-persist a registration-mode change; no-op when unchanged.

    Computes the current mode from the stored global config and normalizes the
    ``requested`` value. When the normalized request equals the current mode this
    is a no-op: ``current`` is returned and **nothing is written** — no audit
    row, no ``set_global`` (R5.2, Property 8).

    Otherwise the change uses **write-before-apply** ordering (mirroring
    :meth:`EntitlementService.set_fields`): a single Mode_Change_Audit_Record is
    written first via ``core_table.put_new`` (acting admin, old value, new value,
    timestamp), and only then is the new mode persisted via
    ``config_store.set_global`` (R4.2, R5.1). If the audit ``put_new`` fails,
    ``set_global`` is never reached — the mode is left unchanged and no partial
    state results.

    Args:
        config_store: The :class:`ConfigStore` holding the global config item.
        core_table: The :class:`CoreTable` the audit row is written to (the same
            table the config lives on).
        requested: The raw requested mode; normalized before comparison so a
            tampered value can only ever resolve to ``CLOSED``.
        admin_sub: The acting administrator's Cognito subject (audited).
        now: Optional injected timestamp source; a callable returning an
            ISO-8601 string, a literal ISO-8601 string, or ``None`` to use
            :func:`_now_iso`.

    Returns:
        The effective mode after the call (the new mode on a change, or the
        unchanged current mode on a no-op).
    """
    current = current_mode(config_store.get_global())
    new = normalize_mode(requested)
    if new == current:
        return current  # R5.2 no-op: no audit row, no persist.

    if callable(now):
        at = now()
    elif isinstance(now, str):
        at = now
    else:
        at = _now_iso()

    # Write-before-apply: the audit row is created first so a failure here
    # aborts before the mode is persisted (R5.1, Error Handling).
    core_table.put_new(
        GLOBAL_CONFIG_PK,
        f"{AUDIT_SK_PREFIX}{at}#{secrets.token_hex(4)}",
        AUDIT_ENTITY_TYPE,
        {"admin_sub": admin_sub, "old": current, "new": new, "at": at},
    )
    config_store.set_global({CONFIG_KEY: new})  # R4.2 persist.
    return new
