"""User profile and Discord-account linking.

A user account is a Cognito identity (subject id ``sub``). After first login a
user may link their Discord account so subsequent logins can go through Discord
OAuth without a password (R3). The Discord id → user mapping is reverse-indexed
on GSI1 so a Discord login resolves the Cognito account in a single indexed
query, and a Discord identity links to at most one account (R3.3, R3.4).

Data model:
* ``PK=USER#<sub>``  ``SK=PROFILE``  data={email, discord_id?, discord_linked}
  When linked: ``GSI1PK=DISCORD#<discordId>``, ``GSI1SK=USER``.

Requirements: 3.1, 3.2, 3.3, 3.4
"""

from __future__ import annotations

from typing import Any

from hellodj_platform_logic.data_access import CoreTable

__all__ = ["UserProfileService", "user_pk", "PROFILE_SK"]

PROFILE_SK = "PROFILE"
USER_ENTITY = "UserProfile"


def user_pk(sub: str) -> str:
    """Return the partition key for a user's profile item."""
    return f"USER#{sub}"


def _discord_gsi1pk(discord_id: str) -> str:
    return f"DISCORD#{discord_id}"


class UserProfileService:
    """Read/update user profiles and the Discord-id reverse index."""

    def __init__(self, core_table: CoreTable) -> None:
        self._core = core_table

    def get(self, sub: str) -> dict[str, Any]:
        """Return a user's profile payload, or an empty mapping if absent."""
        item = self._core.get(user_pk(sub), PROFILE_SK)
        return dict(item.get("data", {})) if item else {}

    def ensure(self, sub: str, *, email: str) -> dict[str, Any]:
        """Create the profile on first sight; return the current payload."""
        item = self._core.get(user_pk(sub), PROFILE_SK)
        if item is not None:
            return dict(item.get("data", {}))
        data = {"email": email, "discord_linked": False}
        self._core.put_new(user_pk(sub), PROFILE_SK, USER_ENTITY, data)
        return data

    def user_for_discord(self, discord_id: str) -> str | None:
        """Return the Cognito subject linked to a Discord id, or ``None``."""
        rows = self._core.query_gsi1(
            _discord_gsi1pk(discord_id), sk_prefix="USER"
        )
        if not rows:
            return None
        pk = rows[0].get("PK", "")
        return pk.split("USER#", 1)[1] if pk.startswith("USER#") else None

    def link_discord(self, sub: str, discord_id: str) -> None:
        """Link a Discord id to a user, enforcing one-account-per-identity.

        Raises ``ValueError`` if the Discord id is already linked to a
        different account (R3.4). Sets the GSI1 reverse index so a later
        Discord OAuth login resolves this account (R3.2, R3.3).
        """
        existing = self.user_for_discord(discord_id)
        if existing is not None and existing != sub:
            raise ValueError(
                "that Discord account is already linked to another user"
            )
        # update_with_lock preserves existing GSI1 keys but cannot ADD them to
        # an item that lacks them, so re-write the profile with the Discord
        # GSI1 keys set (delete + put_new) to (re)establish the reverse index.
        self._relink(sub, discord_id, self.get(sub))

    def _relink(
        self, sub: str, discord_id: str, current: dict[str, Any]
    ) -> None:
        """Write the profile with the Discord GSI1 keys set."""
        data = {**current, "discord_id": discord_id, "discord_linked": True}
        # Delete + recreate to (re)establish GSI1 keys deterministically; the
        # profile item is small and single-owner so this is safe.
        self._core.delete(user_pk(sub), PROFILE_SK)
        self._core.put_new(
            user_pk(sub),
            PROFILE_SK,
            USER_ENTITY,
            data,
            gsi1pk=_discord_gsi1pk(discord_id),
            gsi1sk="USER",
        )
