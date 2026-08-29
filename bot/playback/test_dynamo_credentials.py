"""Tests for the unified DynamoDB credential resolution branch (Task 9).

Covers the bot's READ-ONLY resolution of the unified per-user credential store
(``PK=USER#<sub>`` / ``SK=SOURCECRED#<provider>``) added to
``guild_credentials.py``, using in-memory fakes for the ``hellodj-core`` table,
the guild-owner lookup, and the envelope-decrypt seam (no live AWS, no
``hellodj_platform_logic`` import) — matching the ``FakeSecrets``/``FakeClock``
style of ``test_guild_credentials.py``.

The fake decrypt seam performs a REAL AES-GCM round-trip (via ``cryptography``,
already a bot dep) with an in-memory fake KMS, so the crypto contract — including
tamper-fails — is exercised, not stubbed.

Validates Requirement 6:
- 6.1 resolve the DynamoDB credential item, decrypt the blob, use the token
- 6.2 an expired access token triggers a re-read (watchdog-refreshed value),
      never serving a dead token from cache
- 6.3 the YouTube ``POST /youtube`` all-fields-together swap is preserved when
      the credential comes from DynamoDB (OAuth refresh + poToken + visitorData
      in ONE payload)
- 6.4 bounded-TTL cache; one user's credential is never returned for another
- 6.5 a DynamoDB-absent credential falls back to the legacy per-guild secret
"""

from __future__ import annotations

import base64
import json
import os

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from guild_credentials import (
    CredentialUnavailable,
    DynamoCredentialResolver,
    GuildCredentialResolver,
    YouTubeCredentialInjector,
    sourcecred_sk,
    token_state_to_tokens,
    youtube_oauth_payload,
)

STAGE = "beta"


# ── deterministic fakes ─────────────────────────────────────────────────


class FakeClock:
    """Deterministic monotonic clock for TTL testing."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeWallClock:
    """Deterministic epoch-seconds clock for expiry comparison (R6.2)."""

    def __init__(self, start: float = 10_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeKms:
    """In-memory fake KMS modeling envelope semantics for one static data key.

    ``generate``/``decrypt`` wrap and unwrap a fixed 32-byte data key by simply
    prefixing it; enough to exercise a real AES-GCM round-trip in the decrypt
    seam without any AWS.
    """

    def __init__(self) -> None:
        self.data_key = os.urandom(32)

    def wrap(self) -> bytes:
        return b"WRAP:" + self.data_key

    def unwrap(self, wrapped: bytes) -> bytes:
        if not wrapped.startswith(b"WRAP:"):
            raise ValueError("bad wrapped key")
        return wrapped[len(b"WRAP:") :]


class FakeCore:
    """Fake ``hellodj-core`` table exposing only ``get`` (read-only, R9.3).

    Records every (pk, sk) fetched so tests can assert the exact items read and
    count reads (for TTL/caching / re-read assertions).
    """

    def __init__(self, items: dict[tuple[str, str], dict] | None = None) -> None:
        self.items: dict[tuple[str, str], dict] = dict(items or {})
        self.calls: list[tuple[str, str]] = []

    def get(self, pk: str, sk: str) -> dict | None:
        self.calls.append((pk, sk))
        item = self.items.get((pk, sk))
        return dict(item) if item is not None else None


class FakeOwners:
    """Fake guild → owner-sub lookup (``GUILD#<gid>`` / ``OWNER``)."""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping = dict(mapping or {})

    def owner_of(self, guild_id: str) -> str | None:
        return self.mapping.get(str(guild_id))


class FakeSecrets:
    """Fake secretsmanager client (matches test_guild_credentials.FakeSecrets)."""

    def __init__(self, store: dict[str, object] | None = None) -> None:
        self.store: dict[str, object] = dict(store or {})
        self.calls: list[str] = []

    def get_secret_value(self, **kwargs: object) -> dict[str, object]:
        name = str(kwargs["SecretId"])
        self.calls.append(name)
        if name not in self.store:
            raise KeyError(f"Secrets Manager can't find {name}")
        value = self.store[name]
        raw = value if isinstance(value, str) else json.dumps(value)
        return {"SecretString": raw}


class FakeLavalink:
    """Fake Lavalink ``/youtube`` endpoint (matches the injector test)."""

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.pushes: list[dict[str, object]] = []
        self.current: dict[str, object] | None = None

    async def push(self, payload: dict[str, object]) -> bool:
        self.pushes.append(dict(payload))
        if self.ok:
            self.current = dict(payload)
        return self.ok


# ── envelope helpers (real AES-GCM round-trip) ──────────────────────────


def _make_decrypt(kms: FakeKms):
    """Return a DecryptBlob seam that AES-GCM-decrypts with the fake KMS key."""

    def _decrypt(
        *, ciphertext: bytes, wrapped_key: bytes, key_id: str, nonce: bytes
    ) -> bytes:
        data_key = kms.unwrap(wrapped_key)
        return AESGCM(data_key).decrypt(nonce, ciphertext, None)

    return _decrypt


def _cred_item(
    kms: FakeKms,
    *,
    access_token: str = "AT",
    refresh_token: str = "RT",
    expires_at: float = 99_999.0,
    scope: str = "",
    extra: dict | None = None,
) -> dict:
    """Build a stored credential item whose blob is really AES-GCM-encrypted.

    Mirrors the web-ui ``source_credential_service`` item shape: base64
    ``enc_blob`` / ``enc_key`` / ``enc_nonce`` + ``kms_key_id``, plaintext
    ``expires_at``.
    """
    blob = json.dumps(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "scope": scope,
            "extra": dict(extra or {}),
        }
    ).encode("utf-8")
    nonce = os.urandom(12)
    ciphertext = AESGCM(kms.data_key).encrypt(nonce, blob, None)
    return {
        "PK": "USER#owner-1",
        "SK": sourcecred_sk("tidal"),
        "entityType": "SourceCredential",
        "data": {
            "connected": True,
            "expires_at": expires_at,
            "scope": scope,
            "refresh_status": "ok",
            "enc_blob": base64.b64encode(ciphertext).decode("ascii"),
            "enc_key": base64.b64encode(kms.wrap()).decode("ascii"),
            "enc_nonce": base64.b64encode(nonce).decode("ascii"),
            "kms_key_id": "arn:aws:kms:key/cmk-1",
        },
    }


