"""Per-guild bot identity (nickname + server avatar) capture for the web-ui.

The web-ui (a Flask process) is NOT the Discord bot, so it cannot call Discord
directly at request time. Instead it PERSISTS the desired per-guild identity and
the bot applies it out of band (see ``bot/bot_identity_apply.py``). This module
owns the web-ui side of that cross-process handoff (R2.7, R2.8).

Storage split (metadata vs bytes):

* **DynamoDB ``hellodj-core`` item** — ``PK=GUILD#<gid>`` ``SK=BOTIDENTITY``,
  entityType ``GuildBotIdentity``. Metadata ONLY: the desired nickname, whether
  an avatar is present + its version/key, who requested it, when, and the
  bot-applier's writeback status (``apply_status`` / ``apply_error`` /
  ``applied_at``). No image bytes ever land in DynamoDB.
* **S3 object** — the avatar image bytes are uploaded to ``avatar_key`` in the
  stage-scoped assets bucket (the web-ui can write, the bot can read via IRSA).
  Bytes never touch DynamoDB (item-size limits) or a secret (not a credential).

Upload constraints (enforced here at capture time, R2.8): format in
{PNG, JPG, GIF} and at most 256 KiB.

Callers (``guild_routes``) MUST have verified ``can_manage_guild`` for the guild
first — this module never persists a guild's identity without the route having
gated the caller's ownership (R3.2, mirrors ``GuildSourcesService``).

Requirements: 2.7, 2.8, 2.9, 3.2, 3.3
"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol

from hellodj_platform_logic.data_access import CoreTable

from guild_admin_service import guild_pk

__all__ = [
    "BOTIDENTITY_SK",
    "DEFAULT_BOT_NAME",
    "AVATAR_MAX_BYTES",
    "AVATAR_FORMATS",
    "BotIdentityService",
    "AvatarValidationError",
    "detect_avatar_format",
    "guild_avatar_key",
    "botidentity_sk",
    "default_bot_name",
]

#: Base sort key for a guild's desired bot-identity item. With no ``client_id``
#: this is the legacy per-guild identity (one bot per guild). When a guild runs
#: multiple bots (each a distinct pool application), identity is keyed PER BOT
#: as ``BOTIDENTITY#<client_id>`` so each assigned application has its own
#: nickname + avatar (:func:`botidentity_sk`).
BOTIDENTITY_SK = "BOTIDENTITY"

IDENTITY_ENTITY = "GuildBotIdentity"

#: Default bot display name applied when the owner lacks the ``custom_name``
#: entitlement. The first bot in a guild is ``HelloDJ``; additional bots
#: iterate ``HelloDJ#1``, ``HelloDJ#2``, … by their claim index.
DEFAULT_BOT_NAME = "HelloDJ"


def botidentity_sk(client_id: str = "") -> str:
    """Return the sort key for a bot's identity item.

    Empty ``client_id`` yields the legacy per-guild key (``BOTIDENTITY``);
    a concrete application id yields the per-bot key
    (``BOTIDENTITY#<client_id>``) so each of a guild's bots has its own
    nickname + avatar.
    """
    return BOTIDENTITY_SK if not client_id else f"{BOTIDENTITY_SK}#{client_id}"


def default_bot_name(index: int) -> str:
    """Return the default bot name for a bot at ``index`` in the guild.

    Index 0 → ``HelloDJ``; index N>0 → ``HelloDJ#N``. Used when the guild
    owner lacks the ``custom_name`` entitlement (or hasn't set a nickname).
    """
    return DEFAULT_BOT_NAME if index <= 0 else f"{DEFAULT_BOT_NAME}#{index}"

#: Discord's server-avatar upload ceiling is generous; 256 KiB is a safe cap.
AVATAR_MAX_BYTES = 256 * 1024

#: Accepted upload formats mapped to their canonical (image/*) content types.
AVATAR_FORMATS: dict[str, str] = {
    "PNG": "image/png",
    "JPG": "image/jpeg",
    "GIF": "image/gif",
}

#: File extension per detected format (used to compose the S3 avatar key).
_FORMAT_EXT = {"PNG": "png", "JPG": "jpg", "GIF": "gif"}


class AvatarValidationError(ValueError):
    """Raised when uploaded avatar bytes fail format/size validation (R2.8)."""


class S3Client(Protocol):
    """Subset of the boto3 ``s3`` client the service uses (fakes-friendly)."""

    def put_object(self, **kwargs: Any) -> dict[str, Any]: ...


def detect_avatar_format(data: bytes) -> str | None:
    """Return ``"PNG"`` / ``"JPG"`` / ``"GIF"`` by magic bytes, or ``None``.

    Format is detected from the file signature (not a client-supplied name or
    content-type) so a mislabeled upload cannot smuggle an unsupported format.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if data.startswith(b"\xff\xd8\xff"):
        return "JPG"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "GIF"
    return None


def guild_avatar_key(
    guild_id: str, data: bytes, fmt: str, *, client_id: str = ""
) -> str:
    """Return the S3 object key for a bot's avatar (content-addressed).

    The key embeds a content hash so a re-upload of the same image is stable and
    a new image gets a distinct key (a natural version marker the bot diffs on).
    With a ``client_id`` the key is scoped per bot so a guild's multiple bots
    never share an avatar object.
    """
    digest = hashlib.sha256(data).hexdigest()[:16]
    scope = f"guild/{guild_id}" if not client_id else (
        f"guild/{guild_id}/bot/{client_id}"
    )
    return f"{scope}/bot-avatar/{digest}.{_FORMAT_EXT[fmt]}"


class BotIdentityService:
    """Persist a guild's desired bot identity (metadata → DynamoDB, bytes → S3).

    The ``apply_status`` / ``apply_error`` / ``applied_at`` fields are owned by
    the bot-side applier; the web-ui writes ``pending`` on each new desired
    change and only reads those fields back for status display.
    """

    def __init__(
        self,
        core_table: CoreTable,
        s3_client: S3Client,
        *,
        stage: str,
        avatar_bucket: str,
    ) -> None:
        self._core = core_table
        self._s3 = s3_client
        self._stage = stage
        self._bucket = avatar_bucket

    # -- reads --------------------------------------------------------------

    def get_identity(
        self, guild_id: str, *, client_id: str = ""
    ) -> dict[str, Any]:
        """Return a bot's desired identity + apply status (metadata only).

        With no ``client_id`` this reads the legacy per-guild identity; with a
        concrete application id it reads that specific bot's identity. Returns
        an empty-but-shaped mapping when no identity has been set yet so the
        template can render "not set" without special-casing ``None``.
        """
        item = self._core.get(guild_pk(guild_id), botidentity_sk(client_id))
        data = item.get("data", {}) if item is not None else {}
        return {
            "nickname": data.get("nickname", ""),
            "avatar_present": bool(data.get("avatar_present", False)),
            "avatar_key": data.get("avatar_key", ""),
            "avatar_version": data.get("avatar_version", ""),
            "requested_by": data.get("requested_by", ""),
            "desired_at": data.get("desired_at", 0),
            "applied_at": data.get("applied_at", 0),
            "apply_status": data.get("apply_status", "none"),
            "apply_error": data.get("apply_error", ""),
        }

    # -- writes -------------------------------------------------------------

    def set_nickname(
        self, guild_id: str, nickname: str, *, requested_by: str,
        client_id: str = "",
    ) -> None:
        """Persist a bot's desired server nickname and mark it pending (R2.7).

        The caller MUST have verified ``can_manage_guild`` AND (for a custom
        name) the owner's ``custom_name`` entitlement first (R3.2). Marks
        ``apply_status="pending"`` so the bot-side applier picks the change up.
        """
        self._upsert(
            guild_id,
            requested_by=requested_by,
            client_id=client_id,
            changes=lambda d: {**d, "nickname": nickname},
        )

    def set_avatar(
        self, guild_id: str, data: bytes, *, requested_by: str,
        client_id: str = "",
    ) -> str:
        """Validate + upload avatar bytes to S3, record metadata (R2.8).

        Enforces format in {PNG, JPG, GIF} and the 256 KiB ceiling BEFORE any
        write, uploads the bytes to the stage-scoped bucket at a content-hashed
        key (bytes never touch DynamoDB), then records only the key/version in
        the ``BOTIDENTITY`` item and marks it pending. Returns the ``avatar_key``.

        Raises:
            AvatarValidationError: if the bytes are empty, too large, or not one
                of the accepted formats.
        """
        if not data:
            raise AvatarValidationError("empty avatar upload")
        if len(data) > AVATAR_MAX_BYTES:
            raise AvatarValidationError(
                f"avatar too large: {len(data)} bytes > {AVATAR_MAX_BYTES}"
            )
        fmt = detect_avatar_format(data)
        if fmt is None:
            raise AvatarValidationError(
                "unsupported avatar format (allowed: PNG, JPG, GIF)"
            )
        key = guild_avatar_key(guild_id, data, fmt, client_id=client_id)
        self._s3.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=AVATAR_FORMATS[fmt],
        )
        version = key.rsplit("/", 1)[1].split(".", 1)[0]
        self._upsert(
            guild_id,
            requested_by=requested_by,
            client_id=client_id,
            changes=lambda d: {
                **d,
                "avatar_present": True,
                "avatar_key": key,
                "avatar_version": version,
            },
        )
        return key

    # -- internal -----------------------------------------------------------

    def _upsert(
        self,
        guild_id: str,
        *,
        requested_by: str,
        changes: Any,
        client_id: str = "",
    ) -> None:
        """Read-modify-write the ``BOTIDENTITY`` item, marking it pending.

        Every desired change bumps ``desired_at`` and resets the applier's
        writeback fields to pending so the bot re-applies and re-reports.
        """
        now = _now_seconds()

        def mutate(current: dict[str, Any]) -> dict[str, Any]:
            merged = changes(dict(current))
            merged.update(
                requested_by=requested_by,
                desired_at=now,
                apply_status="pending",
                apply_error="",
            )
            merged.setdefault("nickname", current.get("nickname", ""))
            merged.setdefault(
                "avatar_present", current.get("avatar_present", False)
            )
            merged.setdefault("applied_at", current.get("applied_at", 0))
            return merged

        self._core.update_with_lock(
            guild_pk(guild_id),
            botidentity_sk(client_id),
            mutate,
            entity_type=IDENTITY_ENTITY,
        )


def _now_seconds() -> int:
    """Return the current epoch seconds (module seam for tests)."""
    import time

    return int(time.time())
