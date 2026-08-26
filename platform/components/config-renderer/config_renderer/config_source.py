"""DynamoDB reader for non-secret Lavalink configuration.

Reads the Lavalink ``Config`` entity from the ``hellodj-core`` single table via
:class:`hellodj_platform_logic.data_access.CoreTable` and maps its ``data``
payload to a :class:`~config_renderer.model.LavalinkSettings`. When the config
item is absent (a fresh, clean-slate table) the architecture defaults are used,
so the renderer still produces a working config.

``boto3`` is imported lazily inside the table factory so the module is
import-safe and unit-testable with an injected ``Table`` (moto/DynamoDB Local).

Requirements: 6.1, 7.3, 15.1
"""

from __future__ import annotations

from typing import Any

from hellodj_platform_logic.data_access import CORE_TABLE_NAME, CoreTable

from .model import LavalinkSettings

__all__ = [
    "LAVALINK_CONFIG_PK",
    "LAVALINK_CONFIG_SK",
    "DynamoConfigSource",
    "build_core_table",
]

#: Primary-key coordinates of the Lavalink config item in ``hellodj-core``.
LAVALINK_CONFIG_PK = "CONFIG#lavalink"
LAVALINK_CONFIG_SK = "CONFIG"


def build_core_table(
    table_name: str = CORE_TABLE_NAME,
    *,
    region_name: str | None = None,
) -> CoreTable:
    """Create a :class:`CoreTable` backed by a real boto3 DynamoDB table."""
    import boto3

    resource = boto3.resource("dynamodb", region_name=region_name)
    return CoreTable(resource.Table(table_name))


class DynamoConfigSource:
    """Loads :class:`LavalinkSettings` from the ``hellodj-core`` table.

    Args:
        core_table: An injected :class:`CoreTable`. Defaults to a real boto3
            resource-backed table via :func:`build_core_table`.
        pk: Partition key of the Lavalink config item.
        sk: Sort key of the Lavalink config item.
    """

    def __init__(
        self,
        core_table: CoreTable | None = None,
        *,
        pk: str = LAVALINK_CONFIG_PK,
        sk: str = LAVALINK_CONFIG_SK,
        region_name: str | None = None,
    ) -> None:
        self._table = core_table or build_core_table(region_name=region_name)
        self._pk = pk
        self._sk = sk

    def load(self) -> LavalinkSettings:
        """Read the config item and map it to settings, or use defaults."""
        item = self._table.get(self._pk, self._sk)
        data: dict[str, Any] = {}
        if item is not None and isinstance(item.get("data"), dict):
            data = dict(item["data"])
        return LavalinkSettings.from_config(data)
