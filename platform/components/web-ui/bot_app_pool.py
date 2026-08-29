"""Global Discord bot-application pool + per-guild claim/assignment.

Discord identifies a bot member in a guild by its APPLICATION id, and dedupes
by it: the same application can never appear twice in one guild. So running N
simultaneous bots in a guild requires N distinct Discord applications. HelloDJ
pre-registers a fixed GLOBAL pool of bot applications (created by hand in the
Discord developer portal) and hands them out to guilds on demand.

Storage split (pool secret vs claim items):

* **Pool secret** — ``hellodj/<stage>/bot-app-pool`` in Secrets Manager: a JSON
  array of ``{label, client_id, client_secret, bot_token}``. The client secret /
  bot token are credentials and never leave this module (the account UI only
  ever renders the ``client_id`` in an invite URL). Read-only here.
* **Claim items** — DynamoDB ``hellodj-core`` ``PK=GUILD#<gid>``
  ``SK=BOTAPP#<client_id>`` entityType ``BotAppClaim``. Records that a guild has
  been assigned a pool application (``client_id``, ``label``, ``claimed_by``,
  ``claimed_at``). No credential material lands in DynamoDB.

Assignment rule: an application is GLOBAL — it may serve many guilds — but a
guild can hold each application at most once (Discord's per-guild dedupe). A
guild claims the next pool app it does not already hold, capped by the caller's
``max_bots_per_guild`` entitlement (enforced in the route, re-checked here).

The invite link for a claimed app is
``https://discord.com/oauth2/authorize?client_id=<id>&scope=bot%20applications.commands&permissions=<perms>``.

Callers (guild/account routes) MUST have verified ``can_manage_guild`` first —
this module never assigns/releases a guild's bots without the route gating the
caller's ownership (mirrors :class:`GuildSourcesService`).

Requirements: multi-bot pool + per-guild invite links.
"""

from __future__ import annotations

import time
import urllib.parse
from typing import Any, Protocol

from hellodj_platform_logic.bot_app_pool import parse_pool
from hellodj_platform_logic.data_access import CoreTable

from guild_admin_service import guild_pk

__all__ = [
    "BOTAPP_SK_PREFIX",
    "DISCORD_BOT_INVITE_BASE",
    "DEFAULT_BOT_PERMISSIONS",
    "PoolExhaustedError",
    "QuotaReachedError",
    "BotAppPool",
    "BotAppAssignmentService",
    "botapp_sk",
    "bot_invite_url",
]

#: Sort-key prefix for a guild's claimed bot-application items.
BOTAPP_SK_PREFIX = "BOTAPP#"

BOTAPP_ENTITY = "BotAppClaim"

DISCORD_BOT_INVITE_BASE = "https://discord.com/oauth2/authorize"

#: Default invite permissions bitfield for a music bot: View Channel,
#: Send Messages, Embed Links, Read Message History, Connect, Speak, and Use
#: Application Commands. Computed as the OR of those Discord permission bits.
DEFAULT_BOT_PERMISSIONS = 2150714368


class PoolExhaustedError(Exception):
    """No unclaimed pool application is available for this guild.

    Raised when every pool app is already held by the guild (its per-guild
    capacity equals the global pool size) — the global pool needs more
    registered applications to grow further.
    """


class QuotaReachedError(Exception):
    """Assigning another bot would exceed the guild's per-guild bot quota."""


def botapp_sk(client_id: str) -> str:
    """Return the sort key for a guild's claim of a pool application."""
    return f"{BOTAPP_SK_PREFIX}{client_id}"


def bot_invite_url(
    client_id: str, *, permissions: int = DEFAULT_BOT_PERMISSIONS
) -> str:
    """Return the Discord bot invite URL for an application id.

    The link authorizes the ``bot`` + ``applications.commands`` scopes with the
    given permission bitfield. Only the ``client_id`` (a public application id)
    appears in the URL — never the client secret or bot token.
    """
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "scope": "bot applications.commands",
            "permissions": str(permissions),
        }
    )
    return f"{DISCORD_BOT_INVITE_BASE}?{query}"


class SecretsClient(Protocol):
    """Subset of the boto3 ``secretsmanager`` client used to read the pool."""

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]: ...


