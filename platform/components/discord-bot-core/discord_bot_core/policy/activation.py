"""Per-guild activation gate (bot half of the on-prem ``/activate`` flow).

A guild does nothing until an administrator runs ``/activate <key>`` with the
key shown on the web dashboard. This is the AWS port of the on-prem activation
gate: the web-ui (``guild_activation_service``) generates + shows the key and
writes ``PK=GUILD#<gid>`` / ``SK=ACTIVATION`` (entityType ``GuildActivation``,
``data={key, activated}``) to the shared ``hellodj-core`` table; this module is
the bot half that VALIDATES the key and flips ``activated`` on success.

The store is injected so the command/gate logic is unit-testable without AWS,
and the bot runs in a degraded (always-locked) mode when no table is configured
— a missing store never lets an unactivated guild through.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from hellodj_platform_logic.data_access import CoreTable

__all__ = [
    "ACTIVATION_SK",
    "ActivationStore",
    "CoreTableActivationStore",
    "GuildActivation",
    "NullActivationStore",
    "build_activation_store",
]

log = logging.getLogger(__name__)

ACTIVATION_SK = "ACTIVATION"
ACTIVATION_ENTITY = "GuildActivation"


def _guild_pk(guild_id: int | str) -> str:
    """Return the ``hellodj-core`` partition key for a guild (web-ui parity)."""
    return f"GUILD#{guild_id}"


class GuildActivation:
    """Read/validate a guild's activation state over ``hellodj-core``.

    Mirrors the web-ui writer's item shape exactly so the two processes agree.
    """

    def __init__(self, store: ActivationStore) -> None:
        self._store = store

    def is_activated(self, guild_id: int) -> bool:
        """Return whether the guild has been activated (default: locked)."""
        data = self._store.get_activation_data(str(guild_id))
        return bool(data and data.get("activated", False))

    def activate(self, guild_id: int, key: str) -> bool:
        """Validate ``key`` against the stored key and activate on match.

        Returns ``True`` and flips ``activated=true`` when the submitted key
        matches the guild's stored key; returns ``False`` (no state change) when
        there is no stored key or the key does not match. Whitespace around the
        submitted key is ignored (on-prem parity).
        """
        data = self._store.get_activation_data(str(guild_id))
        stored_key = (data or {}).get("key", "")
        if not stored_key:
            log.warning(
                "activate: no key generated for guild %s — cannot activate",
                guild_id,
            )
            return False
        if key.strip() != str(stored_key).strip():
            log.warning("activate: invalid key attempt for guild %s", guild_id)
            return False
        self._store.set_activated(str(guild_id), True)
        log.info("activate: guild %s activated", guild_id)
        return True


class ActivationStore(Protocol):
    """Persistence surface the activation gate needs (injectable for tests)."""

    def get_activation_data(self, guild_id: str) -> dict[str, Any] | None:
        """Return the guild's ``ACTIVATION`` ``data`` mapping, or ``None``."""
        ...

    def set_activated(self, guild_id: str, activated: bool) -> None:
        """Set the guild's ``activated`` flag, preserving the key."""
        ...


class NullActivationStore:
    """A degraded :class:`ActivationStore` used when no core table is configured.

    Every guild reads as NOT activated (secure default: locked) and a write is a
    no-op — there is no backing table to persist to. This lets the activation
    cog (and thus the ``/activate`` slash command) ALWAYS be registered, so a
    guild is never left with zero commands and no recovery path: ``/activate``
    is always present. When the store is null, ``/activate`` cannot succeed
    (there is no stored key to match), which is correct — activation genuinely
    requires the table; the fix is that the recovery command still EXISTS and
    the guild is loudly locked rather than silently command-less.
    """

    def get_activation_data(self, guild_id: str) -> dict[str, Any] | None:
        """Return ``None`` — no activation state exists without a table."""
        return None

    def set_activated(self, guild_id: str, activated: bool) -> None:
        """No-op — there is no backing table to persist activation to."""
        log.warning(
            "activation: no core table configured — cannot persist activation "
            "for guild %s (guild stays locked)",
            guild_id,
        )


class CoreTableActivationStore:
    """:class:`ActivationStore` over the ``GUILD#<gid>`` / ``ACTIVATION`` item."""

    def __init__(self, core_table: CoreTable) -> None:
        self._core = core_table

    def get_activation_data(self, guild_id: str) -> dict[str, Any] | None:
        item = self._core.get(_guild_pk(guild_id), ACTIVATION_SK)
        if item is None:
            return None
        data = item.get("data")
        return dict(data) if isinstance(data, dict) else {}

    def set_activated(self, guild_id: str, activated: bool) -> None:
        self._core.update_with_lock(
            _guild_pk(guild_id),
            ACTIVATION_SK,
            lambda d: {**d, "activated": activated},
            entity_type=ACTIVATION_ENTITY,
        )


def build_activation_store(
    table_name: str, region: str | None
) -> CoreTableActivationStore | None:
    """Build a :class:`CoreTableActivationStore`, or ``None`` when unconfigured.

    Mirrors ``build_identity_store``: lazily builds the DynamoDB table and
    returns ``None`` on any failure so a credential-less/misconfigured env
    disables activation writes — but the gate then treats every guild as locked
    (secure default), never as activated.
    """
    if not table_name:
        return None
    try:
        import boto3

        ddb = boto3.resource("dynamodb", region_name=region or "us-east-1")
        return CoreTableActivationStore(CoreTable(ddb.Table(table_name)))
    except Exception:  # noqa: BLE001 - degrade to no activation store
        log.warning("activation: could not build CoreTable activation store")
        return None
