"""Account-level delegated administration (co-admins of a user's account).

Distinct from *platform* admins (the Cognito ``admins`` group, who run the whole
service) and from *guild* admins (:mod:`guild_admin_service`, who manage a single
Discord guild): an **account admin** is a Discord identity a user appoints to
co-manage *their own account*. When an appointed Discord id signs in via Discord
OAuth, it logs straight into the appointing owner's account (shared access,
Option B) — the session identity becomes the owner's Cognito subject.

Data model (hellodj-core single table):

* Account-admin edge:
  ``PK=USER#<owner_sub>``  ``SK=ACCTADMIN#<discordId>``  entityType=AccountAdmin
  ``GSI1PK=DISCORD#<discordId>``  ``GSI1SK=ACCTADMIN#<owner_sub>``
  data={appointed_at}

The reverse GSI1 lets a Discord login resolve, in a single indexed query, which
account(s) that Discord id administers — the lookup the login path uses to
establish the owner's session.

Requirements: account-delegated admin (owner appoints co-admins by Discord id;
appointed ids may Discord-OAuth into the owner's account and land on the
dashboard).
"""

from __future__ import annotations

from typing import Any

from hellodj_platform_logic.data_access import CoreTable

__all__ = [
    "AccountAdminService",
    "user_pk",
    "acct_admin_sk",
]

ACCOUNT_ADMIN_ENTITY = "AccountAdmin"


def user_pk(owner_sub: str) -> str:
    """Return the partition key for a user's account items."""
    return f"USER#{owner_sub}"


def acct_admin_sk(discord_id: str) -> str:
    """Return the sort key for an account-admin edge keyed by Discord id."""
    return f"ACCTADMIN#{discord_id}"


def _discord_gsi1pk(discord_id: str) -> str:
    return f"DISCORD#{discord_id}"


def _acctadmin_gsi1sk(owner_sub: str) -> str:
    return f"ACCTADMIN#{owner_sub}"


class AccountAdminService:
    """Appoint / remove / resolve account co-admins over ``hellodj-core``."""

    def __init__(self, core_table: CoreTable) -> None:
        self._core = core_table

    def list_admins(self, owner_sub: str) -> list[dict[str, Any]]:
        """Return the Discord-id account-admin edges the owner has appointed."""
        rows = self._core.query_pk_prefix(
            user_pk(owner_sub), sk_prefix="ACCTADMIN#"
        )
        return [
            {
                "discord_id": r["SK"].split("ACCTADMIN#", 1)[1],
                "appointed_at": r.get("data", {}).get("appointed_at", 0),
            }
            for r in rows
        ]

    def appoint_admin(self, owner_sub: str, discord_id: str) -> None:
        """Appoint a Discord id as a co-admin of the owner's account.

        Idempotent: re-appointing an existing edge is a no-op (never raises or
        duplicates). Sets the GSI1 reverse index so a later Discord OAuth login
        resolves this owner's account.
        """
        existing = self._core.get(user_pk(owner_sub), acct_admin_sk(discord_id))
        if existing is not None:
            return
        self._core.put_new(
            user_pk(owner_sub),
            acct_admin_sk(discord_id),
            ACCOUNT_ADMIN_ENTITY,
            {},
            gsi1pk=_discord_gsi1pk(discord_id),
            gsi1sk=_acctadmin_gsi1sk(owner_sub),
        )

    def remove_admin(self, owner_sub: str, discord_id: str) -> None:
        """Remove a Discord-id account-admin edge from the owner's account."""
        self._core.delete(user_pk(owner_sub), acct_admin_sk(discord_id))

    def owner_for_discord(self, discord_id: str) -> str | None:
        """Return the owner subject a Discord id co-administers, or ``None``.

        Resolves via the GSI1 reverse index. If a Discord id has been appointed
        on more than one account (edge case), the lexically-first owner subject
        is chosen deterministically so the login target is stable.
        """
        rows = self._core.query_gsi1(
            _discord_gsi1pk(discord_id), sk_prefix="ACCTADMIN#"
        )
        owners = [
            r["GSI1SK"].split("ACCTADMIN#", 1)[1]
            for r in rows
            if str(r.get("GSI1SK", "")).startswith("ACCTADMIN#")
        ]
        if not owners:
            return None
        return sorted(owners)[0]
