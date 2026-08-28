"""Per-guild bot-identity apply subpackage for discord-bot-core.

The web-ui persists a guild's desired bot identity (nickname + server avatar) to
the ``hellodj-core`` DynamoDB table and S3; this subpackage is the bot-side half
that reads those persisted items and applies the changes to Discord.

* :mod:`~discord_bot_core.identity.applier` — the injectable
  :class:`IdentityApplier` plus its pure diff/plan helpers and protocols.
* :mod:`~discord_bot_core.identity.store` — a concrete
  :class:`CoreTableIdentityStore` implementing the applier's ``IdentityStore``
  protocol over ``hellodj_platform_logic.data_access.CoreTable``.
"""

from __future__ import annotations

from .applier import (
    BOTIDENTITY_SK,
    ApplyOutcome,
    DesiredIdentity,
    IdentityApplier,
    IdentityStore,
    S3Reader,
    avatar_content_type,
    avatar_data_uri,
    plan_apply,
)
from .store import CoreTableIdentityStore, build_identity_store

__all__ = [
    "BOTIDENTITY_SK",
    "ApplyOutcome",
    "CoreTableIdentityStore",
    "DesiredIdentity",
    "IdentityApplier",
    "IdentityStore",
    "S3Reader",
    "avatar_content_type",
    "avatar_data_uri",
    "build_identity_store",
    "plan_apply",
]
