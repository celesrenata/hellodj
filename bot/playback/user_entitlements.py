"""HelloDJ — Bot-side per-user entitlement resolution and AI cost metering.

The web-ui (a separate Flask process) authors per-user entitlements — feature
flags and numeric quotas — into the shared ``hellodj-core`` DynamoDB single
table (see ``platform/components/web-ui/entitlement_service.py``). This module
is the OTHER half of that cross-process contract: running inside the Discord
bot, :class:`UserEntitlementResolver` reads the SAME items at runtime so that a
capability disabled in the admin panel takes real effect in Discord rather than
being cosmetic (R14.1).

Shared-contract mirroring (design decision 3)
---------------------------------------------

The secure default entitlement set (:data:`DEFAULT_ENTITLEMENTS`) and the pure
merge/decision logic are **mirrored VERBATIM** from the web-ui's
``entitlements_core.py`` so the two processes agree exactly. The web-ui and the
bot are packaged/deployed independently (web-ui image vs bot image), so this is
an intentional shared copy rather than a shared import — any change to the
default set MUST be made in both files together. Absence of a field in a stored
record resolves to the value here, and no absent field resolves to a
more-permissive value than the default (secure by default, R13).

Storage keys are likewise mirrored from the web-ui service so both sides address
the SAME items on ``hellodj-core``: entitlement ``PK=USER#<sub> SK=ENTITLEMENT``,
AI tally ``PK=USER#<sub> SK=AITALLY``, AI pricing ``PK=CONFIG#AIPRICING SK=CONFIG``.

Identity resolution (design decision 1): entitlements govern a platform account
keyed by Cognito subject (``sub``), not a Discord id. The acting Discord user is
resolved to a ``sub`` via the existing ``UserProfileService`` reverse index
(Discord id → sub, on GSI1) before entitlements are resolved. An unlinked Discord
id resolves to the restrictive :data:`DEFAULT_ENTITLEMENTS` (R14.3, Property 7).

Caching + fail-safe (R14.2, R14.3)
----------------------------------

Effective entitlements are cached per ``sub`` with a bounded TTL and refreshed on
expiry, so an administrator's change takes effect within one TTL without a bot
redeploy (R14.2). On ANY datastore/lookup failure — DynamoDB unavailable, an
unlinked Discord id, a malformed item — the resolver returns
:data:`DEFAULT_ENTITLEMENTS` (restrictive), never a fully-permissive set (R14.3,
Property 7). This mirrors the fail-safe convention of
:class:`playback.guild_credentials.GuildCredentialResolver`.

Design for testability (fakes-friendly, matching the ``FakeSecrets`` style in
``playback/test_guild_credentials.py``): there is **no hard ``boto3`` import at
module load** — DynamoDB access is injected behind the small
:class:`EntitlementStore` and :class:`ProfileIndex` protocols, so this module
stays importable with no boto3 and is exercised with in-memory fakes.
:func:`build_user_entitlement_resolver` wires the real ``CoreTable``-backed seams
at bot startup (lazy boto3 import).

Requirements: 14.1, 14.2, 14.3, 10.1
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

__all__ = [
    "AIPRICING_PK",
    "AIPRICING_SK",
    "AITALLY_ENTITY_TYPE",
    "AITALLY_SK",
    "DEFAULT_ENTITLEMENTS",
    "DEFAULT_MARKUP",
    "DEFAULT_TTL_SECONDS",
    "PREMIUM_SOURCES",
    "ENTITLEMENT_SK",
    "EntitlementStore",
    "ProfileIndex",
    "UserEntitlementResolver",
    "build_user_entitlement_resolver",
    "effective_max_bots_per_guild",
    "merge_effective",
    "quota_reached",
    "user_pk",
]

log = logging.getLogger(__name__)

# ── Mirrored contract constants (kept in lock-step with the web-ui) ─────────
#
# These MUST match ``platform/components/web-ui/entitlements_core.py`` and
# ``platform/components/web-ui/entitlement_service.py`` exactly. The bot and the
# web-ui are separate deployables, so this is a deliberate shared copy — change
# both together.

#: Baseline markup applied over the Bedrock unit cost when none is configured.
#: 1.0 == 100% markup == 2x Bedrock cost (R10.2). Mirrors ``entitlements_core``.
DEFAULT_MARKUP: float = 1.0

#: Secure default entitlement set (R13). Every gated capability defaults to its
#: most-restrictive permitted state; custom identity (avatar and name) defaults
#: to restricted (R13.2). Mirrored VERBATIM from the web-ui ``entitlements_core``
#: so both processes agree exactly (design decision 3).
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
    # BOTH its per-source flag is on AND this capability is enabled. Mirrored
    # VERBATIM from the web-ui ``entitlements_core.DEFAULT_ENTITLEMENTS``.
    "premium_sources": False,
    "max_bots_per_guild": 1,
    "max_bots_per_guild_enabled": False,
    "max_guilds": 1,
    "ai_spend_cap": None,
}

#: The playback sources considered PREMIUM (paid streaming services outside of
#: YouTube). Mirrored VERBATIM from the web-ui ``entitlements_core`` so the bot
#: and web-ui agree on which sources the ``premium_sources`` gate governs.
PREMIUM_SOURCES: frozenset[str] = frozenset({"spotify", "tidal"})

#: Storage keys mirrored from the web-ui ``entitlement_service`` so the reader
#: (this resolver) addresses the SAME items the writer (web-ui) creates.
ENTITLEMENT_SK = "ENTITLEMENT"
AITALLY_SK = "AITALLY"
AIPRICING_PK = "CONFIG#AIPRICING"
AIPRICING_SK = "CONFIG"

#: ``entityType`` discriminator for the AI tally item (matches the web-ui so a
#: bot-created tally item and a web-ui-created one are indistinguishable).
AITALLY_ENTITY_TYPE = "AiTally"

#: Default cache time-to-live, in seconds. An administrator's entitlement change
#: takes effect for a user within this bound (R14.2). Matches the intent of
#: ``GuildCredentialResolver``'s bounded TTL.
DEFAULT_TTL_SECONDS = 60.0


def user_pk(sub: str) -> str:
    """Return the ``hellodj-core`` partition key for a user's items.

    Mirrors the web-ui ``entitlement_service.user_pk`` / ``user_profile.user_pk``
    so both processes key by the stable Cognito subject.
    """
    return f"USER#{sub}"


def merge_effective(stored: dict[str, Any] | None) -> dict[str, Any]:
    """Merge an explicit stored record over :data:`DEFAULT_ENTITLEMENTS`.

    Mirrored VERBATIM from the web-ui ``entitlements_core.merge_effective`` so
    the bot and web-ui resolve identical effective entitlements (design decision
    3). A field absent from ``stored`` takes its default; an explicitly present
    field overrides the default (R13.3). The ``sources`` map is merged per-key so
    a stored record that omits some providers still resolves those providers to
    their per-source default (R13.1/R13.2). The returned value is a fresh,
    independent copy; neither ``stored`` nor :data:`DEFAULT_ENTITLEMENTS` is
    mutated.
    """
    effective: dict[str, Any] = dict(DEFAULT_ENTITLEMENTS)
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


def effective_max_bots_per_guild(effective: dict[str, Any]) -> int:
    """Resolve the per-guild bot limit for a user (R11).

    Mirrored VERBATIM from the web-ui ``entitlements_core.effective_max_bots_per_guild``
    so the bot and web-ui agree on the enforced limit (design decision 3):

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

    Mirrored VERBATIM from the web-ui ``entitlements_core.quota_reached``. Used
    for both the per-guild bot quota (R11.2) and the guild quota (R12.3).
    """
    return current >= limit


# ── injected seams (Protocols) ─────────────────────────────────────────────


class EntitlementStore(Protocol):
    """The ``hellodj-core`` item store the resolver reads/writes.

    Only the operations the resolver needs. Backed for real by a ``CoreTable``
    (via :class:`_CoreTableEntitlementStore`); replaced by an in-memory fake in
    tests so the resolver is exercised without live AWS.
    """

    def get(self, pk: str, sk: str) -> dict[str, Any] | None:
        """Return the ``hellodj-core`` item at (``pk``, ``sk``), or ``None``."""
        ...

    def update_with_lock(
        self,
        pk: str,
        sk: str,
        mutator: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        entity_type: str | None = None,
    ) -> dict[str, Any]:
        """Optimistic-lock read-modify-write of an item's ``data`` payload."""
        ...


