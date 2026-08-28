"""Per-guild source isolation tests (task 12).

Verifies that a guild's music-source OAuth tokens live ONLY in an isolated
Per_Guild_Secret and never leak — neither into DynamoDB nor across guilds
(R5.1-R5.5). Two flavours:

* **Property tests** (hypothesis):
  1. ``guild_source_secret_name`` is *injective* — distinct (guild, provider)
     pairs always produce distinct secret names and never collide across
     different guilds/providers, and every name sits under the
     ``hellodj/<stage>/guild/<gid>/`` prefix the IAM policy scopes to (R5.1).
  2. Authorization never leaks across guilds — for arbitrary guilds ``A != B``
     and a session that controls only ``A`` (its owner, or an appointed admin's
     linked Discord id), ``can_manage_guild(guild_id=B, ...)`` is ``False``
     unless the caller is the super-admin (R5.2).

* **Example unit tests**: tokens land in Secrets Manager (never DynamoDB);
  disconnect deletes the secret + metadata; ``status`` reflects per-provider
  connection state.

Uses in-memory fakes for the DynamoDB ``TableLike`` and the Secrets Manager
client — no AWS.

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5
"""

from __future__ import annotations

import json
from typing import Any

from hellodj_platform_logic.data_access import CoreTable
from hypothesis import given, settings
from hypothesis import strategies as st

from guild_admin_service import admin_sk, can_manage_guild, guild_pk, owner_sk
from guild_sources import (
    SUPPORTED_PROVIDERS,
    GuildSourcesService,
    guild_source_secret_name,
    source_sk,
)

STAGE = "beta"

# Discord/guild ids are numeric strings; keep them constrained but arbitrary.
_ID = st.integers(min_value=1, max_value=10**18).map(str)
_PROVIDER = st.sampled_from(SUPPORTED_PROVIDERS)


class _ClientError(Exception):
    """Minimal botocore-shaped client error for the fake table."""

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
            if key[0] == pk
            and (prefix is None or str(key[1]).startswith(prefix))
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


# -- Property 1: secret name is injective + prefix-scoped (R5.1) ------------ #


@settings(max_examples=200)
@given(
    pairs=st.lists(
        st.tuples(_ID, _PROVIDER), min_size=1, max_size=25, unique=True
    )
)
def test_secret_name_is_unique_per_guild_provider_and_prefix_scoped(
    pairs: list[tuple[str, str]],
) -> None:
    """Distinct (guild, provider) -> distinct name; all under the guild prefix.

    Injectivity proves one guild's secret can never alias another's (R5.1): the
    ``guild/<gid>/<provider>`` path is what isolates tokens, so the map from a
    distinct (guild, provider) to a name must be one-to-one. Every name must
    also live under ``hellodj/<stage>/guild/<gid>/`` — the exact prefix the IAM
    grant scopes to.
    """
    names = [
        guild_source_secret_name(STAGE, gid, provider) for gid, provider in pairs
    ]

    # Injective: no two distinct inputs share a name.
    assert len(set(names)) == len(pairs)

    for (gid, provider), name in zip(pairs, names, strict=True):
        assert name == f"hellodj/{STAGE}/guild/{gid}/{provider}"
        assert name.startswith(f"hellodj/{STAGE}/guild/{gid}/")
        # The provider is the final path segment; the guild segment isolates.
        assert name.rsplit("/", 1)[1] == provider


@settings(max_examples=200)
@given(a=_ID, b=_ID, pa=_PROVIDER, pb=_PROVIDER)
def test_different_guilds_never_collide_on_secret_name(
    a: str, b: str, pa: str, pb: str
) -> None:
    """Two names collide ONLY when both guild AND provider match (R5.1, R5.3).

    A collision across *different* guilds would break isolation, so the only
    permissible equal-name case is the same guild with the same provider.
    """
    same = guild_source_secret_name(STAGE, a, pa) == guild_source_secret_name(
        STAGE, b, pb
    )
    assert same == (a == b and pa == pb)


# -- Property 2: authorization never leaks across guilds (R5.2) ------------- #


@settings(max_examples=200)
@given(
    a=_ID,
    b=_ID,
    owner_a=st.text(min_size=1, max_size=12),
    admin_disc=_ID,
)
def test_guild_a_session_cannot_manage_guild_b(
    a: str, b: str, owner_a: str, admin_disc: str
) -> None:
    """A session controlling only guild A is denied guild B (R5.2).

    Models "reading guild B's source with guild A's session": the caller is
    guild A's owner and (separately) an appointed admin of A via ``admin_disc``.
    Presented against guild B — whose owner/admin facts are empty — the pure
    gate must refuse, so no cross-guild secret read/write is possible. The one
    exception is the super-admin, asserted separately below.
    """
    from hypothesis import assume

    assume(a != b)

    # The session's facts are guild A's; guild B has a distinct owner and no
    # admin edges for this caller.
    granted_b = can_manage_guild(
        guild_id=b,
        user_sub=owner_a,  # owner of A, presented against B
        discord_id=admin_disc,  # admin of A, presented against B
        is_super_admin=False,
        owner_sub=f"owner-of-{b}",  # B's real owner, different from owner_a-ish
        admin_discord_ids=set(),  # B has no admin edges for this caller
    )
    # owner_a might coincidentally equal f"owner-of-{b}" only if it literally
    # matches; the text strategy can't produce that string (has no '/'), but be
    # explicit: the only way granted is if subs match.
    assert granted_b == (owner_a == f"owner-of-{b}")


