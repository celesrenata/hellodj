"""Per-bot identity applier for the AWS multi-bot instance runtime.

The web-ui persists a desired identity (name + avatar) PER pool bot at the
``hellodj-core`` item ``PK=GUILD#<gid>`` / ``SK=BOTIDENTITY#<client_id>``
(entityType ``GuildBotIdentity``), with the avatar bytes uploaded to the
stage-scoped S3 assets bucket (see ``web-ui/bot_identity.py``). The primary
bot's own applier (``discord-bot-core/identity/applier.py``) applies the
PRIMARY's per-guild nickname + member avatar; it never touches the SECONDARY
pool bots, which are voice-only :class:`discord.Client`s connected by the
orchestrator :class:`~playback_orchestrator.instance_runtime.AwsInstanceOrchestrator`.

This module is the missing apply half for those secondaries. Each secondary is
its OWN distinct Discord application, so its identity is the application user's
GLOBAL username + avatar (``ClientUser.edit(username=..., avatar=...)``) — not a
per-guild nickname hack. The applier:

* reads the per-bot ``BOTIDENTITY#<client_id>`` item for each connected instance,
* diffs the desired name/avatar against what was last applied (a pure,
  idempotent version compare — Discord rate-limits global username edits hard,
  so we only call it on a genuine change),
* applies name via ``ClientUser.edit(username=...)`` and avatar via
  ``ClientUser.edit(avatar=<bytes>)`` (the global-user edit accepts raw bytes,
  unlike the primary's per-guild member PATCH which needs a data URI),
* writes ``apply_status`` / ``applied_at`` / ``apply_error`` back on the item so
  the web-ui identity tab surfaces the result.

Design for testability mirrors the primary applier: no hard ``discord`` /
``boto3`` import at module load, every side-effecting seam injected (the item
store, the S3 reader, and the per-instance client lookup), and the pure
diff/status logic in plain functions.

Requirements: aws-multi-bot-runtime (per-bot identity), reusing the web-ui
per-bot ``BOTIDENTITY#<client_id>`` schema.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

_LOG = logging.getLogger("playback_orchestrator.instance_identity")

__all__ = [
    "BOTIDENTITY_SK_PREFIX",
    "DesiredBotIdentity",
    "IdentityApplyOutcome",
    "InstanceIdentityApplier",
    "InstanceIdentityStore",
    "S3Reader",
    "apply_instance_identities",
    "botidentity_sk",
    "plan_bot_identity_apply",
]

#: Base sort key + per-bot prefix (mirrors the web-ui ``bot_identity`` writer).
#: A per-bot item is keyed ``BOTIDENTITY#<client_id>``; the bare ``BOTIDENTITY``
#: is the legacy single-bot key (not used for pool bots, but read-compatible).
_BOTIDENTITY_SK = "BOTIDENTITY"
BOTIDENTITY_SK_PREFIX = f"{_BOTIDENTITY_SK}#"

#: entityType stamped on the item (matches the web-ui writer + primary applier).
_IDENTITY_ENTITY = "GuildBotIdentity"

#: apply_status values written back for the web-ui identity tab to read.
_STATUS_APPLIED = "applied"
_STATUS_ERROR = "error"


def botidentity_sk(client_id: str = "") -> str:
    """Return the sort key for a bot's identity item (web-ui parity).

    Empty ``client_id`` yields the legacy per-guild key (``BOTIDENTITY``); a
    concrete application id yields the per-bot key (``BOTIDENTITY#<client_id>``).
    """
    return _BOTIDENTITY_SK if not client_id else f"{BOTIDENTITY_SK_PREFIX}{client_id}"


# -- injected seams (Protocols) ---------------------------------------------


class InstanceIdentityStore(Protocol):
    """Read a bot's desired identity ``data`` and write back apply status.

    Addresses ``PK=GUILD#<gid>`` / ``SK=BOTIDENTITY#<client_id>`` on the
    ``hellodj-core`` table — the exact item the web-ui ``BotIdentityService``
    writes for a pool bot.
    """

    def get_identity_data(
        self, guild_id: str, *, client_id: str
    ) -> dict[str, Any] | None:
        """Return the bot's ``BOTIDENTITY#<client_id>`` ``data`` mapping, or None."""
        ...

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
        """Write the applier's status fields back onto the bot's item."""
        ...


class S3Reader(Protocol):
    """Subset of the boto3 ``s3`` client used to read avatar bytes (read-only)."""

    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...


# -- pure value objects + diff/status logic (no discord/boto imports) -------


@dataclass(frozen=True)
class DesiredBotIdentity:
    """A pool bot's desired identity + applier bookkeeping, parsed from ``data``.

    ``applied_version`` combines the applied name and avatar version so the diff
    only fires on a genuine change (idempotent polling — global username edits
    are heavily rate-limited, so re-applying an unchanged identity must be a
    no-op).
    """

    nickname: str = ""
    avatar_present: bool = False
    avatar_key: str = ""
    avatar_version: str = ""
    desired_at: int = 0
    applied_at: int = 0
    apply_status: str = "none"
    applied_version: str = ""

    @classmethod
    def from_data(cls, data: dict[str, Any] | None) -> DesiredBotIdentity:
        """Build from the item's ``data`` mapping (the web-ui's stored shape)."""
        d = data or {}
        return cls(
            nickname=str(d.get("nickname", "") or ""),
            avatar_present=bool(d.get("avatar_present", False)),
            avatar_key=str(d.get("avatar_key", "") or ""),
            avatar_version=str(d.get("avatar_version", "") or ""),
            desired_at=int(d.get("desired_at", 0) or 0),
            applied_at=int(d.get("applied_at", 0) or 0),
            apply_status=str(d.get("apply_status", "none") or "none"),
            applied_version=str(d.get("applied_version", "") or ""),
        )

    def desired_version(self) -> str:
        """A stable marker of the CURRENT desired identity (name + avatar)."""
        return f"name={self.nickname}\x1favatar={self.avatar_version}"


@dataclass
class IdentityApplyOutcome:
    """The result of planning/applying one pool bot's identity change."""

    changed: bool = False
    apply_name: bool = False
    apply_avatar: bool = False
    status: str = ""
    apply_error: str = ""
    applied_version: str = ""
    errors: list[str] = field(default_factory=list)


def plan_bot_identity_apply(desired: DesiredBotIdentity) -> IdentityApplyOutcome:
    """Decide what (if anything) to apply for a pool bot — PURE, no side effects.

    Fires only when the desired version differs from the last applied version
    (change-only application, so a periodic apply pass is idempotent). The name
    is applied when set; the avatar when present with a key.
    """
    if desired.desired_version() == desired.applied_version:
        return IdentityApplyOutcome(changed=False)
    return IdentityApplyOutcome(
        changed=True,
        apply_name=bool(desired.nickname),
        apply_avatar=bool(desired.avatar_present and desired.avatar_key),
        applied_version=desired.desired_version(),
    )


# -- the applier (side-effecting; discord imported lazily) ------------------


class InstanceIdentityApplier:
    """Apply persisted per-bot identity to each connected secondary client.

    Args:
        store: The :class:`InstanceIdentityStore` reading desired identity +
            writing status (DynamoDB ``hellodj-core`` backed).
        s3_client: A :class:`S3Reader` for the avatar bytes.
        avatar_bucket: The stage-scoped S3 bucket the web-ui uploaded avatar
            bytes to.
        time_fn: Injectable epoch-seconds clock (defaults to ``time.time``).
    """

    def __init__(
        self,
        store: InstanceIdentityStore,
        s3_client: S3Reader,
        *,
        avatar_bucket: str,
        time_fn: Any = time.time,
    ) -> None:
        self._store = store
        self._s3 = s3_client
        self._bucket = avatar_bucket
        self._now = time_fn

    async def apply_instance(
        self, client: Any, guild_id: str | int, client_id: str
    ) -> IdentityApplyOutcome:
        """Read + diff + apply one connected secondary's identity, writing status.

        ``client`` is the connected :class:`discord.Client` for the pool app
        whose Discord application id is ``client_id``; ``guild_id`` is the guild
        whose claim authorized the instance (the identity item is per guild+bot).
        Never raises for the expected rate-limit / permission cases — a
        ``discord.HTTPException`` is caught and recorded as ``apply_status=error``.
        """
        gid = str(guild_id)
        cid = str(client_id)
        data = self._store.get_identity_data(gid, client_id=cid)
        desired = DesiredBotIdentity.from_data(data)
        outcome = plan_bot_identity_apply(desired)
        if not outcome.changed:
            return outcome

        user = getattr(client, "user", None)
        if user is None:
            # Client not ready yet — leave the item pending for a later pass.
            _LOG.debug(
                "instance identity: client for app %s not ready, skipping", cid
            )
            outcome.changed = False
            return outcome

        edit_kwargs: dict[str, Any] = {}
        if outcome.apply_name:
            edit_kwargs["username"] = desired.nickname
        if outcome.apply_avatar:
            try:
                edit_kwargs["avatar"] = self._read_avatar(desired.avatar_key)
            except Exception as exc:  # noqa: BLE001 - surface a clear message
                outcome.errors.append(f"Failed to read avatar: {exc}.")

        if edit_kwargs:
            try:
                await user.edit(**edit_kwargs)
                _LOG.info(
                    "instance identity: applied %s for app %s (guild %s)",
                    "+".join(sorted(edit_kwargs)),
                    cid,
                    gid,
                )
            except self._http_error_type() as exc:
                outcome.errors.append(f"Discord rejected the identity edit: {exc}.")
            except Exception as exc:  # noqa: BLE001 - surface a clear message
                outcome.errors.append(f"Failed to apply identity: {exc}.")

        self._write_back(gid, cid, outcome)
        return outcome

    # -- side-effecting helpers ---------------------------------------------

    def _read_avatar(self, avatar_key: str) -> bytes:
        """Read the avatar image bytes from S3 (the global-user edit takes bytes)."""
        raw = self._s3.get_object(Bucket=self._bucket, Key=avatar_key)
        body = raw["Body"]
        return body.read() if hasattr(body, "read") else bytes(body)

    def _write_back(
        self, guild_id: str, client_id: str, outcome: IdentityApplyOutcome
    ) -> None:
        """Record applied/error status back on the item for the UI to read.

        On any error the change is marked ``error`` and the applied version is
        NOT advanced, so a corrected retry (or a rate-limit backoff) re-applies.
        On success the version advances so the change is not re-applied.
        """
        if outcome.errors:
            outcome.status = _STATUS_ERROR
            outcome.apply_error = " ".join(outcome.errors)
            outcome.applied_version = ""  # do not advance -> retry next pass
            applied_at = 0
        else:
            outcome.status = _STATUS_APPLIED
            outcome.apply_error = ""
            applied_at = int(self._now())
        self._store.set_apply_status(
            guild_id,
            client_id=client_id,
            status=outcome.status,
            applied_at=applied_at,
            apply_error=outcome.apply_error,
            applied_version=outcome.applied_version,
        )

    @staticmethod
    def _http_error_type() -> type[BaseException]:
        """Return ``discord.HTTPException`` (lazy), or an inert sentinel.

        Keeping the import lazy lets the module load in a discord-less test env;
        tests inject a fake client whose ``edit`` raises their own error.
        """
        try:
            import discord

            return discord.HTTPException
        except Exception:  # noqa: BLE001 - discord not installed (tests)
            return _NeverRaisedError


class _NeverRaisedError(Exception):
    """Placeholder used when ``discord`` is unavailable so ``except`` is inert."""


async def apply_instance_identities(
    applier: Any | None,
    instances: Any,
    claimed_guild_by_index: dict[int, str],
) -> None:
    """Apply each connected instance's per-bot identity (name + avatar).

    For every instance that connected (not ``unhealthy``), reads its persisted
    ``BOTIDENTITY#<client_id>`` identity for the guild whose claim authorized it
    and applies name/avatar to that application's Discord user via ``applier``.
    Skips instances that did not connect or have no claiming guild. Never raises:
    each instance is isolated so one bot's failure can't stop the others, and a
    ``None`` applier is a no-op (per-bot identity disabled / degraded).

    Kept out of :mod:`instance_runtime` so that file stays under the 500-line
    ceiling; it operates only on the passed-in instances + index→guild map.
    """
    if applier is None:
        return
    for inst in instances:
        if getattr(inst, "status", None) == "unhealthy":
            continue
        guild_id = claimed_guild_by_index.get(inst.index)
        if guild_id is None:
            continue
        try:
            await applier.apply_instance(
                inst.client, guild_id, str(inst.application_id)
            )
        except Exception as exc:  # noqa: BLE001 - never crash on one bot
            _LOG.warning(
                "instance identity: apply failed for instance %d (%s): %s",
                inst.index,
                getattr(inst, "display_name", ""),
                exc,
            )