class ProfileIndex(Protocol):
    """The Discord id → Cognito sub reverse index.

    Backed for real by the web-ui's ``UserProfileService`` reverse index on GSI1
    (via :class:`_CoreTableProfileIndex`); replaced by an in-memory fake in
    tests. Returns ``None`` for an unlinked Discord id.
    """

    def user_for_discord(self, discord_id: str) -> str | None:
        """Return the Cognito subject linked to a Discord id, or ``None``."""
        ...


class UserEntitlementResolver:
    """Resolve a user's effective entitlements (cached, bounded TTL, fail-safe).

    Parameters
    ----------
    store:
        The :class:`EntitlementStore` reading/writing entitlement, tally, and
        pricing items on ``hellodj-core``.
    profiles:
        The :class:`ProfileIndex` resolving a Discord id to a Cognito sub.
    ttl_seconds:
        Bounded cache TTL. A resolution is reused for at most this many seconds
        before it is refreshed from the datastore (R14.2).
    time_fn:
        Injectable monotonic clock (defaults to :func:`time.monotonic`) so the
        cache TTL is deterministically testable.
    """

    def __init__(
        self,
        store: EntitlementStore,
        profiles: ProfileIndex,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._profiles = profiles
        self._ttl = float(ttl_seconds)
        self._now = time_fn
        # cache: sub -> (expires_at_monotonic, effective entitlements)
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    # -- resolution ---------------------------------------------------------

    def effective_for_discord(self, discord_id: str | int) -> dict[str, Any]:
        """Resolve a Discord user's effective entitlements (R14.1-R14.3).

        Resolves the acting Discord id to a Cognito sub via the reverse index,
        then returns that sub's effective entitlements (explicit record merged
        over the secure defaults). Cached per sub with a bounded TTL (R14.2).

        Fails safe to :data:`DEFAULT_ENTITLEMENTS` on ANY datastore/lookup
        failure or an unlinked Discord id (R14.3, Property 7) — never a
        fully-permissive set.
        """
        try:
            sub = self._profiles.user_for_discord(str(discord_id))
        except Exception as exc:  # noqa: BLE001 - datastore unavailable → defaults
            log.debug(
                "user_entitlements: reverse-index lookup failed for discord %s "
                "(%s) — applying restrictive defaults", discord_id, exc,
            )
            return self._defaults()

        if not sub:
            # Unlinked Discord id: no platform account → restrictive defaults.
            return self._defaults()

        return self.effective_for_sub(sub)

    def effective_for_sub(self, sub: str) -> dict[str, Any]:
        """Return a sub's effective entitlements, cached with a bounded TTL.

        Fails safe to :data:`DEFAULT_ENTITLEMENTS` on any datastore failure
        (R14.3). The returned mapping is a fresh copy so a caller cannot mutate
        the cached value.
        """
        cached = self._cache.get(sub)
        if cached is not None and self._now() < cached[0]:
            return dict(cached[1])

        try:
            item = self._store.get(user_pk(sub), ENTITLEMENT_SK)
            stored = dict(item.get("data", {})) if item else None
            effective = merge_effective(stored)
        except Exception as exc:  # noqa: BLE001 - datastore unavailable → defaults
            log.debug(
                "user_entitlements: entitlement read failed for sub %s (%s) — "
                "applying restrictive defaults", sub, exc,
            )
            # Do NOT cache a fail-safe result; retry on the next call so a
            # transient outage does not pin a user to defaults for a full TTL.
            return self._defaults()

        self._cache[sub] = (self._now() + self._ttl, effective)
        return dict(effective)

    def invalidate(self, sub: str) -> None:
        """Drop any cached resolution for a sub (forces a refresh next call)."""
        self._cache.pop(sub, None)

    def sub_for_discord(self, discord_id: str | int) -> str | None:
        """Resolve a Discord id to its Cognito sub, or ``None`` (fail-safe).

        A thin wrapper over the reverse index used by the AI gate to meter cost
        against the platform account (metering is keyed by ``sub``, R10.1). An
        unlinked id or any lookup failure yields ``None`` so a metering path can
        skip (no ``sub`` → no tally) without crashing.
        """
        try:
            return self._profiles.user_for_discord(str(discord_id))
        except Exception as exc:  # noqa: BLE001 - lookup failure → no sub
            log.debug("user_entitlements: sub lookup failed (%s)", exc)
            return None

    def ai_tally_for_sub(self, sub: str) -> float:
        """Return a user's accumulated AI tally, or ``0.0`` (fail-safe).

        Used by the AI gate to surface an over-cap warning (R10.5). A missing
        item or read failure yields ``0.0`` (treated as under cap) so the warning
        path never blocks or crashes an otherwise-permitted request.
        """
        try:
            item = self._store.get(user_pk(sub), AITALLY_SK)
        except Exception as exc:  # noqa: BLE001 - unreadable → 0.0
            log.debug("user_entitlements: tally read failed (%s)", exc)
            return 0.0
        data = (item or {}).get("data") or {}
        try:
            return float(data.get("accumulated_cost", 0.0))
        except (TypeError, ValueError):
            return 0.0

    # -- AI cost metering ---------------------------------------------------

    def record_ai_cost(self, sub: str, bedrock_cost: float) -> None:
        """Apply the pricing markup and increment the user's AI tally (R10.1).

        Reads the shared ``CONFIG#AIPRICING`` markup (default 1.0 == 100% ==
        2x Bedrock cost, R10.2) and increments the ``AITALLY`` item by the
        effective cost ``bedrock_cost * (1 + markup)`` with an optimistic-lock
        read-modify-write so concurrent meters accumulate rather than clobber.

        Metering is best-effort: a datastore failure is logged and swallowed so
        a metering outage never crashes the AI request path. (The gate that
        decides whether to permit the request is separate; this only records the
        cost of a request that was already permitted.)
        """
        markup = self._resolve_markup()
        effective = bedrock_cost * (1.0 + markup)

        def _increment(data: dict[str, Any]) -> dict[str, Any]:
            current = float(data.get("accumulated_cost", 0.0))
            data["accumulated_cost"] = current + effective
            data.setdefault("currency", "USD")
            return data

        try:
            self._store.update_with_lock(
                user_pk(sub),
                AITALLY_SK,
                _increment,
                entity_type=AITALLY_ENTITY_TYPE,
            )
        except Exception as exc:  # noqa: BLE001 - metering must never crash play
            log.warning(
                "user_entitlements: failed to record AI cost for sub %s (%s)",
                sub, exc,
            )

    # -- internals ----------------------------------------------------------

    def _resolve_markup(self) -> float:
        """Return the configured AI markup, defaulting to :data:`DEFAULT_MARKUP`.

        Prices/markup are data (``CONFIG#AIPRICING``), not code (R10.3), so a
        price/markup change is a data edit with no bot redeploy. A missing item
        or read failure falls back to the default markup.
        """
        try:
            item = self._store.get(AIPRICING_PK, AIPRICING_SK)
        except Exception as exc:  # noqa: BLE001 - unreadable → default markup
            log.debug(
                "user_entitlements: pricing read failed (%s) — default markup",
                exc,
            )
            return DEFAULT_MARKUP
        if not item:
            return DEFAULT_MARKUP
        data = item.get("data") or {}
        try:
            return float(data.get("markup", DEFAULT_MARKUP))
        except (TypeError, ValueError):
            return DEFAULT_MARKUP

    def _defaults(self) -> dict[str, Any]:
        """Return a fresh copy of the restrictive default entitlement set."""
        return merge_effective(None)


# ── real seams over CoreTable (wired at bot startup) ────────────────────────


class _CoreTableEntitlementStore:
    """:class:`EntitlementStore` backed by a ``CoreTable`` repository."""

    def __init__(self, core_table: Any) -> None:
        self._core = core_table

    def get(self, pk: str, sk: str) -> dict[str, Any] | None:
        return self._core.get(pk, sk)

    def update_with_lock(
        self,
        pk: str,
        sk: str,
        mutator: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        entity_type: str | None = None,
    ) -> dict[str, Any]:
        return self._core.update_with_lock(
            pk, sk, mutator, entity_type=entity_type
        )


class _CoreTableProfileIndex:
    """:class:`ProfileIndex` backed by the ``UserProfileService`` reverse index.

    Mirrors the web-ui ``UserProfileService.user_for_discord`` logic VERBATIM —
    a single GSI1 query on ``GSI1PK=DISCORD#<discordId>`` / ``GSI1SK`` prefix
    ``USER`` — over the same ``CoreTable`` so the bot resolves Discord → sub
    exactly as the web-ui does. The query is reimplemented here (rather than
    importing ``UserProfileService``) so the bot does not depend on the web-ui
    package being on its import path; the reverse-index shape is the shared
    contract, kept in lock-step with ``platform/components/web-ui/user_profile.py``.
    """

    #: The GSI1 sort-key marker the web-ui writes for the reverse index. Shared
    #: verbatim with ``UserProfileService._relink`` (``GSI1SK="USER"``).
    _USER_GSI1SK = "USER"

    def __init__(self, core_table: Any) -> None:
        self._core = core_table

    def user_for_discord(self, discord_id: str) -> str | None:
        rows = self._core.query_gsi1(
            f"DISCORD#{discord_id}", sk_prefix=self._USER_GSI1SK
        )
        if not rows:
            return None
        pk = rows[0].get("PK", "")
        return pk.split("USER#", 1)[1] if pk.startswith("USER#") else None


def build_user_entitlement_resolver(
    *,
    table_name: str = "hellodj-core",
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> UserEntitlementResolver | None:
    """Construct a real ``CoreTable``-backed resolver, or ``None`` on failure.

    Lazily imports ``boto3`` and the shared ``hellodj_platform_logic`` /
    ``UserProfileService`` so this module stays importable where those are
    absent (local dev / unit tests). On ANY construction failure (boto3 missing,
    no credentials, package unavailable) it logs and returns ``None`` so the
    caller can decide how to degrade — the bot wires this at startup and simply
    skips entitlement resolution if it is ``None``, matching the non-fatal
    convention of :func:`bot._build_guild_credential_resolver`.

    The Discord → sub reverse index is resolved directly over the same
    ``CoreTable`` GSI1 (:class:`_CoreTableProfileIndex`), mirroring the web-ui
    ``UserProfileService`` shape, so resolution is identical on both sides
    without the bot depending on the web-ui package.
    """
    try:
        import boto3  # lazy — only present/needed in the SaaS deployment
        from hellodj_platform_logic.data_access import CoreTable

        ddb = boto3.resource("dynamodb")
        table = ddb.Table(table_name)
        core = CoreTable(table)
        store = _CoreTableEntitlementStore(core)
        profiles = _CoreTableProfileIndex(core)
        resolver = UserEntitlementResolver(
            store, profiles, ttl_seconds=ttl_seconds
        )
        log.info(
            "user_entitlements: resolver wired (table=%s, ttl=%ss)",
            table_name, ttl_seconds,
        )
        return resolver
    except Exception as exc:  # noqa: BLE001 - non-fatal: skip entitlement gates
        log.info(
            "user_entitlements: resolver unavailable (%s) — entitlement gates "
            "fall back to restrictive defaults", exc,
        )
        return None
