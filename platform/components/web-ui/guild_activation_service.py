"""Per-guild activation key (port of the on-prem ``/activate <key>`` gate).

On-prem, a server does nothing until an administrator runs ``/activate <key>``
in Discord with the key shown on the web dashboard — this stops arbitrary
people from adding the bot to a server and using it. This module is the AWS
port of the DASHBOARD half: it generates, stores, shows, and regenerates the
per-guild key on the shared ``hellodj-core`` table. The BOT half
(``discord-bot-core``) reads the SAME item and owns the ``/activate`` command +
command gate.

Data model (hellodj-core single table):

* ``PK=GUILD#<gid>``  ``SK=ACTIVATION``  entityType=GuildActivation
  data={key, activated}

The key mirrors the on-prem shape (``secrets.token_urlsafe(16)``). Regenerating
a key (or deactivating) invalidates the old one so it cannot be reused, exactly
like the on-prem deactivate flow.
"""

from __future__ import annotations

import secrets
from typing import Any

from hellodj_platform_logic.data_access import CoreTable

from guild_admin_service import guild_pk

__all__ = ["GuildActivationService", "ACTIVATION_SK", "new_activation_key"]

ACTIVATION_SK = "ACTIVATION"
ACTIVATION_ENTITY = "GuildActivation"


def new_activation_key() -> str:
    """Return a fresh per-guild activation key (on-prem parity)."""
    return secrets.token_urlsafe(16)


class GuildActivationService:
    """Generate / read / regenerate a guild's activation key over hellodj-core."""

    def __init__(self, core_table: CoreTable) -> None:
        self._core = core_table

    def status(self, guild_id: str) -> dict[str, Any]:
        """Return ``{key, activated}`` for a guild (empty key if none yet)."""
        item = self._core.get(guild_pk(guild_id), ACTIVATION_SK)
        if item is None:
            return {"key": "", "activated": False}
        data = item.get("data", {})
        return {
            "key": data.get("key", ""),
            "activated": bool(data.get("activated", False)),
        }

    def get_or_create_key(self, guild_id: str) -> str:
        """Return the guild's activation key, generating one on first view.

        Mirrors the on-prem dashboard behavior: the key is minted lazily the
        first time the guild's panel is viewed and persisted so ``/activate``
        can validate against it. Idempotent — a subsequent call returns the same
        key without regenerating (which would invalidate a key already handed to
        an admin).
        """
        existing = self._core.get(guild_pk(guild_id), ACTIVATION_SK)
        if existing is not None:
            key = existing.get("data", {}).get("key", "")
            if key:
                return key
        key = new_activation_key()
        self._core.update_with_lock(
            guild_pk(guild_id),
            ACTIVATION_SK,
            lambda d: {**d, "key": key, "activated": bool(d.get("activated", False))},
            entity_type=ACTIVATION_ENTITY,
        )
        return key

    def regenerate_key(self, guild_id: str) -> str:
        """Mint a NEW key and clear activation, invalidating the old key.

        Matches the on-prem deactivate flow: a regenerated key means the old one
        can no longer activate the guild, and the guild returns to the
        not-activated state until an admin runs ``/activate`` with the new key.
        """
        key = new_activation_key()
        self._core.update_with_lock(
            guild_pk(guild_id),
            ACTIVATION_SK,
            lambda d: {**d, "key": key, "activated": False},
            entity_type=ACTIVATION_ENTITY,
        )
        return key
