"""One-time migration for stale pending invites (task 16).

The invite flow was amended (see the multi-tenant-guild-sources spec): the old
Cognito temp-password invitation was replaced by a single-use tokenized link to
a HelloDJ-hosted registration page + a branded SES email. Old-flow invites were
recorded without a ``token_hash`` — their links can no longer be resolved by the
new ``/invite/<token>`` route, so any that are still pending are dead weight.

This module migrates those stale, old-flow pending invites in one of two ways,
selectable per run:

* ``expire`` (default, the safe non-emailing action): flip a pending old-flow
  invite's status to ``expired`` so the admin panel stops surfacing it as
  actionable and its (unusable) link is explicitly closed out.
* ``resend``: mint a fresh single-use token and send the branded invitation
  email under the new flow via :meth:`InviteService.resend`, giving the invitee
  a working link.

New-flow invites (those *with* a ``token_hash``) and non-pending invites
(``accepted`` / ``revoked``) are left untouched. Expiry is judged the same way
the admin listing does — a stored ``invited`` invite past ``expires_at`` is
already effectively expired and is treated as pending-old-flow only when it has
no ``token_hash``.

The core :func:`migrate_stale_invites` is pure with respect to AWS: it takes a
:class:`CoreTable` and an :class:`InviteService` (or any object exposing
``list_invites`` + ``resend``), so it is unit-testable with in-memory fakes.

Requirements: 1.2, 1.4
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from hellodj_platform_logic.data_access import CoreTable

from invite_service import INVITE_SK, invite_pk

__all__ = [
    "MigrationSummary",
    "migrate_stale_invites",
    "MIGRATION_MARKER",
]

#: ``invited_by`` marker recorded for invites re-sent by this migration when the
#: original record carries no ``invited_by`` of its own.
MIGRATION_MARKER = "invite-migration"

#: Non-terminal statuses. A pending invite is one whose stored status is
#: ``invited`` (an ``invited`` invite past ``expires_at`` is still "pending" as
#: far as this migration is concerned — it just never got accepted/revoked).
_PENDING_STATUS = "invited"


class _InviteMigrator(Protocol):
    """The slice of :class:`InviteService` this migration drives."""

    def list_invites(self) -> list[dict[str, Any]]: ...

    def resend(self, email: str, *, invited_by: str) -> dict[str, Any]: ...


@dataclass
class MigrationSummary:
    """Counts describing what a migration run did, for logging.

    * ``scanned``  — every invite enumerated.
    * ``resent``   — old-flow pending invites re-sent under the new flow.
    * ``expired``  — old-flow pending invites flipped to ``expired``.
    * ``skipped``  — invites left untouched (new-flow, or not pending).
    * ``errors``   — per-email error messages for invites that failed to migrate.
    """

    scanned: int = 0
    resent: int = 0
    expired: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def migrated(self) -> int:
        """Total invites acted on (re-sent + expired)."""
        return self.resent + self.expired

    def as_dict(self) -> dict[str, Any]:
        """Return a plain dict of the counts for structured logging."""
        return {
            "scanned": self.scanned,
            "resent": self.resent,
            "expired": self.expired,
            "skipped": self.skipped,
            "migrated": self.migrated,
            "errors": list(self.errors),
        }


def _is_old_flow_pending(raw_record: dict[str, Any] | None) -> bool:
    """Return whether a raw invite record is an old-flow *pending* invite.

    Old-flow invites lack a ``token_hash`` (their links predate the tokenized
    ``/invite/<token>`` route). Only ``invited`` records are candidates — an
    ``accepted`` or ``revoked`` invite is terminal and never touched.
    """
    if raw_record is None:
        return False
    data = raw_record.get("data", {})
    if data.get("status") != _PENDING_STATUS:
        return False
    return not data.get("token_hash")


def _expire_record(core: CoreTable, email: str) -> None:
    """Flip a pending old-flow invite's stored status to ``expired``."""
    core.update_with_lock(
        invite_pk(email),
        INVITE_SK,
        lambda data: {**data, "status": "expired", "expired_at": int(time.time())},
    )


def migrate_stale_invites(
    core: CoreTable,
    invites: _InviteMigrator,
    *,
    mode: str = "expire",
) -> MigrationSummary:
    """Migrate stale old-flow pending invites; return a summary of counts.

    Enumerates invites via ``invites.list_invites()`` (the admin listing access
    pattern) and reads each invite's raw record back from ``core`` to inspect
    its ``token_hash`` — ``list_invites`` intentionally does not surface secret
    material, so the raw record is the source of truth for old-vs-new flow.

    For each *old-flow, still-pending* invite (no ``token_hash``, status
    ``invited``):

    * ``mode="expire"`` (default): flip its status to ``expired`` (no email).
    * ``mode="resend"``: mint a fresh token + send the branded email via
      ``invites.resend(email, invited_by=...)``. ``invited_by`` defaults to the
      record's own value, falling back to :data:`MIGRATION_MARKER`.

    New-flow invites (with a ``token_hash``) and non-pending invites are left
    untouched and counted as ``skipped``. Per-email failures are collected in
    the summary's ``errors`` and do not abort the run.

    Raises:
        ValueError: If ``mode`` is not ``"expire"`` or ``"resend"``.
    """
    if mode not in ("expire", "resend"):
        raise ValueError(f"mode must be 'expire' or 'resend', got {mode!r}")

    summary = MigrationSummary()
    for row in invites.list_invites():
        summary.scanned += 1
        email = row.get("email")
        if not email:
            summary.skipped += 1
            continue
        raw_record = core.get(invite_pk(email), INVITE_SK)
        if not _is_old_flow_pending(raw_record):
            summary.skipped += 1
            continue
        try:
            if mode == "resend":
                data = raw_record.get("data", {}) if raw_record else {}
                invited_by = data.get("invited_by") or MIGRATION_MARKER
                invites.resend(email, invited_by=invited_by)
                summary.resent += 1
            else:
                _expire_record(core, email)
                summary.expired += 1
        except Exception as error:  # noqa: BLE001 - collect + continue
            summary.errors.append(f"{email}: {error}")
    return summary


def _run_cli() -> int:
    """CLI entry: build services via bootstrap and run the migration."""
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Migrate stale old-flow pending invites (no token_hash)."
    )
    parser.add_argument(
        "--mode",
        choices=("expire", "resend"),
        default="expire",
        help=(
            "expire (default, no email): mark old-flow pending invites expired. "
            "resend: mint a fresh token + send the branded email."
        ),
    )
    args = parser.parse_args()

    import bootstrap

    services = bootstrap.build_services()
    core = getattr(services.get("invite_service"), "_core", None)
    invite_service = services.get("invite_service")
    if invite_service is None or core is None:
        print(
            "invite service unavailable (missing HELLODJ_CORE_TABLE / "
            "HELLODJ_COGNITO_USER_POOL_ID); nothing to migrate.",
        )
        return 1

    summary = migrate_stale_invites(core, invite_service, mode=args.mode)
    print(json.dumps(summary.as_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI wiring
    raise SystemExit(_run_cli())
