"""Shared in-memory fakes for the task 4.3 YouTube node-pool tests.

Extracted so both node-pool test modules
(``test_youtube_node_pool`` — assignment/isolation/lock behavior — and
``test_youtube_node_pool_props`` — the Hypothesis P1 isolation properties)
share ONE set of fakes and stay under the 500-line ceiling. These are TEST
helpers only (not shipped): they let the fully-wired
:class:`YouTubeCredentialInjector` +
:class:`~playback.lavalink_node_pool.LavalinkNodePool` +
:class:`DynamoCredentialResolver` path run with no boto3 / no live AWS or
Lavalink.

Bare imports rely on ``bot/playback`` being on ``sys.path`` (see ``conftest``).
"""

from __future__ import annotations

import asyncio
import base64
import json

from guild_credentials import (
    DynamoCredentialResolver,
    GuildCredentialResolver,
    YouTubeCredentialInjector,
    sourcecred_sk,
    user_pk,
)
from lavalink_node_pool import LavalinkNodePool

STAGE = "beta"


class StepClock:
    """Deterministic monotonic clock; ``tick`` advances it by one second."""

    def __init__(self) -> None:
        self._t = 0.0

    def __call__(self) -> float:
        return self._t

    def tick(self, dt: float = 1.0) -> None:
        self._t += dt


class FakeStore:
    """In-memory ``CredentialItemStore`` keyed by ``(pk, sk)``.

    Records every read so tests can assert the resolver addressed exactly the
    owning user's ``USER#<sub>`` / ``SOURCECRED#<provider>`` item and no other.
    """

    def __init__(
        self, items: dict[tuple[str, str], dict[str, object]] | None = None
    ) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = dict(items or {})
        self.reads: list[tuple[str, str]] = []

    def get(self, pk: str, sk: str) -> dict[str, object] | None:
        self.reads.append((pk, sk))
        return self.items.get((pk, sk))


class FakeOwners:
    """In-memory ``OwnerLookup`` (guild id → owning Cognito ``sub``)."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = dict(mapping)

    def owner_of(self, guild_id: str) -> str | None:
        return self.mapping.get(str(guild_id))


class FakeLavalink:
    """Fake Lavalink ``/youtube`` endpoint modeling one node's mutable state.

    The real plugin replaces ALL credential fields on every ``POST /youtube``
    call for a node. This models that node's mutable ``current`` state so tests
    can assert that, under the per-node lock, an owner's resolution observes
    exactly its own creds even when other owners swap the SAME node
    concurrently. An optional ``delay`` widens the interleaving window so a
    missing lock would be observable.
    """

    def __init__(self, *, ok: bool = True, delay: float = 0.0) -> None:
        self.ok = ok
        self.delay = delay
        self.pushes: list[dict[str, object]] = []
        self.current: dict[str, object] | None = None

    async def push(self, payload: dict[str, object]) -> bool:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.pushes.append(dict(payload))
        if self.ok:
            self.current = dict(payload)
        return self.ok


class FakeSecrets:
    """Empty fake secretsmanager: the DynamoDB path is the source of truth.

    A guild with no unified item and no secret resolves to ``None`` (no
    credential) — exercising the no-credential-guild global-path behavior.
    """

    def __init__(self, store: dict[str, object] | None = None) -> None:
        self.store: dict[str, object] = dict(store or {})
        self.calls: list[str] = []

    def get_secret_value(self, **kwargs: object) -> dict[str, object]:
        name = str(kwargs["SecretId"])
        self.calls.append(name)
        if name not in self.store:
            raise KeyError(name)
        value = self.store[name]
        raw = value if isinstance(value, str) else json.dumps(value)
        return {"SecretString": raw}


def enc_item(refresh: str, pot: str, visitor: str) -> dict[str, object]:
    """A stored ``SourceCredential`` item whose blob :func:`fake_decrypt` returns.

    The envelope fields must be present and base64 for the resolver's decrypt
    path; :func:`fake_decrypt` ignores them and returns the token JSON embedded
    in ``enc_blob`` so a test controls the plaintext without real crypto.
    """
    blob = {
        "refresh_token": refresh,
        "extra": {"pot_token": pot, "pot_visitor_data": visitor},
    }
    raw = base64.b64encode(json.dumps(blob).encode("utf-8")).decode("ascii")
    return {
        "entityType": "SourceCredential",
        "data": {
            "enc_blob": raw,
            "enc_key": base64.b64encode(b"k").decode("ascii"),
            "enc_nonce": base64.b64encode(b"n").decode("ascii"),
            "kms_key_id": "alias/test",
        },
    }


def fake_decrypt(
    *, ciphertext: bytes, wrapped_key: bytes, key_id: str, nonce: bytes
) -> bytes:
    """Fake ``DecryptBlob``: the ciphertext IS the plaintext token JSON.

    :func:`enc_item` base64-encodes the token JSON into ``enc_blob``; the
    resolver base64-decodes it into ``ciphertext`` before calling this, so
    returning it verbatim yields the embedded token blob with no real
    KMS/crypto.
    """
    return ciphertext


def build_injector(
    store: FakeStore,
    owners: FakeOwners,
    pool: LavalinkNodePool,
    push,
) -> YouTubeCredentialInjector:
    """Build a fully-wired injector over the unified DynamoDB resolver + pool."""
    dyn = DynamoCredentialResolver(store, owners, fake_decrypt)
    guild_resolver = GuildCredentialResolver(
        FakeSecrets(), stage=STAGE, dynamo_resolver=dyn
    )
    return YouTubeCredentialInjector(
        guild_resolver, push, node_pool=pool, owner_lookup=owners
    )


def guild_items(
    specs: dict[str, tuple[str, str, str, str]],
) -> tuple[FakeStore, FakeOwners]:
    """Build a store + owners from ``{gid: (owner_sub, refresh, pot, visitor)}``.

    Writes both the ``GUILD#<gid>``/``OWNER`` mapping (via owners) and the
    owner's ``USER#<sub>``/``SOURCECRED#youtube`` credential item.
    """
    owners_map: dict[str, str] = {}
    items: dict[tuple[str, str], dict[str, object]] = {}
    for gid, (sub, refresh, pot, visitor) in specs.items():
        owners_map[gid] = sub
        items[(user_pk(sub), sourcecred_sk("youtube"))] = enc_item(
            refresh, pot, visitor
        )
    return FakeStore(items), FakeOwners(owners_map)
