"""Configuration read/write for the web-ui backed by DynamoDB.

The web-ui reads and writes platform configuration through the shared
:class:`hellodj_platform_logic.data_access.CoreTable` repository over the
``hellodj-core`` single table (R6.5, R7.1). No PostgreSQL/SQLite is used
anywhere; the legacy encrypted-SQLite credential store is gone.

Configuration lives under a single ``Config`` entity per scope:

* Global platform config:  ``PK=CONFIG#GLOBAL``, ``SK=CONFIG``
* Per-guild config:        ``PK=GUILD#<id>``,    ``SK=CONFIG``

The ``CoreTable`` resource table is *injected* so this module never imports
``boto3`` at load time and stays testable against moto / DynamoDB Local.

Requirements: 6.5, 7.1, 7.3
"""

from __future__ import annotations

from typing import Any

from hellodj_platform_logic.data_access import CoreTable

__all__ = [
    "GLOBAL_CONFIG_PK",
    "CONFIG_SK",
    "CONFIG_ENTITY_TYPE",
    "ConfigStore",
    "guild_config_pk",
]

#: Partition key for the single global platform-configuration item.
GLOBAL_CONFIG_PK = "CONFIG#GLOBAL"

#: Sort key shared by every ``Config`` entity (global or per-guild).
CONFIG_SK = "CONFIG"

#: ``entityType`` discriminator for configuration items in ``hellodj-core``.
CONFIG_ENTITY_TYPE = "Config"


def guild_config_pk(guild_id: str) -> str:
    """Return the ``hellodj-core`` partition key for a guild's config item."""
    return f"GUILD#{guild_id}"


class ConfigStore:
    """Read/update platform configuration on the ``hellodj-core`` table.

    Args:
        core_table: An initialized :class:`CoreTable` repository bound to the
            ``hellodj-core`` DynamoDB (optionally DAX-fronted) resource table.
    """

    def __init__(self, core_table: CoreTable) -> None:
        self._core = core_table

    @property
    def core_table(self) -> CoreTable:
        """Return the underlying :class:`CoreTable` (read-only accessor).

        Lets collaborators (e.g. the registration-mode audit write) reuse the
        same table the config is stored on without reaching into the private
        ``_core`` attribute.
        """
        return self._core

    # -- global config ------------------------------------------------------

    def get_global(self) -> dict[str, Any]:
        """Return the global config payload, or an empty mapping if unset."""
        item = self._core.get(GLOBAL_CONFIG_PK, CONFIG_SK)
        if item is None:
            return {}
        return dict(item.get("data", {}))

    def set_global(self, values: dict[str, Any]) -> dict[str, Any]:
        """Merge ``values`` into the global config, creating it if absent.

        Returns the full merged config payload after the write.
        """
        return self._upsert(GLOBAL_CONFIG_PK, values)

    # -- per-guild config ---------------------------------------------------

    def get_guild(self, guild_id: str) -> dict[str, Any]:
        """Return a guild's config payload, or an empty mapping if unset."""
        item = self._core.get(guild_config_pk(guild_id), CONFIG_SK)
        if item is None:
            return {}
        return dict(item.get("data", {}))

    def set_guild(self, guild_id: str, values: dict[str, Any]) -> dict[str, Any]:
        """Merge ``values`` into a guild's config, creating it if absent."""
        return self._upsert(guild_config_pk(guild_id), values)

    # -- internal -----------------------------------------------------------

    def _upsert(self, pk: str, values: dict[str, Any]) -> dict[str, Any]:
        """Create-or-merge the config item at ``(pk, CONFIG_SK)``.

        Uses the CoreTable optimistic-lock read-modify-write when the item
        already exists, and ``put_new`` on first write. The mutator merges the
        supplied ``values`` over the existing payload so partial updates from
        the UI only touch the fields the form submitted.
        """
        existing = self._core.get(pk, CONFIG_SK)
        if existing is None:
            merged = dict(values)
            self._core.put_new(pk, CONFIG_SK, CONFIG_ENTITY_TYPE, merged)
            return merged

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            data.update(values)
            return data

        updated = self._core.update_with_lock(
            pk, CONFIG_SK, _mutate, entity_type=CONFIG_ENTITY_TYPE
        )
        return dict(updated.get("data", {}))