def _make_resolver(
    core: FakeCore,
    owners: FakeOwners,
    kms: FakeKms,
    clock: FakeClock,
    wall: FakeWallClock,
    **kwargs,
) -> DynamoCredentialResolver:
    return DynamoCredentialResolver(
        core,
        owners,
        _make_decrypt(kms),
        time_fn=clock,
        wall_clock=wall,
        **kwargs,
    )


# ── token_state_to_tokens flattening ────────────────────────────────────


class TestFlatten:
    def test_surfaces_refresh_as_oauth_refresh_token(self):
        tokens = token_state_to_tokens(
            {"refresh_token": "RT", "access_token": "AT", "expires_at": 5.0}
        )
        assert tokens["refresh_token"] == "RT"
        assert tokens["oauth_refresh_token"] == "RT"
        assert tokens["access_token"] == "AT"
        assert tokens["expires_at"] == 5.0

    def test_merges_provider_extra_fields(self):
        # YouTube poToken / visitorData live in ``extra`` and must travel with
        # the refresh token (R6.3).
        tokens = token_state_to_tokens(
            {
                "refresh_token": "RT",
                "extra": {"pot_token": "PO", "pot_visitor_data": "VD"},
            }
        )
        assert tokens["pot_token"] == "PO"
        assert tokens["pot_visitor_data"] == "VD"
        assert tokens["oauth_refresh_token"] == "RT"

    def test_empty_blob_flattens_empty(self):
        assert token_state_to_tokens({}) == {}


# ── R6.1: resolve + decrypt ─────────────────────────────────────────────


