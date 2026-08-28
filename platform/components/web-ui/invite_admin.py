"""Admin-side invite listing/revocation helpers over ``hellodj-core``.

Split out of :mod:`invite_service` to keep :class:`InviteService` cohesive and
under the per-file line ceiling (R13.3). These are the *management* access
patterns the admin panel needs — enumerate every invite and revoke a pending
one — expressed as free functions over a :class:`CoreTable` plus the shared
key helpers/constants from :mod:`invite_service`.

The invite record lives under a per-email partition (``INVITE#<email>``) and
its GSI1 slot is taken by the single-use-token lookup, so a dedicated index
partition (``INVITE_INDEX_PK``) holds one lightweight pointer per invite as the
listing access pattern. Pointers carry no secret material (R7.4).

Requirements: 1.2, 1.4
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from hellodj_platform_logic.data_access import CoreTable
from hellodj_platform_logic.data_access.errors import (
    ConditionalCheckFailedError,
    OptimisticLockError,
)

__all__ = [
    "put_index_pointer",
    "list_invites",
    "revoke_invite",
    "effective_status",
    "RevokeError",
]

INVITE_INDEX_ENTITY = "InviteIndex"


class RevokeError(Exception):
    """Raised when a pending invite cannot be revoked (unknown/not pending)."""


class _NotPendingError(Exception):
    """Internal signal that the invite is no longer ``invited`` mid-revoke."""


def put_index_pointer(core: CoreTable, index_pk: str, index_sk: str, email: str) -> None:
    """Record/refresh an invite's pointer in the shared index partition.

    Idempotent: an existing pointer (from a prior invite of the same email) is
    left as-is — the invite record itself is the source of truth read back
    during :func:`list_invites`.
    """
    if core.get(index_pk, index_sk) is not None:
        return
    try:
        core.put_new(index_pk, index_sk, INVITE_INDEX_ENTITY, {"email": email})
    except ConditionalCheckFailedError:
        # A concurrent invite wrote the same pointer first; harmless.
        return


def list_invites(
    core: CoreTable,
    *,
    index_pk: str,
    invite_pk: Any,
    invite_sk: str,
) -> list[dict[str, Any]]:
    """Return every recorded invite with an effective display status.

    Enumerates the shared index partition then reads each invite record back as
    the source of truth. Each row is a plain dict (``email``, ``invited_by``,
    ``created_at``, ``expires_at``, ``status``) where ``status`` is the
    *effective* status: a stored ``invited`` invite past ``expires_at`` is
    surfaced as ``expired`` (R1.4) without mutating the record. Rows are sorted
    newest first by ``created_at``.
    """
    pointers = core.query_pk_prefix(index_pk, sk_prefix="EMAIL#")
    invites: list[dict[str, Any]] = []
    for pointer in pointers:
        email = pointer.get("data", {}).get("email")
        if not email:
            continue
        item = core.get(invite_pk(email), invite_sk)
        if item is None:
            continue
        data = item.get("data", {})
        invites.append(
            {
                "email": data.get("email", email),
                "invited_by": data.get("invited_by", ""),
                "created_at": data.get("created_at", 0),
                "expires_at": data.get("expires_at", 0),
                "status": effective_status(data),
            }
        )
    invites.sort(key=lambda row: row.get("created_at", 0), reverse=True)
    return invites


def revoke_invite(
    core: CoreTable,
    *,
    invite_pk: Any,
    invite_sk: str,
    email: str,
) -> dict[str, Any]:
    """Flip a still-``invited`` invite to ``revoked`` so its token dies.

    ``resolve_by_token`` / ``consume`` reject any non-``invited`` status, so the
    revoked token can no longer be used. A non-pending or unknown invite raises
    :class:`RevokeError` (R1.4).
    """
    email_norm = email.strip().lower()
    existing = core.get(invite_pk(email_norm), invite_sk)
    if existing is None or existing.get("data", {}).get("status") != "invited":
        raise RevokeError(f"no pending invite for {email_norm} to revoke")

    def _revoke(data: dict[str, Any]) -> dict[str, Any]:
        if data.get("status") != "invited":
            raise _NotPendingError
        return {**data, "status": "revoked", "revoked_at": int(time.time())}

    try:
        core.update_with_lock(invite_pk(email_norm), invite_sk, _revoke)
    except (_NotPendingError, OptimisticLockError) as error:
        raise RevokeError(
            f"no pending invite for {email_norm} to revoke"
        ) from error
    return {"email": email_norm, "status": "revoked"}


def effective_status(data: Mapping[str, Any]) -> str:
    """Return the display status, surfacing a lapsed ``invited`` as expired."""
    status = data.get("status", "invited")
    if status == "invited":
        expires_at = data.get("expires_at")
        if not (isinstance(expires_at, int) and expires_at > int(time.time())):
            return "expired"
    return status