class BotAppPool:
    """Read-only view of the global bot-application pool (Secrets Manager).

    Parses ``hellodj/<stage>/bot-app-pool`` (a JSON array) into a stable,
    ordered list of applications. Only the ``label`` + ``client_id`` are
    exposed publicly; the client secret / bot token stay internal (never
    rendered). Degrades to an empty pool when the secret is absent/invalid so
    the app still runs (the account UI then shows "no bots available").
    """

    def __init__(
        self,
        secrets_client: SecretsClient,
        *,
        stage: str,
        primary_client_id: str = "",
    ) -> None:
        self._secrets = secrets_client
        self._stage = stage
        # The Primary_Bot application id (DISCORD_CLIENT_ID). It is the
        # platform's command-owner and already in every guild via
        # discord-bot-core, so it must never be surfaced as an assignable
        # secondary — excluded from the parsed pool regardless of the secret.
        self._primary_client_id = (primary_client_id or "").strip()
        self._cache: list[dict[str, Any]] | None = None

    @property
    def secret_name(self) -> str:
        """Return the pool secret name for this stage."""
        return f"hellodj/{self._stage}/bot-app-pool"

    def _load(self) -> list[dict[str, Any]]:
        if self._cache is not None:
            return self._cache
        try:
            resp = self._secrets.get_secret_value(SecretId=self.secret_name)
            raw = resp.get("SecretString", "") or ""
        except Exception:  # noqa: BLE001 - absent/denied → empty pool
            raw = ""
        # Delegate the pure parsing (JSON array shape, client-id skip, secret
        # handling) to the shared ``parse_pool`` so the web-ui reader and the
        # orchestrator's instance runtime agree exactly. This reader projects
        # the parsed apps down to ONLY the public ``label`` + ``client_id`` —
        # the client secret / bot token never leave the shared ``PoolApp`` and
        # are never cached or rendered here.
        pool: list[dict[str, Any]] = [
            {"label": app.label, "client_id": app.client_id}
            for app in parse_pool(
                raw, exclude_client_ids={self._primary_client_id}
            )
        ]
        self._cache = pool
        return pool

    def client_ids(self) -> list[str]:
        """Return the ordered pool application ids (public)."""
        return [e["client_id"] for e in self._load()]

    def label_for(self, client_id: str) -> str:
        """Return the human label for an application id, or the id itself."""
        for e in self._load():
            if e["client_id"] == client_id:
                return e["label"]
        return client_id

    def size(self) -> int:
        """Return the number of applications in the global pool."""
        return len(self._load())


class BotAppAssignmentService:
    """Assign / list / release per-guild bot-application claims.

    Claims live on ``hellodj-core`` as ``BotAppClaim`` items under the guild.
    Assignment picks the next pool app the guild does not already hold; the
    per-guild quota is enforced by the caller and re-checked here.
    """

    def __init__(self, core_table: CoreTable, pool: BotAppPool) -> None:
        self._core = core_table
        self._pool = pool

    def list_claims(self, guild_id: str) -> list[dict[str, Any]]:
        """Return the guild's claimed bots (label + client_id + invite url).

        Ordered by the pool order for stable rendering. Never includes any
        credential material — only the public application id + its invite link.
        """
        held = {
            r["SK"][len(BOTAPP_SK_PREFIX):]: r.get("data", {})
            for r in self._core.query_pk_prefix(
                guild_pk(guild_id), sk_prefix=BOTAPP_SK_PREFIX
            )
        }
        claims: list[dict[str, Any]] = []
        index = 0
        for cid in self._pool.client_ids():
            if cid in held:
                claims.append(
                    {
                        "client_id": cid,
                        "label": self._pool.label_for(cid),
                        "invite_url": bot_invite_url(cid),
                        "claimed_at": held[cid].get("claimed_at", 0),
                        # Zero-based position among THIS guild's bots, in pool
                        # order — drives the default HelloDJ / HelloDJ#N name.
                        "index": index,
                    }
                )
                index += 1
        return claims

    def pool_size(self) -> int:
        """Return the global pool size (number of registered applications)."""
        return self._pool.size()

    def claim_count(self, guild_id: str) -> int:
        """Return how many pool apps the guild currently holds."""
        return sum(
            1
            for r in self._core.query_pk_prefix(
                guild_pk(guild_id), sk_prefix=BOTAPP_SK_PREFIX
            )
        )

    def assign_next(
        self, guild_id: str, *, max_bots: int, claimed_by: str
    ) -> dict[str, Any]:
        """Assign the next free pool app to a guild, returning its claim row.

        Raises:
            QuotaReachedError: when the guild already holds ``max_bots`` apps.
            PoolExhaustedError: when the guild already holds every pool app.
        """
        held = {
            r["SK"][len(BOTAPP_SK_PREFIX):]
            for r in self._core.query_pk_prefix(
                guild_pk(guild_id), sk_prefix=BOTAPP_SK_PREFIX
            )
        }
        if len(held) >= max_bots:
            raise QuotaReachedError(
                f"guild already has its maximum of {max_bots} bot(s)"
            )
        for cid in self._pool.client_ids():
            if cid not in held:
                self._core.put_new(
                    guild_pk(guild_id),
                    botapp_sk(cid),
                    BOTAPP_ENTITY,
                    {
                        "client_id": cid,
                        "label": self._pool.label_for(cid),
                        "claimed_by": claimed_by,
                        "claimed_at": int(time.time()),
                    },
                )
                return {
                    "client_id": cid,
                    "label": self._pool.label_for(cid),
                    "invite_url": bot_invite_url(cid),
                }
        raise PoolExhaustedError(
            "no unclaimed bot application is available in the global pool"
        )

    def release(self, guild_id: str, client_id: str) -> None:
        """Release (delete) a guild's claim on a pool application.

        Idempotent: a missing claim is a no-op. Only deletes the claim for the
        given guild — never affects another guild's assignment of the same app.
        """
        self._core.delete(guild_pk(guild_id), botapp_sk(client_id))