class TestResolveDecrypt:
    def test_resolves_and_decrypts_owner_credential(self):
        kms = FakeKms()
        core = FakeCore(
            {("USER#owner-1", sourcecred_sk("tidal")): _cred_item(kms, refresh_token="RT")}
        )
        owners = FakeOwners({"111": "owner-1"})
        r = _make_resolver(core, owners, kms, FakeClock(), FakeWallClock())

        tokens = r.resolve("111", "tidal")

        assert tokens is not None
        assert tokens["refresh_token"] == "RT"
        assert tokens["access_token"] == "AT"
        # read the owner's credential item under the resolved sub
        assert ("USER#owner-1", sourcecred_sk("tidal")) in core.calls

    def test_no_owner_returns_unavailable(self):
        # R1.2: no recorded owner → typed CredentialUnavailable(no_owner),
        # never another user's credential.
        kms = FakeKms()
        core = FakeCore()
        r = _make_resolver(core, FakeOwners({}), kms, FakeClock(), FakeWallClock())
        result = r.resolve("111", "tidal")
        assert result == CredentialUnavailable("no_owner")
        # no credential item read attempted without an owner
        assert core.calls == []

    def test_absent_item_returns_unavailable(self):
        # R1.2: owner exists but no SOURCECRED item → no_credential.
        kms = FakeKms()
        core = FakeCore()  # owner exists but no credential item
        owners = FakeOwners({"111": "owner-1"})
        r = _make_resolver(core, owners, kms, FakeClock(), FakeWallClock())
        assert r.resolve("111", "tidal") == CredentialUnavailable("no_credential")

    def test_tampered_ciphertext_returns_unavailable(self):
        # R3.4/R2.3: a tampered blob must not decrypt to wrong plaintext — it
        # becomes unusable (decrypt_failed), never a crash, never a token.
        kms = FakeKms()
        item = _cred_item(kms, refresh_token="RT")
        raw = base64.b64decode(item["data"]["enc_blob"])
        item["data"]["enc_blob"] = base64.b64encode(b"\x00" + raw[1:]).decode("ascii")
        core = FakeCore({("USER#owner-1", sourcecred_sk("tidal")): item})
        owners = FakeOwners({"111": "owner-1"})
        r = _make_resolver(core, owners, kms, FakeClock(), FakeWallClock())
        assert r.resolve("111", "tidal") == CredentialUnavailable("decrypt_failed")

    def test_missing_envelope_fields_returns_unavailable(self):
        kms = FakeKms()
        item = _cred_item(kms)
        del item["data"]["enc_key"]  # short envelope
        core = FakeCore({("USER#owner-1", sourcecred_sk("tidal")): item})
        owners = FakeOwners({"111": "owner-1"})
        r = _make_resolver(core, owners, kms, FakeClock(), FakeWallClock())
        assert r.resolve("111", "tidal") == CredentialUnavailable("decrypt_failed")

    def test_refresh_status_failed_gates_before_decrypt(self):
        # R2.3: an item marked refresh_status=failed resolves to
        # CredentialUnavailable(refresh_failed) and NEVER a token — even though
        # the (untampered) blob would decrypt fine.
        kms = FakeKms()
        item = _cred_item(kms, refresh_token="RT")
        item["data"]["refresh_status"] = "failed"
        core = FakeCore({("USER#owner-1", sourcecred_sk("tidal")): item})
        owners = FakeOwners({"111": "owner-1"})
        r = _make_resolver(core, owners, kms, FakeClock(), FakeWallClock())
        assert r.resolve("111", "tidal") == CredentialUnavailable("refresh_failed")


# ── R6.4: cache TTL ─────────────────────────────────────────────────────


class TestCacheTtl:
    def test_within_ttl_hits_core_once(self):
        kms = FakeKms()
        core = FakeCore(
            {("USER#owner-1", sourcecred_sk("tidal")): _cred_item(kms)}
        )
        owners = FakeOwners({"111": "owner-1"})
        clock = FakeClock()
        r = _make_resolver(core, owners, kms, clock, FakeWallClock(), ttl_seconds=300.0)

        r.resolve("111", "tidal")
        reads_after_first = len(core.calls)
        clock.advance(299.0)
        r.resolve("111", "tidal")

        # cached — no additional credential read within TTL
        assert len(core.calls) == reads_after_first

    def test_refreshes_after_ttl(self):
        kms = FakeKms()
        core = FakeCore(
            {("USER#owner-1", sourcecred_sk("tidal")): _cred_item(kms)}
        )
        owners = FakeOwners({"111": "owner-1"})
        clock = FakeClock()
        r = _make_resolver(core, owners, kms, clock, FakeWallClock(), ttl_seconds=300.0)

        r.resolve("111", "tidal")
        reads_after_first = len(core.calls)
        clock.advance(301.0)
        r.resolve("111", "tidal")

        assert len(core.calls) > reads_after_first

    def test_invalidate_forces_refresh(self):
        kms = FakeKms()
        core = FakeCore(
            {("USER#owner-1", sourcecred_sk("tidal")): _cred_item(kms)}
        )
        owners = FakeOwners({"111": "owner-1"})
        r = _make_resolver(core, owners, kms, FakeClock(), FakeWallClock())
        r.resolve("111", "tidal")
        reads = len(core.calls)
        r.invalidate("111", "tidal")
        r.resolve("111", "tidal")
        assert len(core.calls) > reads


# ── R6.2: expired access token → re-read (watchdog value) ───────────────


