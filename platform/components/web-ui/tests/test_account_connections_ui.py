"""Task 6 UI tests: per-user account connections panel + Discord control.

Feature: unified-oauth-and-token-watchdog.

Exercises the account/config connections surface end-to-end through the Flask
test client against a REAL :class:`SourceCredentialService` over an in-memory
``CoreTable`` fake + an envelope ``FakeKms`` (real AES-GCM) and an in-memory
``UserProfileService`` stand-in — no live AWS / Discord.

Asserts the task's acceptance criteria:

* The connections partial renders each provider's plaintext status but NEVER a
  token value (R8.1, R8.3).
* A provider whose OAuth client id is NOT configured renders a disabled
  "Needs setup" control (never an active Connect link) (R1.2).
* SoundCloud gets NO OAuth control (search-only, R1.7).
* Disconnect calls :meth:`SourceCredentialService.disconnect` and returns the
  updated partial with the credential deleted (R8.2).
* The Discord control shows linked/not-linked and the enable + reset (unlink)
  actions are present and wired; reset unlinks and re-renders (R8.4).

Validates: Requirements 1.1, 1.2, 1.7, 8.1, 8.2, 8.3, 8.4
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from hellodj_platform_logic.data_access import CoreTable
from hellodj_platform_logic.source_refresh import TokenState

from app import create_app
from source_credential_service import (
    SourceCredentialService,
    sourcecred_sk,
    user_pk,
)

STAGE = "beta"
_SUB = "acct-sub-7"

# A recognizable "token" so the no-leak assertion is unambiguous.
_SECRET_REFRESH = "1//0g-ACCOUNT-REFRESH-secret"
_SECRET_ACCESS = "BQC-ACCOUNT-ACCESS-secret"


# ── Fake KMS (envelope semantics) + CoreTable-backing table ────────────────

_WRAP_PREFIX = b"wrapped::"


@dataclass
class FakeKms:
    """Deterministic in-process KMS modeling envelope wrap/unwrap (no AWS)."""

    key_id: str = "arn:aws:kms:us-east-1:000000000000:key/source-creds"

    def generate_data_key(self, **kwargs: Any) -> dict[str, Any]:
        plaintext = os.urandom(32)
        return {
            "Plaintext": plaintext,
            "CiphertextBlob": _WRAP_PREFIX + plaintext,
            "KeyId": kwargs.get("KeyId", self.key_id),
        }

    def decrypt(self, **kwargs: Any) -> dict[str, Any]:
        blob = kwargs["CiphertextBlob"]
        return {"Plaintext": blob[len(_WRAP_PREFIX):]}


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


@dataclass
class _FakeTable:
    """In-memory ``TableLike`` with the create/version condition guards."""

    _items: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        item = self._items.get((kwargs["Key"]["PK"], kwargs["Key"]["SK"]))
        return {"Item": dict(item)} if item is not None else {}

    def query(self, **kwargs: Any) -> dict[str, Any]:
        values = kwargs.get("ExpressionAttributeValues", {})
        pk = values[":pk"]
        prefix = values.get(":skp")
        items = [
            dict(it)
            for (ipk, isk), it in self._items.items()
            if ipk == pk and (prefix is None or isk.startswith(prefix))
        ]
        return {"Items": items}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = kwargs["Item"]
        key = (item["PK"], item["SK"])
        condition = kwargs.get("ConditionExpression")
        existing = self._items.get(key)
        if condition == "attribute_not_exists(PK)" and existing is not None:
            raise _ClientError("ConditionalCheckFailedException")
        if condition == "attribute_not_exists(version)" and existing is not None:
            raise _ClientError("ConditionalCheckFailedException")
        if condition == "version = :expected":
            expected = kwargs["ExpressionAttributeValues"][":expected"]
            if existing is None or existing.get("version") != expected:
                raise _ClientError("ConditionalCheckFailedException")
        self._items[key] = dict(item)
        return {}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        self._items.pop((kwargs["Key"]["PK"], kwargs["Key"]["SK"]), None)
        return {}


class _FakeProfiles:
    """In-memory ``UserProfileService`` stand-in with link/unlink + reverse idx."""

    def __init__(self) -> None:
        self._profiles: dict[str, dict[str, Any]] = {}
        self._by_discord: dict[str, str] = {}

    def get(self, sub: str) -> dict[str, Any]:
        return dict(self._profiles.get(sub, {}))

    def user_for_discord(self, discord_id: str) -> str | None:
        return self._by_discord.get(discord_id)

    def link_discord(self, sub: str, discord_id: str) -> None:
        self._profiles.setdefault(sub, {})
        self._profiles[sub].update(
            {"discord_id": discord_id, "discord_linked": True}
        )
        self._by_discord[discord_id] = sub

    def unlink_discord(self, sub: str) -> None:
        current = self._profiles.get(sub, {})
        discord_id = current.pop("discord_id", None)
        current["discord_linked"] = False
        self._profiles[sub] = current
        if discord_id is not None:
            self._by_discord.pop(discord_id, None)


@dataclass
class _Ctx:
    app: Any
    core: CoreTable
    table: _FakeTable
    creds: SourceCredentialService
    profiles: _FakeProfiles


def _make_ctx(*, configured: bool = True) -> _Ctx:
    """Build a degraded-mode app wired with the unified store + fake profiles.

    When ``configured`` is True the Spotify/Tidal/Google client ids are set so
    those providers are "configured"; the toggles let a test render both an
    active Connect and a disabled "Needs setup" control.
    """
    table = _FakeTable()
    core = CoreTable(table)
    kms = FakeKms()
    creds = SourceCredentialService(core, kms, kms.key_id)
    profiles = _FakeProfiles()
    overrides: dict[str, Any] = {
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "HELLODJ_STAGE": STAGE,
        "PUBLIC_BASE_URL": "https://beta.example.test",
    }
    if configured:
        overrides.update(
            {
                "GOOGLE_CLIENT_ID": "google-client-abc",
                "SPOTIFY_CLIENT_ID": "spotify-client-abc",
                "TIDAL_CLIENT_ID": "tidal-client-abc",
            }
        )
    app = create_app(overrides=overrides)
    app.extensions["source_credentials"] = creds
    app.extensions["user_profiles"] = profiles
    return _Ctx(app=app, core=core, table=table, creds=creds, profiles=profiles)


def _client(app: Any) -> Any:
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = {"is_admin": False, "sub": _SUB}
    return client


def _seed_spotify(ctx: _Ctx) -> None:
    """Store a real encrypted Spotify credential for the account user."""
    ctx.creds.store(
        _SUB,
        "spotify",
        TokenState(
            access_token=_SECRET_ACCESS,
            refresh_token=_SECRET_REFRESH,
            expires_at=9_999_999_999.0,
            scope="streaming",
        ),
        connected_by=_SUB,
    )


# ── R8.1 / R8.3: status partial renders status but NO token value ──────────


def test_account_page_renders_status_without_token():
    ctx = _make_ctx()
    _seed_spotify(ctx)
    client = _client(ctx.app)

    resp = client.get("/account")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    # Status is shown for the connected provider.
    assert "Connected" in body
    # No token material EVER reaches the rendered HTML (R8.3).
    assert _SECRET_REFRESH not in body
    assert _SECRET_ACCESS not in body
    assert "enc_blob" not in body
    # The account-admin (co-admin by Discord id) section renders, with its
    # empty state when no admins are appointed (degraded mode: no service).
    assert "Account administrators" in body
    assert "No account administrators appointed." in body


# ── R1.7: SoundCloud gets NO OAuth control ─────────────────────────────────


def test_account_page_has_no_soundcloud_control():
    ctx = _make_ctx()
    client = _client(ctx.app)

    body = client.get("/account").get_data(as_text=True)

    assert "soundcloud" not in body.lower()
    # The four OAuth providers ARE present.
    assert "Spotify" in body
    assert "Tidal" in body
    assert "Youtube" in body


# ── R1.2: unconfigured provider renders a disabled "Needs setup" control ───


def test_unconfigured_provider_renders_needs_setup_disabled():
    ctx = _make_ctx(configured=False)  # no client ids at all
    client = _client(ctx.app)

    body = client.get("/account").get_data(as_text=True)

    # Spotify / Tidal have no client id configured, so they render the disabled
    # "Needs setup" control (never an active Connect link) (R1.2).
    assert "Needs setup" in body
    assert "disabled" in body
    # YouTube / YouTube Music authenticate via the youtube-source plugin's
    # PUBLIC device-code client (no operator Google app), so they ALWAYS offer
    # an active Connect control even with no client ids configured. Its connect
    # is HTMX-driven (device flow) rather than a plain redirect link.
    assert ">Connect<" in body
    assert "youtube/device/poll" not in body  # the code panel only renders after Connect


def test_configured_provider_renders_active_connect():
    ctx = _make_ctx(configured=True)
    client = _client(ctx.app)

    body = client.get("/account").get_data(as_text=True)

    # A configured but not-yet-connected provider offers an active Connect.
    assert ">Connect<" in body


# ── R8.2: Disconnect calls the service + returns updated partial ───────────


def test_disconnect_deletes_credential_and_returns_partial():
    ctx = _make_ctx()
    _seed_spotify(ctx)
    # Sanity: the credential exists before disconnect.
    assert ctx.core.get(user_pk(_SUB), sourcecred_sk("spotify")) is not None
    client = _client(ctx.app)

    resp = client.post(
        "/account/sources/spotify/disconnect",
        headers={"HX-Request": "true"},
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    # The credential item is deleted (only that provider).
    assert ctx.core.get(user_pk(_SUB), sourcecred_sk("spotify")) is None
    # The returned partial reflects the change: Spotify now Not connected.
    assert "Not connected" in body
    # No token leaks in the partial and it is a fragment (no full shell).
    assert _SECRET_REFRESH not in body
    assert "<html" not in body.lower()


def test_disconnect_only_targets_calling_user():
    ctx = _make_ctx()
    _seed_spotify(ctx)
    # Another user's credential must be untouched by our disconnect.
    ctx.creds.store(
        "other-sub",
        "spotify",
        TokenState(
            access_token="x", refresh_token="y", expires_at=9_999_999_999.0
        ),
        connected_by="other-sub",
    )
    client = _client(ctx.app)

    client.post("/account/sources/spotify/disconnect")

    assert ctx.core.get(user_pk(_SUB), sourcecred_sk("spotify")) is None
    assert ctx.core.get(user_pk("other-sub"), sourcecred_sk("spotify")) is not None


# ── R8.4: Discord control shows linked/not-linked + enable + reset ─────────


def test_discord_control_not_linked_shows_enable():
    ctx = _make_ctx()
    client = _client(ctx.app)

    body = client.get("/account").get_data(as_text=True)

    assert "Enable Discord link" in body
    # The enable action is wired to the existing link flow.
    assert "/auth/discord/link" in body


def test_discord_control_linked_shows_reset():
    ctx = _make_ctx()
    ctx.profiles.link_discord(_SUB, "disc-123")
    client = _client(ctx.app)

    body = client.get("/account").get_data(as_text=True)

    assert "disc-123" in body
    assert "Reset link" in body
    # Reset is wired to the account unlink route.
    assert "/account/discord/reset" in body


def test_discord_reset_unlinks_and_returns_partial():
    ctx = _make_ctx()
    ctx.profiles.link_discord(_SUB, "disc-123")
    assert ctx.profiles.user_for_discord("disc-123") == _SUB
    client = _client(ctx.app)

    resp = client.post(
        "/account/discord/reset", headers={"HX-Request": "true"}
    )
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    # The link is cleared (reverse index gone) and the partial reflects it.
    assert ctx.profiles.user_for_discord("disc-123") is None
    assert ctx.profiles.get(_SUB).get("discord_linked") is False
    assert "Enable Discord link" in body
    assert "<html" not in body.lower()