@settings(max_examples=100)
@given(a=_ID, b=_ID, owner_a=st.text(min_size=1, max_size=12), disc=_ID)
def test_super_admin_still_manages_any_guild(
    a: str, b: str, owner_a: str, disc: str
) -> None:
    """The Platform_Owner (super-admin) manages any guild (R5.2 exception)."""
    assert can_manage_guild(
        guild_id=b,
        user_sub=owner_a,
        discord_id=disc,
        is_super_admin=True,
        owner_sub=f"owner-of-{b}",
        admin_discord_ids=set(),
    )


# -- Example: tokens land in Secrets Manager, never DynamoDB (R5.1) --------- #


def test_store_tokens_writes_secret_and_metadata_only_in_dynamo() -> None:
    svc, core, secrets = _service()

    svc.store_tokens(
        "g1",
        "tidal",
        {"access_token": "secret-A", "refresh_token": "secret-R"},
        connected_by="owner-sub",
    )

    # Tokens are ONLY in the isolated Per_Guild_Secret.
    name = guild_source_secret_name(STAGE, "g1", "tidal")
    assert name in secrets.store
    assert json.loads(secrets.store[name]) == {
        "access_token": "secret-A",
        "refresh_token": "secret-R",
    }

    # The DynamoDB item holds ONLY non-secret metadata — no token material.
    item = core.get(guild_pk("g1"), source_sk("tidal"))
    assert item is not None
    data = item["data"]
    assert data["connected"] is True
    assert data["connected_by"] == "owner-sub"
    serialized = json.dumps(item).lower()
    for leak in ("secret-a", "secret-r", "access_token", "refresh_token"):
        assert leak.lower() not in serialized


def test_store_tokens_updates_existing_secret_in_place() -> None:
    svc, core, secrets = _service()
    name = guild_source_secret_name(STAGE, "g1", "spotify")

    svc.store_tokens("g1", "spotify", {"t": "v1"}, connected_by="owner")
    svc.store_tokens("g1", "spotify", {"t": "v2"}, connected_by="owner")

    # Re-connect overwrites the same isolated secret (no duplicate).
    assert json.loads(secrets.store[name]) == {"t": "v2"}
    item = core.get(guild_pk("g1"), source_sk("spotify"))
    assert item is not None and item["data"]["connected"] is True


def test_store_tokens_is_per_guild_isolated() -> None:
    """Connecting a provider for guild A does not touch guild B (R5.3)."""
    svc, _, secrets = _service()

    svc.store_tokens("A", "tidal", {"t": "A-token"}, connected_by="a")

    assert guild_source_secret_name(STAGE, "A", "tidal") in secrets.store
    assert guild_source_secret_name(STAGE, "B", "tidal") not in secrets.store


# -- Example: disconnect deletes secret + metadata (R5.3) ------------------- #


def test_disconnect_deletes_secret_and_metadata() -> None:
    svc, core, secrets = _service()
    name = guild_source_secret_name(STAGE, "g1", "youtube")
    svc.store_tokens("g1", "youtube", {"t": "v"}, connected_by="owner")
    assert name in secrets.store

    svc.disconnect("g1", "youtube")

    assert name not in secrets.store
    assert core.get(guild_pk("g1"), source_sk("youtube")) is None


def test_disconnect_is_noop_for_never_connected_provider() -> None:
    svc, core, _ = _service()

    # Must not raise when nothing was ever connected.
    svc.disconnect("g1", "spotify")

    assert core.get(guild_pk("g1"), source_sk("spotify")) is None


# -- Example: status reflects per-provider connection (R5.4) ---------------- #


def test_status_lists_every_provider_with_connection_flags() -> None:
    svc, _, _ = _service()
    svc.store_tokens("g1", "tidal", {"t": "v"}, connected_by="owner")

    status = {row["provider"]: row for row in svc.status("g1")}

    # Every supported provider appears (per-provider status, R5.4).
    assert set(status) == set(SUPPORTED_PROVIDERS)
    assert status["tidal"]["connected"] is True
    for other in SUPPORTED_PROVIDERS:
        if other != "tidal":
            assert status[other]["connected"] is False


def test_status_is_isolated_between_guilds() -> None:
    """Guild A's connection does not appear as connected for guild B (R5.3)."""
    svc, _, _ = _service()
    svc.store_tokens("A", "tidal", {"t": "v"}, connected_by="a")

    b_status = {row["provider"]: row["connected"] for row in svc.status("B")}

    assert all(connected is False for connected in b_status.values())


def test_store_tokens_rejects_unsupported_provider() -> None:
    svc, _, secrets = _service()

    import pytest

    with pytest.raises(ValueError, match="unsupported provider"):
        svc.store_tokens("g1", "soundcloud", {"t": "v"}, connected_by="owner")
    # Nothing was written for the rejected provider.
    assert secrets.store == {}


def test_owner_sk_and_admin_sk_helpers_are_stable() -> None:
    # Guard the SK shapes the isolation model relies on.
    assert owner_sk() == "OWNER"
    assert admin_sk("123") == "ADMIN#123"
    assert source_sk("tidal") == "SOURCE#tidal"