class TestExpiryReread:
    def test_expired_cached_token_triggers_reread(self):
        kms = FakeKms()
        wall = FakeWallClock(start=10_000.0)
        # stored token already expired relative to wall clock
        expired = _cred_item(kms, refresh_token="OLD", expires_at=9_000.0)
        core = FakeCore({("USER#owner-1", sourcecred_sk("tidal")): expired})
        owners = FakeOwners({"111": "owner-1"})
        clock = FakeClock()
        r = _make_resolver(
            core, owners, kms, clock, wall, ttl_seconds=300.0, expiry_skew_seconds=30.0
        )

        first = r.resolve("111", "tidal")
        assert first["refresh_token"] == "OLD"
        reads_after_first = len(core.calls)

        # watchdog refreshes the item out-of-band with a fresh, non-expired token
        core.items[("USER#owner-1", sourcecred_sk("tidal"))] = _cred_item(
            kms, refresh_token="FRESH", expires_at=99_999.0
        )

        # still within the monotonic TTL window, but the cached token is expired
        # → resolver must NOT serve the dead token; it re-reads (R6.2).
        second = r.resolve("111", "tidal")
        assert second["refresh_token"] == "FRESH"
        assert len(core.calls) > reads_after_first

    def test_non_expired_token_not_rereadwithin_ttl(self):
        kms = FakeKms()
        wall = FakeWallClock(start=10_000.0)
        fresh = _cred_item(kms, expires_at=99_999.0)
        core = FakeCore({("USER#owner-1", sourcecred_sk("tidal")): fresh})
        owners = FakeOwners({"111": "owner-1"})
        r = _make_resolver(
            core, owners, kms, FakeClock(), wall, ttl_seconds=300.0
        )
        r.resolve("111", "tidal")
        reads = len(core.calls)
        r.resolve("111", "tidal")
        assert len(core.calls) == reads  # cached, not re-read

    def test_blob_without_expiry_never_considered_expired(self):
        kms = FakeKms()
        wall = FakeWallClock(start=10_000.0)
        item = _cred_item(kms, expires_at=1.0)
        # remove the blob-level expiry so _is_expired sees no expires_at
        raw = json.loads(AESGCM(kms.data_key).decrypt(
            base64.b64decode(item["data"]["enc_nonce"]),
            base64.b64decode(item["data"]["enc_blob"]),
            None,
        ))
        raw.pop("expires_at", None)
        blob = json.dumps(raw).encode("utf-8")
        nonce = os.urandom(12)
        item["data"]["enc_blob"] = base64.b64encode(
            AESGCM(kms.data_key).encrypt(nonce, blob, None)
        ).decode("ascii")
        item["data"]["enc_nonce"] = base64.b64encode(nonce).decode("ascii")
        core = FakeCore({("USER#owner-1", sourcecred_sk("tidal")): item})
        owners = FakeOwners({"111": "owner-1"})
        r = _make_resolver(core, owners, kms, FakeClock(), wall, ttl_seconds=300.0)
        r.resolve("111", "tidal")
        reads = len(core.calls)
        r.resolve("111", "tidal")
        assert len(core.calls) == reads  # refresh-only cred: cached, no re-read


# ── R6.3, R6.4: cross-user isolation ────────────────────────────────────


class TestIsolation:
    def _two_user_core(self, kms: FakeKms) -> FakeCore:
        item_a = _cred_item(kms, refresh_token="A-token")
        item_a["PK"] = "USER#owner-A"
        item_b = _cred_item(kms, refresh_token="B-token")
        item_b["PK"] = "USER#owner-B"
        return FakeCore(
            {
                ("USER#owner-A", sourcecred_sk("tidal")): item_a,
                ("USER#owner-B", sourcecred_sk("tidal")): item_b,
            }
        )

    def test_each_guild_resolves_its_owners_credential(self):
        kms = FakeKms()
        core = self._two_user_core(kms)
        owners = FakeOwners({"111": "owner-A", "222": "owner-B"})
        r = _make_resolver(core, owners, kms, FakeClock(), FakeWallClock())

        assert r.resolve("111", "tidal")["refresh_token"] == "A-token"
        assert r.resolve("222", "tidal")["refresh_token"] == "B-token"

    def test_cached_user_a_never_returned_for_guild_b(self):
        kms = FakeKms()
        # only guild 111 (owner-A) has a credential; guild 222 (owner-B) has none
        item_a = _cred_item(kms, refresh_token="A-token")
        item_a["PK"] = "USER#owner-A"
        core = FakeCore({("USER#owner-A", sourcecred_sk("tidal")): item_a})
        owners = FakeOwners({"111": "owner-A", "222": "owner-B"})
        r = _make_resolver(core, owners, kms, FakeClock(), FakeWallClock())

        assert r.resolve("111", "tidal")["refresh_token"] == "A-token"
        # B must resolve to unavailable — the cached A entry must not bleed
        # across, and no other user's credential is substituted (R1.2, R6).
        assert r.resolve("222", "tidal") == CredentialUnavailable("no_credential")
        # and A stays A after B was resolved
        assert r.resolve("111", "tidal")["refresh_token"] == "A-token"


