"""Bot-side applier for per-guild bot identity (nickname + server avatar).

The web-ui (a separate Flask process) cannot call Discord at request time, so it
PERSISTS a guild's desired bot identity to the ``hellodj-core`` DynamoDB table
(``PK=GUILD#<gid>`` / ``SK=BOTIDENTITY``, entityType ``GuildBotIdentity``) and
uploads any avatar bytes to S3 (see ``platform/components/web-ui/bot_identity.py``).
This module is the OTHER half of that cross-process handoff: running inside the
discord-bot-core component, it reads pending ``BOTIDENTITY`` items, diffs each
against what has already been applied, and applies only the changes — the server
nickname via ``guild.me.edit(nick=...)`` and the per-guild server avatar via a
raw Discord REST ``PATCH /guilds/{guild_id}/members/@me`` (discord.py exposes no
stable public method for the bot's OWN per-guild member avatar). On success it
writes ``apply_status="applied"`` + ``applied_at`` back to the item and clears
``apply_error``; on ``discord.Forbidden`` (bot lacking the Manage-Nicknames /
guild permission) it records ``apply_status="error"`` + a human-readable
``apply_error`` so the UI surfaces a clear error.

Design for testability (fakes-friendly):

* **No hard ``discord`` / ``boto3`` import at module load.** ``discord`` is only
  imported lazily inside the methods that touch it (so this module stays
  importable in a test env where the full Discord stack is not installed), and
  the pure diff + status-transition logic lives in plain functions with NO such
  imports at all.
* **Every side-effecting dependency is injected**: the DynamoDB-backed identity
  store (:class:`IdentityStore` protocol), the S3 client (:class:`S3Reader`
  protocol), the Discord client/guild lookup, and the raw-REST route seam.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "BOTIDENTITY_SK",
    "ApplyOutcome",
    "DesiredIdentity",
    "IdentityApplier",
    "IdentityStore",
    "S3Reader",
    "avatar_content_type",
    "avatar_data_uri",
    "plan_apply",
]

log = logging.getLogger(__name__)

#: Sort key for a guild's desired bot-identity item (mirrors the web-ui writer).
BOTIDENTITY_SK = "BOTIDENTITY"

#: apply_status values written back to the item (read by the web-ui UI tab).
STATUS_APPLIED = "applied"
STATUS_ERROR = "error"

#: Map an avatar S3 key's extension to the data-URI content type. Discord's
#: member-avatar PATCH accepts a base64 data URI whose media type must match the
#: image; the web-ui only ever stores png/jpg/gif keys (see BotIdentityService).
_EXT_CONTENT_TYPE = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
}


# -- injected seams (Protocols) ---------------------------------------------


class IdentityStore(Protocol):
    """The per-guild identity item store (DynamoDB ``hellodj-core`` backed).

    Only the operations the applier needs: read one guild's desired identity
    ``data`` mapping, and write back the applier's status fields. Both sides
    (web-ui writer, this reader) address ``PK=GUILD#<gid>`` / ``SK=BOTIDENTITY``.
    """

    def get_identity_data(self, guild_id: str) -> dict[str, Any] | None:
        """Return the guild's ``BOTIDENTITY`` ``data`` mapping, or ``None``."""
        ...

    def set_apply_status(
        self,
        guild_id: str,
        *,
        status: str,
        applied_at: int,
        apply_error: str,
        applied_version: str,
    ) -> None:
        """Write the applier's status fields back onto the guild's item."""
        ...


class S3Reader(Protocol):
    """Subset of the boto3 ``s3`` client used to read avatar bytes (read-only)."""

    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...


# -- pure value objects + diff/status logic (no discord/boto imports) -------


@dataclass(frozen=True)
class DesiredIdentity:
    """The desired per-guild identity + applier bookkeeping, parsed from ``data``.

    ``applied_version`` is the marker the applier last successfully applied; it
    combines the applied nickname and the applied avatar key so the diff only
    fires on a genuine change (idempotent polling).
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
    def from_data(cls, data: dict[str, Any] | None) -> DesiredIdentity:
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
        """A stable marker of the CURRENT desired identity (nick + avatar).

        The applier compares this against ``applied_version``; equal means the
        current desired state was already applied and nothing needs doing.
        """
        return f"nick={self.nickname}\x1favatar={self.avatar_version}"


@dataclass
class ApplyOutcome:
    """The result of planning/applying one guild's identity change."""

    changed: bool = False
    apply_nickname: bool = False
    apply_avatar: bool = False
    status: str = ""
    apply_error: str = ""
    applied_version: str = ""
    errors: list[str] = field(default_factory=list)


