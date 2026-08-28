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
from numbers import Number
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
    "delete_invite",
    "effective_status",
    "is_unexpired",
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


def delete_invite(
    core: CoreTable,
    *,
    index_pk: str,
    invite_pk: Any,
    invite_sk: str,
    invite_index_sk: Any,
    email: str,
) -> dict[str, Any]:
    """Permanently delete an invite record and its listing pointer.

    Unlike :func:`revoke_invite` (which keeps the record, flipping it to
    ``revoked`` so its token dies but the row lingers in the admin list), this
    removes the invite outright: both the per-email invite item and its pointer
    in the shared index partition are deleted, so the row disappears from
    :func:`list_invites`. Any token the invite held stops resolving because the
    record — and its GSI1 token slot — no longer exist.

    Idempotent: deleting an unknown/already-deleted invite is a no-op (the two
    ``delete`` calls tolerate missing items) and still returns a ``deleted``
    result so the caller renders a clean list.
    """
    email_norm = email.strip().lower()
    core.delete(invite_pk(email_norm), invite_sk)
    core.delete(index_pk, invite_index_sk(email_norm))
    return {"email": email_norm, "status": "deleted"}


def is_unexpired(expires_at: Any) -> bool:
    """Return whether an ``expires_at`` epoch-second value is in the future.

    ``expires_at`` is a Number (``N``) attribute, which live DynamoDB returns as
    :class:`decimal.Decimal` — a ``numbers.Number`` but NOT a ``numbers.Real``
    or ``int``. Accept any ``numbers.Number`` (``int`` or ``Decimal``, excluding
    ``bool``) so a freshly minted invite isn't treated as expired just because
    of its deserialized type.
    """
    if isinstance(expires_at, bool) or not isinstance(expires_at, Number):
        return False
    return expires_at > int(time.time())


def effective_status(data: Mapping[str, Any]) -> str:
    """Return the display status, surfacing a lapsed ``invited`` as expired."""
    status = data.get("status", "invited")
    if status == "invited" and not is_unexpired(data.get("expires_at")):
        return "expired"
    return status


def blocks_new_invite(item: Mapping[str, Any]) -> bool:
    """Return whether an existing invite record should block a fresh invite.

    Only a *live new-flow* invite blocks: status ``invited``, carrying a
    ``token_hash`` (minted by the tokenized flow), and not past ``expires_at``.
    Terminal records (``accepted``/``revoked``/``expired``), already-expired
    invites, and legacy *old-flow* records (no ``token_hash`` — their link
    predates the tokenized route) do NOT block: they are stale and get replaced.
    This prevents a deleted account or an old-flow leftover from permanently
    blocking re-invites (R1.5).
    """
    data = item.get("data", {})
    if data.get("status") != "invited":
        return False
    if not data.get("token_hash"):
        return False
    return is_unexpired(data.get("expires_at"))
