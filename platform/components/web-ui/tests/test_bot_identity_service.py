"""Unit tests for the web-ui ``BotIdentityService`` + identity routes.

Task 7.3 (web-ui half) of the ``bot-identity-and-source-auth`` bugfix spec
(Change area F). Verifies the web-ui side of the per-guild bot-identity feature:

* ``BotIdentityService`` persists the ``GUILD#<gid>`` / ``BOTIDENTITY`` DynamoDB
  item (metadata ONLY) and uploads avatar bytes to a FAKE S3 client — image
  bytes never land in DynamoDB (R2.7, R2.8, R3.3).
* Avatar upload validation: format in {PNG, JPG, GIF} + 256 KiB max — oversize
  and wrong-format uploads are rejected before any S3/DynamoDB write (R2.8).
* ``set_nickname`` / ``set_avatar`` mark the item ``pending`` and ``get_identity``
  reads back the applier's ``apply_status`` / ``apply_error`` for the UI (R2.9).
* Every per-guild identity route (``set_bot_nickname`` / ``set_bot_avatar``) is
  gated by ``can_manage_guild`` and rejects non-managers (R3.2).

Uses in-memory fakes for the DynamoDB ``TableLike`` and the boto3 ``s3`` client
(``put_object``) — no AWS. Mirrors the fixture style of
``test_guild_sources_isolation.py`` / ``test_youtube_token_exchange.py``.

Requirements: 2.7, 2.8, 2.9
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from hellodj_platform_logic.data_access import CoreTable
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app import create_app
from bot_identity import (
    AVATAR_MAX_BYTES,
    BOTIDENTITY_SK,
    AvatarValidationError,
    BotIdentityService,
    detect_avatar_format,
    guild_avatar_key,
)
from guild_admin_service import guild_pk

STAGE = "beta"
BUCKET = "hellodj-beta-assets"

# Guild ids are numeric strings; keep them constrained but arbitrary.
_ID = st.integers(min_value=1, max_value=10**18).map(str)

# Minimal valid magic-byte headers per accepted format.
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
_GIF = b"GIF89a" + b"\x00" * 32


# ── In-memory fakes ────────────────────────────────────────────────────────


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeTable:
    """In-memory ``TableLike`` supporting PK/SK access + optimistic-lock puts."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        item = self._items.get(key)
        return {"Item": dict(item)} if item is not None else {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = kwargs["Item"]
        key = (item["PK"], item["SK"])
        condition = kwargs.get("ConditionExpression")
        values = kwargs.get("ExpressionAttributeValues", {})
        existing = self._items.get(key)
        if condition == "attribute_not_exists(PK)" and existing is not None:
            raise _ClientError("ConditionalCheckFailedException")
        if condition == "attribute_not_exists(version)" and existing is not None:
            raise _ClientError("ConditionalCheckFailedException")
        if condition == "version = :expected":
            expected = values[":expected"]
            if existing is None or existing.get("version") != expected:
                raise _ClientError("ConditionalCheckFailedException")
        self._items[key] = dict(item)
        return {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        values = kwargs["ExpressionAttributeValues"]
        pk = values[":pk"]
        prefix = values.get(":skp")
        items = [
            dict(it)
            for key, it in self._items.items()
            if key[0] == pk
            and (prefix is None or str(key[1]).startswith(prefix))
        ]
        return {"Items": items}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        self._items.pop((kwargs["Key"]["PK"], kwargs["Key"]["SK"]), None)
        return {}


class _FakeS3:
    """In-memory boto3 ``s3`` client capturing every ``put_object`` call."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = {
            "Body": kwargs["Body"],
            "ContentType": kwargs.get("ContentType"),
        }
        return {}


def _service() -> tuple[BotIdentityService, CoreTable, _FakeS3]:
    table = _FakeTable()
    core = CoreTable(table)
    s3 = _FakeS3()
    return (
        BotIdentityService(core, s3, stage=STAGE, avatar_bucket=BUCKET),
        core,
        s3,
    )


# ── format detection + key derivation ──────────────────────────────────────


class TestAvatarFormatDetection:
    def test_detects_png_jpg_gif_by_magic_bytes(self):
        assert detect_avatar_format(_PNG) == "PNG"
        assert detect_avatar_format(_JPG) == "JPG"
        assert detect_avatar_format(_GIF) == "GIF"
        assert detect_avatar_format(b"GIF87a" + b"\x00" * 8) == "GIF"

    def test_rejects_unknown_signature(self):
        assert detect_avatar_format(b"%PDF-1.7 not an image") is None
        assert detect_avatar_format(b"") is None

    def test_avatar_key_is_content_addressed_and_prefixed(self):
        key = guild_avatar_key("111", _PNG, "PNG")
        assert key.startswith("guild/111/bot-avatar/")
        assert key.endswith(".png")
        # Same bytes → stable key; different bytes → different key.
        assert guild_avatar_key("111", _PNG, "PNG") == key
        assert guild_avatar_key("111", _PNG + b"x", "PNG") != key


# ── set_avatar: happy path stores bytes in S3, metadata in DynamoDB ─────────


class TestSetAvatar:
    def test_uploads_bytes_to_s3_and_records_metadata_only(self):
        svc, core, s3 = _service()

        key = svc.set_avatar("111", _PNG, requested_by="owner-sub")

        # Bytes went to S3 at the returned key in the stage-scoped bucket.
        assert (BUCKET, key) in s3.objects
        assert s3.objects[(BUCKET, key)]["Body"] == _PNG
        assert s3.objects[(BUCKET, key)]["ContentType"] == "image/png"

        # DynamoDB item holds ONLY metadata (key/version), never image bytes.
        item = core.get(guild_pk("111"), BOTIDENTITY_SK)
        assert item is not None
        data = item["data"]
        assert data["avatar_present"] is True
        assert data["avatar_key"] == key
        assert data["avatar_version"] == key.rsplit("/", 1)[1].split(".", 1)[0]
        assert data["requested_by"] == "owner-sub"
        assert data["apply_status"] == "pending"
        # No image bytes anywhere in the persisted item.
        serialized = json.dumps(item)
        assert "\\u0089PNG" not in serialized
        assert "PNG\\r\\n" not in serialized

    def test_jpg_and_gif_content_types(self):
        svc, _core, s3 = _service()
        jkey = svc.set_avatar("111", _JPG, requested_by="o")
        gkey = svc.set_avatar("222", _GIF, requested_by="o")
        assert s3.objects[(BUCKET, jkey)]["ContentType"] == "image/jpeg"
        assert s3.objects[(BUCKET, gkey)]["ContentType"] == "image/gif"

    def test_rejects_oversize_upload_before_any_write(self):
        svc, core, s3 = _service()
        too_big = _PNG + b"\x00" * (AVATAR_MAX_BYTES + 1)

        with pytest.raises(AvatarValidationError, match="too large"):
            svc.set_avatar("111", too_big, requested_by="o")

        # Nothing written to S3 or DynamoDB.
        assert s3.calls == []
        assert core.get(guild_pk("111"), BOTIDENTITY_SK) is None

    def test_accepts_upload_at_the_size_ceiling(self):
        svc, _core, s3 = _service()
        # Exactly AVATAR_MAX_BYTES (the boundary is inclusive).
        at_limit = _PNG + b"\x00" * (AVATAR_MAX_BYTES - len(_PNG))
        assert len(at_limit) == AVATAR_MAX_BYTES

        key = svc.set_avatar("111", at_limit, requested_by="o")
        assert (BUCKET, key) in s3.objects

    def test_rejects_wrong_format_before_any_write(self):
        svc, core, s3 = _service()

        with pytest.raises(AvatarValidationError, match="unsupported"):
            svc.set_avatar("111", b"%PDF-1.7 not an image", requested_by="o")

        assert s3.calls == []
        assert core.get(guild_pk("111"), BOTIDENTITY_SK) is None

    def test_rejects_empty_upload(self):
        svc, _core, s3 = _service()
        with pytest.raises(AvatarValidationError, match="empty"):
            svc.set_avatar("111", b"", requested_by="o")
        assert s3.calls == []


# ── set_nickname + get_identity ────────────────────────────────────────────


class TestNicknameAndStatus:
    def test_set_nickname_marks_pending_and_reads_back(self):
        svc, _core, _s3 = _service()

        svc.set_nickname("111", "DJ Vinyl", requested_by="owner-sub")

        identity = svc.get_identity("111")
        assert identity["nickname"] == "DJ Vinyl"
        assert identity["requested_by"] == "owner-sub"
        assert identity["apply_status"] == "pending"
        assert identity["apply_error"] == ""
        assert identity["avatar_present"] is False

    def test_get_identity_shape_when_unset(self):
        svc, _core, _s3 = _service()
        identity = svc.get_identity("999")
        # Empty-but-shaped so the template renders "not set" without None.
        assert identity == {
            "nickname": "",
            "avatar_present": False,
            "avatar_key": "",
            "avatar_version": "",
            "requested_by": "",
            "desired_at": 0,
            "applied_at": 0,
            "apply_status": "none",
            "apply_error": "",
        }

    def test_nickname_and_avatar_coexist_on_the_same_item(self):
        svc, _core, _s3 = _service()
        svc.set_nickname("111", "DJ Vinyl", requested_by="o")
        svc.set_avatar("111", _PNG, requested_by="o")

        identity = svc.get_identity("111")
        assert identity["nickname"] == "DJ Vinyl"
        assert identity["avatar_present"] is True
        assert identity["avatar_key"].startswith("guild/111/bot-avatar/")

    def test_get_identity_surfaces_applier_writeback(self):
        """The UI reads apply_status/apply_error the bot applier writes back."""
        svc, core, _s3 = _service()
        svc.set_nickname("111", "DJ", requested_by="o")

        # Simulate the bot-side applier recording an error (R2.9).
        core.update_with_lock(
            guild_pk("111"),
            BOTIDENTITY_SK,
            lambda d: {
                **d,
                "apply_status": "error",
                "apply_error": "Cannot set nickname: the bot lacks permission.",
            },
        )

        identity = svc.get_identity("111")
        assert identity["apply_status"] == "error"
        assert "lacks permission" in identity["apply_error"]


# ── identity routes: ownership gating (R3.2) ───────────────────────────────


def _make_app(svc: BotIdentityService | None) -> Any:
    app = create_app(
        overrides={
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "HELLODJ_STAGE": STAGE,
            "PUBLIC_BASE_URL": "https://beta.example.test",
        }
    )
    # guild_admin stays degraded (None) → empty owner/admin edges, so only a
    # super-admin session passes can_manage_guild.
    app.extensions["guild_identity_service"] = svc
    return app


class TestIdentityRouteGating:
    @settings(
        max_examples=50,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(guild_id=_ID)
    def test_set_nickname_rejects_non_manager(self, guild_id: str):
        """A non-manager cannot persist a nickname — nothing is stored (R3.2)."""
        svc, core, _s3 = _service()
        app = _make_app(svc)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user"] = {"is_admin": False, "sub": "rando-sub"}

        resp = client.post(
            f"/guilds/{guild_id}/identity/nickname",
            data={"nickname": "Hijack"},
        )

        assert resp.status_code in (301, 302)
        assert "/guilds" in resp.headers.get("Location", "")
        # The gate blocked the mutation — no item written.
        assert core.get(guild_pk(guild_id), BOTIDENTITY_SK) is None

    @settings(
        max_examples=50,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(guild_id=_ID)
    def test_set_avatar_rejects_non_manager(self, guild_id: str):
        """A non-manager cannot upload an avatar — no S3/DynamoDB write (R3.2)."""
        svc, core, s3 = _service()
        app = _make_app(svc)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user"] = {"is_admin": False, "sub": "rando-sub"}

        resp = client.post(
            f"/guilds/{guild_id}/identity/avatar",
            data={"avatar": (_png_stream(), "avatar.png")},
            content_type="multipart/form-data",
        )

        assert resp.status_code in (301, 302)
        assert "/guilds" in resp.headers.get("Location", "")
        assert s3.calls == []
        assert core.get(guild_pk(guild_id), BOTIDENTITY_SK) is None

    def test_set_nickname_requires_login(self):
        svc, core, _s3 = _service()
        app = _make_app(svc)
        client = app.test_client()  # no session

        resp = client.post(
            "/guilds/111/identity/nickname", data={"nickname": "x"}
        )

        assert resp.status_code in (301, 302)
        assert "/login" in resp.headers.get("Location", "")
        assert core.get(guild_pk("111"), BOTIDENTITY_SK) is None

    def test_manager_can_set_nickname(self):
        """A super-admin passes the gate and the nickname is persisted (R2.7)."""
        svc, core, _s3 = _service()
        app = _make_app(svc)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user"] = {"is_admin": True, "sub": "admin-sub"}

        resp = client.post(
            "/guilds/111/identity/nickname", data={"nickname": "DJ Vinyl"}
        )

        assert resp.status_code == 200
        item = core.get(guild_pk("111"), BOTIDENTITY_SK)
        assert item is not None
        assert item["data"]["nickname"] == "DJ Vinyl"
        assert item["data"]["apply_status"] == "pending"

    def test_manager_can_set_avatar(self):
        """A super-admin passes the gate and the avatar is uploaded (R2.8)."""
        svc, core, s3 = _service()
        app = _make_app(svc)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user"] = {"is_admin": True, "sub": "admin-sub"}

        resp = client.post(
            "/guilds/111/identity/avatar",
            data={"avatar": (_png_stream(), "avatar.png")},
            content_type="multipart/form-data",
        )

        assert resp.status_code == 200
        item = core.get(guild_pk("111"), BOTIDENTITY_SK)
        assert item is not None
        assert item["data"]["avatar_present"] is True
        assert len(s3.calls) == 1

    def test_manager_oversize_upload_surfaces_error_no_store(self):
        """Oversize upload by a manager → validation error, nothing stored."""
        svc, core, s3 = _service()
        app = _make_app(svc)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user"] = {"is_admin": True, "sub": "admin-sub"}

        import io

        too_big = io.BytesIO(_PNG + b"\x00" * (AVATAR_MAX_BYTES + 1))
        resp = client.post(
            "/guilds/111/identity/avatar",
            data={"avatar": (too_big, "big.png")},
            content_type="multipart/form-data",
        )

        # The route renders the form partial with the upload error (200), and
        # nothing was written to S3 or DynamoDB.
        assert resp.status_code == 200
        assert s3.calls == []
        assert core.get(guild_pk("111"), BOTIDENTITY_SK) is None


def _png_stream() -> Any:
    import io

    return io.BytesIO(_PNG)
