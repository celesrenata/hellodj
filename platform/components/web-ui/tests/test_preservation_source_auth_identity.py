"""Preservation tests (web-ui) — bot-identity-and-source-auth.

Task 2 of the ``bot-identity-and-source-auth`` bugfix spec (web-ui side).
Observation-first: these capture behavior OBSERVED on the CURRENT (unfixed)
code as a baseline (Property 2: Preservation). They MUST PASS on unfixed code
and MUST STILL PASS after the fix (no regressions).

Covers:

* **Tidal connect preserved (3.1):** with ``TIDAL_CLIENT_ID`` set, ``connect``
  redirects to the Tidal authorize URL, and ``disconnect`` removes the secret
  at ``hellodj/<stage>/guild/<gid>/tidal``.
* **Ownership gating preserved (3.2):** a PROPERTY test over arbitrary callers
  (managers vs non-managers) asserts every per-guild source connect/disconnect
  route rejects callers failing ``can_manage_guild`` / ``_can_manage``.
* **Secret isolation + tokens-out-of-DynamoDB preserved (3.3):**
  ``GuildSourcesService.store_tokens`` writes tokens ONLY to the per-guild
  secret; the DynamoDB ``SOURCE#<provider>`` item holds only non-secret
  metadata.
* **Disconnect preserved (3.4):** disconnect deletes BOTH the guild secret and
  the ``SOURCE#<provider>`` metadata item.

Uses the same in-memory ``_FakeTable`` / ``_FakeSecrets`` fakes as
``test_guild_sources_isolation.py`` and the ``conftest`` path setup. No live
AWS / Discord.

Validates: Requirements 3.1, 3.2, 3.3, 3.4
"""

from __future__ import annotations

import json
from typing import Any

from hellodj_platform_logic.data_access import CoreTable
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app import create_app
from guild_admin_service import guild_pk
from guild_sources import GuildSourcesService, guild_source_secret_name, source_sk

STAGE = "beta"

_ID = st.integers(min_value=1, max_value=10**18).map(str)


# ── In-memory fakes (mirror test_guild_sources_isolation.py) ───────────────


class _ClientError(Exception):
    """Minimal botocore-shaped client error for the fake table/secrets."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _FakeTable:
    """In-memory ``TableLike`` supporting PK access + base-PK prefix query."""

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
            if key[0] == pk and (prefix is None or str(key[1]).startswith(prefix))
        ]
        return {"Items": items}

    def delete_item(self, **kwargs: Any) -> dict[str, Any]:
        self._items.pop((kwargs["Key"]["PK"], kwargs["Key"]["SK"]), None)
        return {}


class _FakeSecrets:
    """In-memory Secrets Manager client keyed by secret name."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def create_secret(self, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["Name"]
        if name in self.store:
            raise _ClientError("ResourceExistsException")
        self.store[name] = kwargs["SecretString"]
        return {"Name": name}

    def put_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        self.store[kwargs["SecretId"]] = kwargs["SecretString"]
        return {"SecretId": kwargs["SecretId"]}

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["SecretId"]
        if name not in self.store:
            raise _ClientError("ResourceNotFoundException")
        return {"SecretString": self.store[name]}

    def delete_secret(self, **kwargs: Any) -> dict[str, Any]:
        self.store.pop(kwargs["SecretId"], None)
        return {}


def _service() -> tuple[GuildSourcesService, CoreTable, _FakeSecrets]:
    table = _FakeTable()
    core = CoreTable(table)
    secrets = _FakeSecrets()
    return GuildSourcesService(core, secrets, stage=STAGE), core, secrets


def _make_app(sources: GuildSourcesService | None) -> Any:
    """A degraded-mode app with a real ``guild_sources`` service injected.

    ``guild_admin`` stays ``None`` (degraded), so ``can_manage_guild`` sees an
    empty owner/admin set — a super-admin session is the way to pass the gate,
    exactly like ``test_bug_condition_source_auth_identity.py`` does.
    """
    app = create_app(
        overrides={
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "HELLODJ_STAGE": STAGE,
            "PUBLIC_BASE_URL": "https://beta.example.test",
            # Tidal is the provider that is (and stays) wired today.
            "TIDAL_CLIENT_ID": "tidal-client-abc",
        }
    )
    app.extensions["guild_sources"] = sources
    return app


