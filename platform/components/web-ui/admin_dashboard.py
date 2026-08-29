"""At-a-glance service KPIs for the administrator dashboard.

An administrator is not a self-serve user: they run the *platform*, so their
landing page is a KPI dashboard summarizing the service as a whole rather than
the per-user Config/Guilds/Account controls a regular member sees. This module
computes those core metrics from the data the admin panel already has access
to — the Cognito-backed user directory, the invite service, and the
``hellodj-core`` single table — and returns a stable list of ``{label, value}``
cards the dashboard template renders.

Every metric degrades independently: a missing service or a transient backend
error resolves that card to ``0`` (and, for a hard failure, the dashboard still
renders) rather than 500-ing the whole page. The counts are derived from real
queryable state — no placeholder constants — so the numbers reflect the live
platform.

Requirements: 6.5 (web admin UI), 8.2 (admin manages the platform).
"""

from __future__ import annotations

from typing import Any, Protocol

__all__ = ["admin_dashboard_stats", "AdminDirectoryLike", "InviteServiceLike"]

#: ``entityType`` of a guild-ownership item in ``hellodj-core`` (one per guild).
GUILD_OWNER_ENTITY = "GuildOwner"

#: ``entityType`` of a per-user source-credential item (one per connected
#: provider) in ``hellodj-core``.
SOURCE_CREDENTIAL_ENTITY = "SourceCredential"

#: The invite status that counts as "still pending" (mirrors InviteService).
PENDING_INVITE_STATUS = "invited"


class AdminDirectoryLike(Protocol):
    """The subset of :class:`AdminDirectory` this module reads."""

    def list_users(self) -> list[dict[str, Any]]: ...


class InviteServiceLike(Protocol):
    """The subset of :class:`InviteService` this module reads."""

    def list_invites(self) -> list[dict[str, Any]]: ...


class CoreTableLike(Protocol):
    """The subset of ``CoreTable`` this module reads (entity enumeration)."""

    def scan_entity(self, entity_type: str) -> Any: ...


def admin_dashboard_stats(
    directory: AdminDirectoryLike | None,
    invite_service: InviteServiceLike | None,
    core_table: CoreTableLike | None,
) -> list[dict[str, Any]]:
    """Return the administrator dashboard KPI cards from live data.

    Args:
        directory: The Cognito user directory (``None`` in degraded mode).
        invite_service: The invite service (``None`` in degraded mode).
        core_table: The ``hellodj-core`` repository (``None`` in degraded mode).

    Returns:
        A stable, ordered list of ``{"label", "value"}`` KPI cards. Each metric
        degrades to ``0`` independently when its backing service is absent or a
        lookup fails, so the dashboard always renders with a full card set.
    """
    users = _safe_list(directory.list_users if directory else None)
    invites = _safe_list(invite_service.list_invites if invite_service else None)

    total_users = len(users)
    admins = sum(1 for u in users if u.get("is_admin"))
    disabled = sum(1 for u in users if not u.get("enabled", True))
    pending_invites = sum(
        1 for i in invites if i.get("status") == PENDING_INVITE_STATUS
    )
    guilds = _count_entity(core_table, GUILD_OWNER_ENTITY)
    connected_sources = _count_entity(core_table, SOURCE_CREDENTIAL_ENTITY)

    return [
        {"label": "Total Users", "value": total_users},
        {"label": "Administrators", "value": admins},
        {"label": "Disabled Accounts", "value": disabled},
        {"label": "Pending Invites", "value": pending_invites},
        {"label": "Guilds", "value": guilds},
        {"label": "Connected Sources", "value": connected_sources},
    ]


def _safe_list(fn: Any) -> list[dict[str, Any]]:
    """Call a zero-arg list accessor, degrading to ``[]`` on absence/failure."""
    if fn is None:
        return []
    try:
        return list(fn())
    except Exception:  # noqa: BLE001 - a dashboard metric never breaks the page
        return []


def _count_entity(core_table: CoreTableLike | None, entity_type: str) -> int:
    """Count ``hellodj-core`` items of ``entity_type``, degrading to ``0``.

    Uses the key-projected :meth:`CoreTable.scan_entity` iterator so counting
    never pulls entity payloads (and never decrypts credential blobs); a missing
    table or any backend error resolves to ``0`` so one metric's failure never
    fails the dashboard.
    """
    if core_table is None:
        return 0
    try:
        return sum(1 for _ in core_table.scan_entity(entity_type))
    except Exception:  # noqa: BLE001 - a dashboard metric never breaks the page
        return 0