# ── R6.5: DynamoDB-absent falls back to legacy secret ───────────────────


class TestLegacyFallback:
    def test_dynamo_hit_wins_over_secret(self):
        kms = FakeKms()
        core = FakeCore(
            {("USER#owner-1", sourcecred_sk("tidal")): _cred_item(kms, refresh_token="DDB")}
        )
        owners = FakeOwners({"111": "owner-1"})
        dynamo = _make_resolver(core, owners, kms, FakeClock(), FakeWallClock())
        secrets = FakeSecrets(
            {"hellodj/beta/guild/111/tidal": {"refresh_token": "LEGACY"}}
        )
        gr = GuildCredentialResolver(
            secrets, stage=STAGE, time_fn=FakeClock(), dynamo_resolver=dynamo
        )

        tokens = gr.resolve("111", "tidal")

        assert tokens["refresh_token"] == "DDB"
        # DynamoDB satisfied it — the legacy secret was never consulted (R6.5).
        assert secrets.calls == []

    def test_falls_back_to_legacy_secret_when_no_ddb_item(self):
        kms = FakeKms()
        core = FakeCore()  # no owner / no item
        owners = FakeOwners({})
        dynamo = _make_resolver(core, owners, kms, FakeClock(), FakeWallClock())
        secrets = FakeSecrets(
            {"hellodj/beta/guild/111/tidal": {"refresh_token": "LEGACY"}}
        )
        gr = GuildCredentialResolver(
            secrets, stage=STAGE, time_fn=FakeClock(), dynamo_resolver=dynamo
        )

        tokens = gr.resolve("111", "tidal")

        assert tokens == {"refresh_token": "LEGACY"}
        assert secrets.calls == ["hellodj/beta/guild/111/tidal"]

    def test_no_dynamo_resolver_uses_legacy_only(self):
        secrets = FakeSecrets(
            {"hellodj/beta/guild/111/tidal": {"refresh_token": "LEGACY"}}
        )
        gr = GuildCredentialResolver(secrets, stage=STAGE, time_fn=FakeClock())
        assert gr.resolve("111", "tidal") == {"refresh_token": "LEGACY"}


# ── R6.3: DynamoDB-sourced YouTube POST /youtube all-fields-together ─────


class TestYouTubeSwapFromDynamo:
    def _youtube_core(self, kms: FakeKms) -> FakeCore:
        item = _cred_item(
            kms,
            refresh_token="yt-refresh",
            extra={"pot_token": "PO", "pot_visitor_data": "VD"},
        )
        item["PK"] = "USER#owner-1"
        item["SK"] = sourcecred_sk("youtube")
        return FakeCore({("USER#owner-1", sourcecred_sk("youtube")): item})

    def test_payload_sends_all_fields_together(self):
        # The single POST /youtube request must carry OAuth refresh + poToken +
        # visitorData TOGETHER (R6.3). Build the payload from the flattened
        # DynamoDB tokens and assert all three are present in ONE dict.
        kms = FakeKms()
        core = self._youtube_core(kms)
        owners = FakeOwners({"111": "owner-1"})
        r = _make_resolver(core, owners, kms, FakeClock(), FakeWallClock())

        tokens = r.resolve("111", "youtube")
        payload = youtube_oauth_payload(tokens, skip_initialization=False)

        assert payload is not None
        assert payload["refreshToken"] == "yt-refresh"
        assert payload["poToken"] == "PO"
        assert payload["visitorData"] == "VD"

    @pytest.mark.asyncio
    async def test_injector_swaps_dynamo_youtube_creds_in_one_request(self):
        kms = FakeKms()
        core = self._youtube_core(kms)
        owners = FakeOwners({"111": "owner-1"})
        dynamo = _make_resolver(core, owners, kms, FakeClock(), FakeWallClock())
        secrets = FakeSecrets({})  # no legacy secret — must come from DynamoDB
        gr = GuildCredentialResolver(
            secrets, stage=STAGE, time_fn=FakeClock(), dynamo_resolver=dynamo
        )
        lava = FakeLavalink()
        injector = YouTubeCredentialInjector(gr, lava.push)

        swapped = await injector.inject_for_guild("111", "youtube")

        assert swapped is True
        # exactly one POST /youtube, carrying all three fields together (R6.3)
        assert len(lava.pushes) == 1
        assert lava.current["refreshToken"] == "yt-refresh"
        assert lava.current["poToken"] == "PO"
        assert lava.current["visitorData"] == "VD"
