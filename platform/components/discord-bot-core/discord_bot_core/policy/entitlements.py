"""Per-guild feature entitlement gating for slash-command visibility.

A paying customer must never see commands for features they don't have — a
missing feature should be INVISIBLE, not just blocked, so unpurchased features
aren't advertised in the command picker. This module is the bot half of that:

* it resolves the guild OWNER's effective entitlements from the shared
  ``hellodj-core`` table (``GUILD#<gid>``/``OWNER`` → owner sub →
  ``USER#<sub>``/``ENTITLEMENT``), merged over secure defaults, and
* it maps each *feature* command to the entitlement that gates it, so the
  gateway can filter the visible/synced command set per guild.

Entitlement semantics MIRROR the web-ui's ``entitlements_core`` (same secure
defaults: every gated feature defaults to OFF). Baseline commands — the ones NOT
in :data:`COMMAND_FEATURE_ENTITLEMENT` (e.g. ``play``/``skip``/``pause``,
``activate``, ``help``) — require no entitlement and are always available once a
guild is activated.

Adding a feature command later is a ONE-LINE change: map its command name to the
boolean entitlement key that gates it in :data:`COMMAND_FEATURE_ENTITLEMENT`.

The datastore access is injected (a ``CoreTable``-like reader) so the pure
gating decisions are unit-testable without AWS.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

log = logging.getLogger(__name__)

__all__ = [
    "COMMAND_FEATURE_ENTITLEMENT",
    "DEFAULT_FEATURE_ENTITLEMENTS",
    "EntitlementResolver",
    "build_entitlement_resolver",
    "command_visible_for_entitlements",
    "entitlement_allowed_commands",
    "feature_entitlement_for",
    "merge_feature_effective",
]

#: Secure defaults for the boolean FEATURE entitlements the bot gates commands
#: on. Mirrors the corresponding keys in the web-ui ``entitlements_core``
#: ``DEFAULT_ENTITLEMENTS`` — every gated feature defaults to OFF (R13.2), so an
#: absent/unresolved entitlement never reveals a feature.
DEFAULT_FEATURE_ENTITLEMENTS: dict[str, bool] = {
    "video_activities": False,
    "visualizations": False,
    "wakeword": False,
    "ai_integration": False,
}

#: Map of slash-command name → the boolean entitlement key that gates it. A
#: command ABSENT here is baseline (no entitlement required). Populated as
#: feature cogs are added; e.g. a future ``visualizer`` command would map to
#: ``"visualizations"``. Empty today because the bot only ships baseline
#: playback commands — the gating MECHANISM is live and tested via the pure
#: helpers below, ready for the first feature command.
COMMAND_FEATURE_ENTITLEMENT: dict[str, str] = {}


def merge_feature_effective(stored: dict[str, Any] | None) -> dict[str, bool]:
    """Merge a stored entitlement record over the feature defaults.

    Only the boolean FEATURE keys the bot gates on are considered; every one
    absent from ``stored`` resolves to its secure default (OFF). This mirrors
    the web-ui ``entitlements_core.merge_effective`` semantics for those keys
    without pulling the full quota/source/cost surface the bot doesn't need.
    """
    effective = dict(DEFAULT_FEATURE_ENTITLEMENTS)
    if stored:
        for key in DEFAULT_FEATURE_ENTITLEMENTS:
            if key in stored:
                effective[key] = bool(stored[key])
    return effective


def feature_entitlement_for(command_name: str) -> str | None:
    """Return the entitlement key gating ``command_name``, or ``None``.

    ``None`` means the command is baseline (no entitlement required).
    """
    return COMMAND_FEATURE_ENTITLEMENT.get(command_name)


def command_visible_for_entitlements(
    command_name: str,
    effective: dict[str, bool],
    *,
    command_map: dict[str, str] | None = None,
) -> bool:
    """Return whether ``command_name`` is permitted by ``effective`` entitlements.

    * A command with no gating entitlement (baseline) is always permitted.
    * A gated command is permitted only when its entitlement is truthy.

    ``command_map`` overrides the module map (tests inject a synthetic one to
    exercise the mechanism regardless of which real feature commands exist).
    """
    cmap = COMMAND_FEATURE_ENTITLEMENT if command_map is None else command_map
    key = cmap.get(command_name)
    if key is None:
        return True
    return bool(effective.get(key, False))


def entitlement_allowed_commands(
    names: frozenset[str] | set[str],
    effective: dict[str, bool],
    *,
    command_map: dict[str, str] | None = None,
) -> set[str]:
    """Filter ``names`` to those the ``effective`` entitlements permit.

    Baseline commands always pass; feature commands pass only when their gating
    entitlement is enabled. This is the pure decision the gateway applies AFTER
    the activation-visibility filter so unpurchased features are hidden.
    """
    return {
        n
        for n in names
        if command_visible_for_entitlements(
            n, effective, command_map=command_map
        )
    }


class _CoreTableLike(Protocol):
    """Minimal ``CoreTable`` surface the resolver reads (injectable for tests)."""

    def get(self, pk: str, sk: str) -> dict[str, Any] | None:
        """Return the item at ``(pk, sk)`` or ``None``."""
        ...


class EntitlementResolver:
    """Resolve a guild owner's effective FEATURE entitlements over hellodj-core.

    Reads ``GUILD#<gid>``/``OWNER`` → ``owner_sub`` → ``USER#<sub>``/``ENTITLEMENT``
    and merges the stored flags over the secure defaults. Every failure or
    missing item resolves to the secure defaults (all features OFF), so a guild
    only ever sees a feature command when its owner's entitlement positively
    enables it.
    """

    def __init__(self, core_table: _CoreTableLike) -> None:
        self._core = core_table

    def effective_for_guild(self, guild_id: int | str) -> dict[str, bool]:
        """Return the guild owner's effective feature entitlements."""
        try:
            owner = self._core.get(f"GUILD#{guild_id}", "OWNER")
            owner_sub = (owner or {}).get("data", {}).get("owner_sub", "")
            if not owner_sub:
                return dict(DEFAULT_FEATURE_ENTITLEMENTS)
            ent = self._core.get(f"USER#{owner_sub}", "ENTITLEMENT")
            stored = (ent or {}).get("data") if ent else None
            return merge_feature_effective(
                stored if isinstance(stored, dict) else None
            )
        except Exception as exc:  # noqa: BLE001 - secure default on any error
            log.warning(
                "entitlements: resolution failed for guild %s: %s",
                guild_id,
                exc,
            )
            return dict(DEFAULT_FEATURE_ENTITLEMENTS)


def build_entitlement_resolver(
    table_name: str, region: str | None
) -> EntitlementResolver | None:
    """Build an :class:`EntitlementResolver`, or ``None`` when unconfigured.

    Mirrors ``build_activation_store``: lazily builds the DynamoDB-backed
    ``CoreTable`` and returns ``None`` on any failure so a credential-less env
    disables entitlement resolution — the gateway then applies the secure
    default (feature commands hidden) rather than revealing them.
    """
    if not table_name:
        return None
    try:
        import boto3
        from hellodj_platform_logic.data_access import CoreTable

        ddb = boto3.resource("dynamodb", region_name=region or "us-east-1")
        return EntitlementResolver(CoreTable(ddb.Table(table_name)))
    except Exception:  # noqa: BLE001 - degrade to no resolver
        log.warning("entitlements: could not build CoreTable resolver")
        return None
