"""Guild ownership and Discord-id-based admin appointment.

Guild control is Discord-derived: a user controls a guild if they are its
recorded ``OWNER`` or their linked Discord id has a Guild_Admin edge for the
guild. The Platform_Owner (Cognito ``admins`` group) is a super-admin and can
manage any guild. ``can_manage_guild`` is the single authorization gate used by
every guild and per-guild-source route (R4, R5.2).

Data model (hellodj-core single table):

* Guild ownership:   ``PK=GUILD#<gid>``  ``SK=OWNER``          data={owner_sub}
* Guild admin edge:  ``PK=GUILD#<gid>``  ``SK=ADMIN#<discordId>``
                     GSI1PK=``DISCORD#<discordId>`` GSI1SK=``GUILDADMIN#<gid>``
                     data={appointed_by, appointed_at}

The reverse GSI1 lets a Discord login enumerate the guilds it administers in a
single indexed query.

Requirements: 4.1, 4.2, 4.3, 4.4, 5.2
"""

from __future__ import annotations

from typing import Any

from hellodj_platform_logic.data_access import CoreTable

__all__ = [
    "GuildAdminService",
    "guild_pk",
    "owner_sk",
    "admin_sk",
    "can_manage_guild",
]

OWNER_SK = "OWNER"
GUILD_ADMIN_ENTITY = "GuildAdmin"
GUILD_OWNER_ENTITY = "GuildOwner"


def guild_pk(guild_id: str) -> str:
    """Return the partition key for a guild's items."""
    return f"GUILD#{guild_id}"


def owner_sk() -> str:
    """Return the sort key for a guild's ownership item."""
    return OWNER_SK


def admin_sk(discord_id: str) -> str:
    """Return the sort key for a guild-admin edge keyed by Discord id."""
    return f"ADMIN#{discord_id}"


def _discord_gsi1pk(discord_id: str) -> str:
    return f"DISCORD#{discord_id}"


def _guildadmin_gsi1sk(guild_id: str) -> str:
    return f"GUILDADMIN#{guild_id}"


def can_manage_guild(
    *,
    guild_id: str,
    user_sub: str | None,
    discord_id: str | None,
    is_super_admin: bool,
    owner_sub: str | None,
    admin_discord_ids: set[str],
) -> bool:
    """Pure authorization decision for guild management (R4.3, R5.2).

    A caller may manage the guild when ANY of:

    * they are the Platform_Owner / super-admin (Cognito ``admins`` group), OR
    * their Cognito subject matches the guild's recorded owner, OR
    * their linked Discord id is an appointed Guild_Admin of the guild.

    This is a pure function over already-resolved facts so it can be unit- and
    property-tested without AWS, and is the single gate every guild/source route
    calls before reading or writing a guild's data or Per_Guild_Secret.
    """
    if is_super_admin:
        return True
    if user_sub is not None and owner_sub is not None and user_sub == owner_sub:
        return True
    if discord_id is not None and discord_id in admin_discord_ids:
        return True
    return False


class GuildAdminService:
    """Guild ownership + Discord-id admin appointment over ``hellodj-core``."""

    def __init__(self, core_table: CoreTable) -> None:
        self._core = core_table

    # -- ownership ----------------------------------------------------------

    def owner_of(self, guild_id: str) -> str | None:
        """Return the owning Cognito subject of a guild, or ``None``."""
        item = self._core.get(guild_pk(guild_id), OWNER_SK)
        if item is None:
            return None
        return item.get("data", {}).get("owner_sub")

    def claim_ownership(self, guild_id: str, user_sub: str) -> None:
        """Record ``user_sub`` as the guild's owner if not already owned."""
        if self.owner_of(guild_id) is not None:
            return
        self._core.put_new(
            guild_pk(guild_id),
            OWNER_SK,
            GUILD_OWNER_ENTITY,
            {"owner_sub": user_sub},
        )

    # -- admin edges --------------------------------------------------------

    def list_admins(self, guild_id: str) -> list[dict[str, Any]]:
        """Return the Discord-id admin edges appointed for a guild."""
        # Admin edges are the guild's items with SK prefix ADMIN#. The core
        # table exposes GSI1 queries; enumerate via the guild's own items by
        # querying each known edge is not available, so we scan via GSI-less
        # get is not possible — use a dedicated query helper on the table.
        rows = self._core.query_pk_prefix(guild_pk(guild_id), sk_prefix="ADMIN#")
        return [
            {
                "discord_id": r["SK"].split("ADMIN#", 1)[1],
                "appointed_by": r.get("data", {}).get("appointed_by", ""),
                "appointed_at": r.get("data", {}).get("appointed_at", 0),
            }
            for r in rows
        ]

    def admin_discord_ids(self, guild_id: str) -> set[str]:
        """Return the set of Discord ids appointed as admins of a guild."""
        return {a["discord_id"] for a in self.list_admins(guild_id)}

    def appoint_admin(
        self, guild_id: str, discord_id: str, appointed_by: str
    ) -> None:
        """Appoint a Discord id as an admin of the guild (idempotent)."""
        existing = self._core.get(guild_pk(guild_id), admin_sk(discord_id))
        if existing is not None:
            return
        self._core.put_new(
            guild_pk(guild_id),
            admin_sk(discord_id),
            GUILD_ADMIN_ENTITY,
            {"appointed_by": appointed_by},
            gsi1pk=_discord_gsi1pk(discord_id),
            gsi1sk=_guildadmin_gsi1sk(guild_id),
        )

    def remove_admin(self, guild_id: str, discord_id: str) -> None:
        """Remove a Discord-id admin edge from the guild."""
        self._core.delete(guild_pk(guild_id), admin_sk(discord_id))

    def guilds_administered_by_discord(self, discord_id: str) -> list[str]:
        """Return guild ids a Discord id is an appointed admin of (via GSI1)."""
        rows = self._core.query_gsi1(
            _discord_gsi1pk(discord_id), sk_prefix="GUILDADMIN#"
        )
        return [r["GSI1SK"].split("GUILDADMIN#", 1)[1] for r in rows]
