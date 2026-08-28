"""Concrete :class:`IdentityStore` over the ``hellodj-core`` single table.

The applier reads a guild's desired identity and writes back its apply status
through the :class:`~discord_bot_core.identity.applier.IdentityStore` protocol.
This module supplies the production implementation backed by
:class:`hellodj_platform_logic.data_access.CoreTable`, addressing the same item
the web-ui ``BotIdentityService`` writes: ``PK=GUILD#<gid>`` / ``SK=BOTIDENTITY``
with entityType ``GuildBotIdentity``.

The shared ``hellodj_platform_logic`` package is bundled into this component's
source tree by the pipeline (mirroring web-ui / tidal-stream), so it is imported
at module top. ``boto3`` stays lazy inside :func:`build_identity_store` so this
module imports cleanly in a test env without AWS.
"""

from __future__ import annotations

import logging
from typing import Any

from hellodj_platform_logic.data_access import CoreTable

from .applier import BOTIDENTITY_SK

__all__ = ["CoreTableIdentityStore", "build_identity_store"]

log = logging.getLogger(__name__)

#: entityType stamped on the item when created (matches the web-ui writer).
IDENTITY_ENTITY = "GuildBotIdentity"


def _guild_pk(guild_id: str) -> str:
    """Return the ``hellodj-core`` partition key for a guild (web-ui parity)."""
    return f"GUILD#{guild_id}"


class CoreTableIdentityStore:
    """Read/write a guild's ``BOTIDENTITY`` item via :class:`CoreTable`.

    Implements the applier's ``IdentityStore`` protocol:

    * :meth:`get_identity_data` reads the item's ``data`` mapping (the web-ui's
      stored shape) so the applier can diff it.
    * :meth:`set_apply_status` merges the applier's status fields back onto the
      item under an optimistic lock, preserving all other data fields.
    """

    def __init__(self, core_table: CoreTable) -> None:
        self._core = core_table

    def get_identity_data(self, guild_id: str) -> dict[str, Any] | None:
        """Return the guild's ``BOTIDENTITY`` ``data`` mapping, or ``None``.

        ``applied_version`` lives inside ``data`` (the web-ui never writes it;
        the applier writes it via :meth:`set_apply_status`), so it round-trips
        naturally and ``DesiredIdentity.from_data`` reads it back.
        """
        item = self._core.get(_guild_pk(guild_id), BOTIDENTITY_SK)
        if item is None:
            return None
        data = item.get("data")
        return dict(data) if isinstance(data, dict) else {}

    def set_apply_status(
        self,
        guild_id: str,
        *,
        status: str,
        applied_at: int,
        apply_error: str,
        applied_version: str,
    ) -> None:
        """Merge the applier's status fields onto the guild's item.

        Preserves every other field in ``data`` (nickname, avatar_*, requested_by,
        desired_at, ...) so only the writeback fields change.
        """

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            merged = dict(current)
            merged.update(
                apply_status=status,
                applied_at=applied_at,
                apply_error=apply_error,
                applied_version=applied_version,
            )
            return merged

        self._core.update_with_lock(
            _guild_pk(guild_id),
            BOTIDENTITY_SK,
            mutate,
            entity_type=IDENTITY_ENTITY,
        )


def build_identity_store(
    table_name: str, region: str | None
) -> CoreTableIdentityStore | None:
    """Build a :class:`CoreTableIdentityStore` from a DynamoDB table name.

    Lazily builds ``boto3.resource("dynamodb").Table(table_name)`` (mirrors the
    web-ui ``bootstrap._core_table``) and returns ``None`` on any failure, so a
    misconfigured or credential-less environment simply disables identity apply
    rather than crashing the bot.
    """
    if not table_name:
        return None
    try:
        import boto3

        ddb = boto3.resource(
            "dynamodb", region_name=region or "us-east-1"
        )
        return CoreTableIdentityStore(CoreTable(ddb.Table(table_name)))
    except Exception:  # noqa: BLE001 - degrade to no identity store
        log.warning("identity-apply: could not build CoreTable identity store")
        return None