def plan_apply(desired: DesiredIdentity) -> ApplyOutcome:
    """Decide what (if anything) to apply for a guild — PURE, no side effects.

    Only fires when the desired version differs from the last applied version
    (change-only application, so a periodic poll is idempotent). When it does
    fire, both a nickname and an avatar are applied when present in the desired
    state; the avatar is only applied when ``avatar_present`` and a key exist.
    """
    if desired.desired_version() == desired.applied_version:
        return ApplyOutcome(changed=False)
    return ApplyOutcome(
        changed=True,
        apply_nickname=bool(desired.nickname),
        apply_avatar=bool(desired.avatar_present and desired.avatar_key),
        applied_version=desired.desired_version(),
    )


def avatar_content_type(avatar_key: str) -> str:
    """Return the image media type for a stored avatar S3 key (by extension)."""
    ext = avatar_key.rsplit(".", 1)[-1].lower() if "." in avatar_key else ""
    return _EXT_CONTENT_TYPE.get(ext, "image/png")


def avatar_data_uri(data: bytes, avatar_key: str) -> str:
    """Build the ``data:image/<t>;base64,<...>`` URI Discord's PATCH expects."""
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{avatar_content_type(avatar_key)};base64,{b64}"


# -- the applier (side-effecting; discord imported lazily) ------------------


