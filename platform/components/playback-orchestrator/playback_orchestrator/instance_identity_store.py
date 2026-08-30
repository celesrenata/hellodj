"""Concrete per-bot :class:`InstanceIdentityStore` over the ``hellodj-core`` table.

Reads/writes the per-bot identity item the web-ui ``BotIdentityService`` writes
for a pool bot: ``PK=GUILD#<gid>`` / ``SK=BOTIDENTITY#<client_id>``, entityType
``GuildBotIdentity``. Mirrors the discord-bot-core ``CoreTableIdentityStore``
(the PRIMARY bot's store) but is ``client_id``-scoped so each secondary pool
bot's identity round-trips independently.

``boto3`` stays lazy inside :func:`build_instance_identity_store` so this module
imports cleanly in a test env without AWS; ``CoreTable`` is a pure wrapper.

Requirements: aws-multi-bot-runtime (per-bot identity), web-ui per-bot schema.
"""

from __future__ import annotations

import logging
from typing import Any

from .instance_identity import botidentity_sk

_LOG = logging.getLogger("playback_orchestrator.instance_identity_store")

#: entityType stamped on the item (matches the web-ui writer + primary applier).
_IDENTITY_ENTITY = "GuildBotIdentity"

__all__ = ["CoreTableInstanceIdentityStore", "build_instance_identity_store"]


def _guild_pk(guild_id: str) -> str:
    """Return the ``hellodj-core`` partition key for a guild (web-ui parity)."""
    return f"GUILD#{guild_id}"


class CoreTableInstanceIdentityStore:
    """Read/write a pool bot's ``BOTIDENTITY#<client_id>`` item via ``CoreTable``.

    Implements the applier's :class:`InstanceIdentityStore` protocol: read the
    item's ``data`` mapping (the web-ui's stored shape) and merge the applier's
    status fields back under an optimistic lock, preserving all other fields.
    """

    def __init__(self, core_table: Any) -> None:
        self._core = core_table

    def get_identity_data(
        self, guild_id: str, *, client_id: str
    ) -> dict[str, Any] | None:
        """Return the bot's ``BOTIDENTITY#<client_id>`` ``data`` mapping, or None."""
        item = self._core.get(_guild_pk(guild_id), botidentity_sk(client_id))
        if item is None:
            return None
        data = item.get("data")
        return dict(data) if isinstance(data, dict) else {}

    def set_apply_status(
        self,
        guild_id: str,
        *,
        client_id: str,
        status: str,
        applied_at: int,
        apply_error: str,
        applied_version: str,
    ) -> None:
        """Merge the applier's status fields onto the bot's item.

        Preserves every other field in ``data`` (nickname, avatar_*,
        requested_by, desired_at, ...) so only the writeback fields change.
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
            botidentity_sk(client_id),
            mutate,
            entity_type=_IDENTITY_ENTITY,
        )


def build_instance_identity_store(
    core_table: Any | None,
) -> CoreTableInstanceIdentityStore | None:
    """Wrap an already-built ``CoreTable`` as an instance identity store, or None.

    The orchestrator bootstrap already builds a ``CoreTable`` for the pool-claim
    reads (``instance_bootstrap._core_table``); this reuses it rather than
    building a second DynamoDB resource. Returns ``None`` when no table is
    configured so per-bot identity apply simply stays disabled (degraded), never
    crashing the runtime.
    """
    if core_table is None:
        return None
    return CoreTableInstanceIdentityStore(core_table)
