"""Shared, side-effect-free entitlement decision logic.

This module is the single contract both the web-ui and the bot depend on for
per-user entitlement decisions. It holds the secure-by-default entitlement set
(:data:`DEFAULT_ENTITLEMENTS`) and the pure functions that resolve an effective
entitlement record and answer the capability/quota/cost questions the callers
gate on.

It is intentionally free of ``boto3`` and Flask imports (no I/O, no globals
mutated) so it can be imported unchanged by both processes and exercised with
unit and property tests without AWS or Discord. The bot mirrors
:data:`DEFAULT_ENTITLEMENTS` so the two processes agree exactly (design task 6).

Requirements: 13.1, 13.2, 13.3, 3.2, 11.3, 11.4, 12.2, 12.3, 10.1, 10.2, 10.5
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DEFAULT_ENTITLEMENTS",
    "DEFAULT_MARKUP",
    "PREMIUM_SOURCES",
    "merge_effective",
    "source_allowed",
    "premium_sources_allowed",
    "effective_max_bots_per_guild",
    "quota_reached",
    "over_cap",
    "effective_cost",
    "validate_quota",
]

#: Baseline markup applied over the Bedrock unit cost when none is configured.
#: 1.0 == 100% markup == 2x Bedrock cost (R10.2).
DEFAULT_MARKUP: float = 1.0

#: Secure default entitlement set (R13). Every gated capability defaults to its
#: most-restrictive permitted state; custom identity (avatar and name) defaults
#: to restricted (R13.2). Absence of a field in a stored record resolves to the
#: value here, and no absent field may resolve to a more-permissive value.
DEFAULT_ENTITLEMENTS: dict[str, Any] = {
    "sources": {
        "youtube": False,
        "youtube_music": False,
        "soundcloud": True,  # baseline no-auth source permitted
        "spotify": False,
        "tidal": False,
    },
    "custom_avatar": False,  # R13.2 custom identity restricted
    "custom_name": False,
    "audio_above_96k": False,
    "video_activities": False,
    "visualizations": False,
    "wakeword": False,
    "ai_integration": False,
    # Single gate for the premium streaming services (Spotify, Tidal) — the
    # paid sources outside of YouTube. A premium source is permitted only when
    # BOTH its per-source flag is on AND this capability is enabled. Defaults
    # OFF (restrictive) so an absent record never grants premium streaming.
    "premium_sources": False,
    "max_bots_per_guild": 1,
    "max_bots_per_guild_enabled": False,
    "max_guilds": 1,
    "ai_spend_cap": None,
}

#: The playback sources considered PREMIUM (paid streaming services outside of
#: YouTube). Access to any of these is gated behind the single
#: ``premium_sources`` capability in addition to the provider's own per-source
#: flag. SoundCloud / YouTube / YouTube Music are NOT premium.
PREMIUM_SOURCES: frozenset[str] = frozenset({"spotify", "tidal"})


def merge_effective(stored: dict[str, Any] | None) -> dict[str, Any]:
    """Merge an explicit stored record over :data:`DEFAULT_ENTITLEMENTS`.

    A field absent from ``stored`` takes its default; an explicitly present
    field overrides the default (R13.3). The ``sources`` map is merged per-key
    so a stored record that omits some providers still resolves those providers
    to their per-source default (R13.1/R13.2).

    The returned value is a fresh, independent copy; neither ``stored`` nor
    :data:`DEFAULT_ENTITLEMENTS` is mutated.

    Args:
        stored: The explicit entitlement ``data`` map, or ``None`` when the user
            has no stored record.

    Returns:
        The effective entitlement record.
    """
    effective: dict[str, Any] = dict(DEFAULT_ENTITLEMENTS)
    # Deep-copy the nested sources map so callers cannot mutate the default.
    effective["sources"] = dict(DEFAULT_ENTITLEMENTS["sources"])

    if not stored:
        return effective

    for key, value in stored.items():
        if key == "sources" and isinstance(value, dict):
            merged_sources = dict(DEFAULT_ENTITLEMENTS["sources"])
            merged_sources.update(value)
            effective["sources"] = merged_sources
        else:
            effective[key] = value

    return effective


def premium_sources_allowed(effective: dict[str, Any]) -> bool:
    """Return whether the user may use premium streaming services.

    The single gate over the paid sources outside of YouTube (Spotify, Tidal).
    Secure by default: an absent flag resolves to ``False`` (not permitted).
    """
    return bool(effective.get("premium_sources", False))


def source_allowed(effective: dict[str, Any], provider: str) -> bool:
    """Return whether ``provider`` is enabled in ``effective`` (R3.2/R3.4).

    A source is permitted iff its per-source flag is ``True`` in the effective
    ``sources`` map. In addition, a PREMIUM provider (:data:`PREMIUM_SOURCES` —
    Spotify/Tidal, the paid services outside of YouTube) is permitted only when
    the single ``premium_sources`` capability is ALSO enabled. So a premium
    source needs BOTH its per-source flag and the premium gate; a non-premium
    source needs only its per-source flag.

    An unknown provider (absent from the effective ``sources`` map) is treated
    as not allowed — secure by default.
    """
    sources = effective.get("sources") or {}
    if not bool(sources.get(provider, False)):
        return False
    if provider in PREMIUM_SOURCES and not premium_sources_allowed(effective):
        return False
    return True


def effective_max_bots_per_guild(effective: dict[str, Any]) -> int:
    """Resolve the per-guild bot limit for a user (R11).

    * If the quota is enabled, the stored numeric value applies.
    * If the quota is disabled but the stored value is greater than 1, the
      stored value still applies (R11.3) — the disabled marker does not lower a
      genuinely-provisioned limit.
    * Otherwise the baseline of 1 applies (R11.4).
    """
    enabled = bool(effective.get("max_bots_per_guild_enabled", False))
    stored = int(effective.get("max_bots_per_guild", 1))
    if enabled:
        return stored
    if stored > 1:
        return stored
    return 1


def quota_reached(current: int, limit: int) -> bool:
    """Return whether ``current`` has reached ``limit`` (``current >= limit``).

    Used for both the per-guild bot quota (R11.2) and the guild quota (R12.3).
    """
    return current >= limit


def over_cap(accumulated: float, cap: float | None) -> bool:
    """Return whether an AI tally has reached its cap (R10.5).

    True only when a cap is configured and ``accumulated >= cap`` — equality
    counts as over-cap. Being over-cap is a warning signal; it does not by
    itself hard-block AI requests (enforced at the call site).
    """
    if cap is None:
        return False
    return accumulated >= cap


def effective_cost(bedrock_cost: float, markup: float = DEFAULT_MARKUP) -> float:
    """Return the tally cost for an AI request (R10.1/R10.2).

    ``bedrock_cost * (1 + markup)``. With the default markup of 1.0 this is
    ``2 * bedrock_cost``.
    """
    return bedrock_cost * (1.0 + markup)


def validate_quota(value: int) -> int:
    """Validate a submitted quota value (R12.2).

    Args:
        value: The submitted quota (max guilds or max bots per guild).

    Returns:
        ``value`` unchanged when it is at least 1.

    Raises:
        ValueError: When ``value`` is less than 1.
    """
    if value < 1:
        raise ValueError("quota must be at least 1")
    return value