def _admin_client(app: Any) -> Any:
    """A test client whose session is a super-admin (passes the gate)."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = {"is_admin": True, "sub": "admin-sub"}
    return client


# ── 3.1 — Tidal connect/disconnect preserved ──────────────────────────────


class TestTidalConnectPreserved:
    def test_connect_redirects_to_tidal_authorize_url(self):
        """With ``TIDAL_CLIENT_ID`` set, connect redirects to Tidal (3.1)."""
        svc, _core, _secrets = _service()
        app = _make_app(svc)
        client = _admin_client(app)

        resp = client.get("/auth/sources/111/tidal/connect")
        location = resp.headers.get("Location", "")

        assert resp.status_code in (301, 302)
        assert location.startswith("https://login.tidal.com/authorize"), (
            f"tidal connect must redirect to the Tidal authorize URL, got "
            f"{location!r}"
        )
        assert "client_id=tidal-client-abc" in location

    def test_disconnect_removes_tidal_secret_and_metadata(self):
        """Disconnect removes the tidal per-guild secret + metadata (3.1/3.4)."""
        svc, core, secrets = _service()
        # Pre-connect tidal for the guild (as a prior connect would have).
        svc.store_tokens(
            "111", "tidal", {"refresh_token": "tidal-R"}, connected_by="admin-sub"
        )
        name = guild_source_secret_name(STAGE, "111", "tidal")
        assert name in secrets.store

        app = _make_app(svc)
        client = _admin_client(app)
        resp = client.post("/guilds/111/sources/tidal/disconnect")

        assert resp.status_code == 200
        # The isolated secret at hellodj/<stage>/guild/<gid>/tidal is gone.
        assert name not in secrets.store
        assert core.get(guild_pk("111"), source_sk("tidal")) is None


# ── 3.2 — ownership gating preserved (property over arbitrary callers) ──────


class TestOwnershipGatingPreserved:
    """Every per-guild source route rejects callers failing can_manage_guild."""

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(guild_id=_ID, provider=st.sampled_from(("youtube", "tidal", "spotify")))
    def test_connect_rejects_non_manager(self, guild_id: str, provider: str):
        """A non-manager caller is redirected away from the guilds list (3.2).

        With ``guild_admin`` degraded (no owner/admin edges) and a NON-admin
        session, ``_guild_source_authorized`` is false, so connect must NOT
        redirect to a provider authorize URL — it bounces to /guilds.
        """
        svc, _core, _secrets = _service()
        app = _make_app(svc)
        client = app.test_client()
        with client.session_transaction() as sess:
            # A logged-in but non-manager user (not super-admin, no ownership).
            sess["user"] = {"is_admin": False, "sub": "rando-sub"}

        resp = client.get(f"/auth/sources/{guild_id}/{provider}/connect")
        location = resp.headers.get("Location", "")

        assert resp.status_code in (301, 302)
        # Rejected: never bounced to a provider authorize host.
        assert "/guilds" in location
        for host in (
            "accounts.spotify.com",
            "login.tidal.com",
            "accounts.google.com",
        ):
            assert host not in location

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(guild_id=_ID, provider=st.sampled_from(("youtube", "tidal", "spotify")))
    def test_disconnect_rejects_non_manager(self, guild_id: str, provider: str):
        """A non-manager caller cannot disconnect a guild's source (3.2).

        The disconnect route redirects non-managers to /guilds and never
        touches the service.
        """
        svc, core, secrets = _service()
        # Seed a connected provider so we can prove it is NOT removed.
        svc.store_tokens(
            guild_id, provider, {"t": "keep"}, connected_by="real-owner"
        )
        name = guild_source_secret_name(STAGE, guild_id, provider)
        app = _make_app(svc)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["user"] = {"is_admin": False, "sub": "rando-sub"}

        resp = client.post(f"/guilds/{guild_id}/sources/{provider}/disconnect")

        assert resp.status_code in (301, 302)
        assert "/guilds" in resp.headers.get("Location", "")
        # The secret + metadata survive — the gate blocked the mutation.
        assert name in secrets.store
        assert core.get(guild_pk(guild_id), source_sk(provider)) is not None

    def test_connect_requires_login(self):
        """An anonymous caller is redirected to login, not to a provider (3.2)."""
        svc, _core, _secrets = _service()
        app = _make_app(svc)
        client = app.test_client()  # no session

        resp = client.get("/auth/sources/111/tidal/connect")

        assert resp.status_code in (301, 302)
        assert "/login" in resp.headers.get("Location", "")


# ── 3.3 — secret isolation + tokens-out-of-DynamoDB preserved ──────────────


class TestSecretIsolationPreserved:
    @settings(max_examples=100)
    @given(
        gid=_ID,
        provider=st.sampled_from(("youtube", "youtube_music", "tidal", "spotify")),
        access=st.text(min_size=1, max_size=24),
        refresh=st.text(min_size=1, max_size=24),
    )
    def test_store_tokens_isolates_secret_and_keeps_dynamo_metadata_only(
        self, gid: str, provider: str, access: str, refresh: str
    ):
        """Tokens land ONLY in the per-guild secret; DynamoDB stays metadata (3.3).

        Property over arbitrary guilds/providers/token values: the secret at
        ``hellodj/<stage>/guild/<gid>/<provider>`` holds the exact token JSON,
        and the ``SOURCE#<provider>`` DynamoDB item contains no token material.
        Token values are prefixed with a sentinel so the "no leak" check can't
        false-positive on numeric metadata (timestamps/versions) that might
        happen to contain a short generated substring.
        """
        svc, core, secrets = _service()
        access_v = f"SECRET-A-{access}"
        refresh_v = f"SECRET-R-{refresh}"
        tokens = {"access_token": access_v, "refresh_token": refresh_v}

        svc.store_tokens(gid, provider, tokens, connected_by="owner-sub")

        name = guild_source_secret_name(STAGE, gid, provider)
        assert name in secrets.store
        assert json.loads(secrets.store[name]) == tokens

        item = core.get(guild_pk(gid), source_sk(provider))
        assert item is not None
        data = item["data"]
        assert data["connected"] is True
        assert data["connected_by"] == "owner-sub"
        # No token material (keys or the sentinel-prefixed values) anywhere in
        # the DynamoDB item.
        serialized = json.dumps(item)
        assert access_v not in serialized
        assert refresh_v not in serialized
        assert "SECRET-A-" not in serialized
        assert "SECRET-R-" not in serialized
        assert "access_token" not in serialized
        assert "refresh_token" not in serialized


# ── 3.4 — disconnect deletes both secret and metadata ──────────────────────


class TestDisconnectPreserved:
    @settings(max_examples=100)
    @given(
        gid=_ID,
        provider=st.sampled_from(("youtube", "youtube_music", "tidal", "spotify")),
    )
    def test_disconnect_deletes_secret_and_metadata(self, gid: str, provider: str):
        """Disconnect removes BOTH the guild secret and the metadata item (3.4)."""
        svc, core, secrets = _service()
        svc.store_tokens(gid, provider, {"t": "v"}, connected_by="owner")
        name = guild_source_secret_name(STAGE, gid, provider)
        assert name in secrets.store
        assert core.get(guild_pk(gid), source_sk(provider)) is not None

        svc.disconnect(gid, provider)

        assert name not in secrets.store
        assert core.get(guild_pk(gid), source_sk(provider)) is None

    def test_disconnect_is_noop_when_never_connected(self):
        """Disconnecting an unconnected provider is a safe no-op (3.4)."""
        svc, core, secrets = _service()

        svc.disconnect("g1", "spotify")

        assert secrets.store == {}
        assert core.get(guild_pk("g1"), source_sk("spotify")) is None