class IdentityApplier:
    """Apply persisted per-guild identity changes to Discord (change-only).

    Args:
        bot: The discord.py client (needs ``get_guild(id)`` and, for the avatar,
            ``http.request``). Injected so tests can pass a fake.
        store: The :class:`IdentityStore` reading desired identity + writing
            status.
        s3_client: A :class:`S3Reader` for the avatar bytes.
        avatar_bucket: The stage-scoped S3 bucket the web-ui uploaded avatar
            bytes to.
        route_factory: Callable building the raw REST route object for the member
            PATCH — defaults to ``discord.http.Route`` (imported lazily).
            Injected so tests can supply a fake without the discord stack.
        time_fn: Injectable epoch-seconds clock (defaults to ``time.time``).
    """

    def __init__(
        self,
        bot: Any,
        store: IdentityStore,
        s3_client: S3Reader,
        *,
        avatar_bucket: str,
        route_factory: Any | None = None,
        time_fn: Any = time.time,
    ) -> None:
        self._bot = bot
        self._store = store
        self._s3 = s3_client
        self._bucket = avatar_bucket
        self._route_factory = route_factory
        self._now = time_fn

    # -- public entry points ------------------------------------------------

    async def apply_guild(self, guild_id: str | int) -> ApplyOutcome:
        """Read + diff + apply one guild's desired identity, writing status back.

        Returns the :class:`ApplyOutcome`. Never raises for the expected
        permission-denied case — a ``discord.Forbidden`` is caught and recorded
        as ``apply_status="error"`` + a human-readable ``apply_error``.
        """
        gid = str(guild_id)
        data = self._store.get_identity_data(gid)
        desired = DesiredIdentity.from_data(data)
        outcome = plan_apply(desired)
        if not outcome.changed:
            return outcome

        guild = self._bot.get_guild(int(gid))
        if guild is None:
            # The bot is not in this guild (or cache not ready). Leave the item
            # pending so a later poll / on_guild_join retries; do not error.
            log.debug("identity-apply: guild %s not in cache, skipping", gid)
            outcome.changed = False
            return outcome

        forbidden = self._forbidden_type()

        if outcome.apply_nickname:
            try:
                await self._apply_nickname(guild, desired.nickname)
            except forbidden as exc:  # bot lacks Manage/Change Nickname
                outcome.errors.append(
                    f"Cannot set nickname: the bot lacks permission ({exc})."
                )
            except Exception as exc:  # noqa: BLE001 - surface a clear message
                outcome.errors.append(f"Failed to set nickname: {exc}.")

        if outcome.apply_avatar:
            try:
                await self._apply_avatar(gid, desired.avatar_key)
            except forbidden as exc:  # bot lacks the guild permission
                outcome.errors.append(
                    f"Cannot set avatar: the bot lacks permission ({exc})."
                )
            except Exception as exc:  # noqa: BLE001 - surface a clear message
                outcome.errors.append(f"Failed to set avatar: {exc}.")

        self._write_back(gid, outcome)
        return outcome

    async def apply_all_pending(self) -> dict[str, ApplyOutcome]:
        """Apply every guild the bot is in that has a pending identity change.

        Iterates the bot's own guild cache (the applier only acts on guilds the
        bot is a member of) and applies each. Used by the periodic watchdog poll
        and by ``on_ready``.
        """
        results: dict[str, ApplyOutcome] = {}
        for guild in list(getattr(self._bot, "guilds", []) or []):
            gid = str(getattr(guild, "id", ""))
            if not gid:
                continue
            try:
                results[gid] = await self.apply_guild(gid)
            except Exception:
                log.exception("identity-apply: error applying guild %s", gid)
        return results

    # -- side-effecting helpers ---------------------------------------------

    async def _apply_nickname(self, guild: Any, nickname: str) -> None:
        """Set the bot's server nickname via discord.py."""
        await guild.me.edit(nick=nickname)
        log.info("identity-apply: set nickname for guild %s", guild.id)

    async def _apply_avatar(self, guild_id: str, avatar_key: str) -> None:
        """Set the bot's per-guild server avatar via a raw REST PATCH.

        discord.py has no stable public method for the bot's OWN per-guild member
        avatar, so this issues the documented endpoint directly on the bot's
        already-authenticated HTTP session:
        ``PATCH /guilds/{guild_id}/members/@me`` with a base64 data-URI body.
        """
        raw = self._s3.get_object(Bucket=self._bucket, Key=avatar_key)
        body = raw["Body"]
        data = body.read() if hasattr(body, "read") else bytes(body)
        payload = {"avatar": avatar_data_uri(data, avatar_key)}
        route = self._make_route(guild_id)
        await self._bot.http.request(route, json=payload)
        log.info("identity-apply: set per-guild avatar for guild %s", guild_id)

    # -- status write-back --------------------------------------------------

    def _write_back(self, guild_id: str, outcome: ApplyOutcome) -> None:
        """Record applied/error status back on the item for the UI to read.

        On any per-field error the whole change is marked ``error`` with a
        combined human-readable message and the applied version is NOT advanced,
        so a corrected retry (or a permission fix) re-applies. On full success
        the version advances so the change is not re-applied.
        """
        if outcome.errors:
            outcome.status = STATUS_ERROR
            outcome.apply_error = " ".join(outcome.errors)
            outcome.applied_version = ""  # do not advance -> retry next poll
            applied_at = 0
        else:
            outcome.status = STATUS_APPLIED
            outcome.apply_error = ""
            applied_at = int(self._now())
        self._store.set_apply_status(
            guild_id,
            status=outcome.status,
            applied_at=applied_at,
            apply_error=outcome.apply_error,
            applied_version=outcome.applied_version,
        )

    # -- discord seam helpers (lazy import) ---------------------------------

    def _make_route(self, guild_id: str) -> Any:
        """Build the raw REST route for the member-avatar PATCH.

        Uses the injected ``route_factory`` when provided (tests), else
        ``discord.http.Route`` lazily so this module imports without discord.
        """
        if self._route_factory is not None:
            return self._route_factory(
                "PATCH", "/guilds/{guild_id}/members/@me", guild_id=guild_id
            )
        import discord.http as _http

        return _http.Route(
            "PATCH", "/guilds/{guild_id}/members/@me", guild_id=guild_id
        )

    @staticmethod
    def _forbidden_type() -> type[BaseException]:
        """Return ``discord.Forbidden`` (lazy), or a sentinel if unavailable.

        Keeping the import lazy lets the module load in a discord-less test env;
        tests that exercise the permission-denied path inject a fake that raises
        their own ``Forbidden``.
        """
        try:
            import discord

            return discord.Forbidden
        except Exception:  # noqa: BLE001 - discord not installed (tests)
            return _NeverRaisedError


class _NeverRaisedError(Exception):
    """Placeholder used when ``discord`` is unavailable so ``except`` is inert."""
