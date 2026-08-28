"""End-to-end Invite_Token lifecycle test (task 4).

The single-stage behaviours — hash-only persistence, GSI1 keys, TTL, duplicate
rejection, ``resolve``/``consume``/``register`` in isolation, and the concurrent
single-use race — are covered in ``test_invite_service.py``. This module adds
the one thing that file lacks: a full **mint -> resolve -> consume -> register**
walk on a *single* token, asserting the three lifecycle guarantees together on
that one token:

* R1.2 / R7.4 — only the SHA-256 hash is ever persisted; the raw token is never
  written to the datastore.
* R2.3 — a consumed / expired / unknown token is rejected with the fixed
  ``InviteConsumedError`` outcome (no registration form / account).
* R2.5 — the token is single-use: once registered it cannot be resolved,
  consumed, or re-registered.

It reuses the in-memory ``_FakeTable`` / ``_FakeCognito`` fakes (no real AWS,
no real sleeps — expiry is simulated by mutating ``expires_at``).

Requirements: 1.2, 2.3, 2.5
"""

from __future__ import annotations

import time

import pytest
from hellodj_platform_logic.data_access import CoreTable
from test_invite_service import _FakeCognito, _FakeTable

from invite_service import (
    INVITE_SK,
    InviteConsumedError,
    InviteService,
    hash_token,
    invite_pk,
    token_gsi1pk,
)
from user_profile import UserProfileService


def _lifecycle_service() -> tuple[InviteService, CoreTable, _FakeCognito]:
    """Build an InviteService wired end-to-end (profiles + cognito + table)."""
    table = _FakeTable()
    core = CoreTable(table)
    profiles = UserProfileService(core)
    cognito = _FakeCognito()
    svc = InviteService(
        core, cognito, user_pool_id="pool-1", user_profiles=profiles
    )
    return svc, core, cognito


def _expire_invite(core: CoreTable, email: str) -> None:
    """Push an invite's ``expires_at`` into the past (no real sleep)."""
    stored = core.get(invite_pk(email), INVITE_SK)
    core.update_with_lock(
        stored["PK"],
        stored["SK"],
        lambda d: {**d, "expires_at": int(time.time()) - 10},
    )


def test_full_lifecycle_mint_resolve_consume_register_single_token() -> None:
    """One token walks mint -> resolve -> consume -> register, then is burnt."""
    svc, core, cognito = _lifecycle_service()
    email = "sam@example.com"

    # -- mint --------------------------------------------------------------
    minted = svc.invite(email, invited_by="owner@x.io")
    raw = minted["raw_token"]
    assert minted["status"] == "invited"

    # R1.2 / R7.4: only the hash is stored; the raw token is never persisted.
    stored = core.get(invite_pk(email), INVITE_SK)
    assert stored["data"]["token_hash"] == hash_token(raw)
    assert raw not in str(stored)
    # The invite is resolvable by hashed token via GSI1 (single indexed lookup).
    rows = core.query_gsi1(token_gsi1pk(hash_token(raw)), sk_prefix=INVITE_SK)
    assert len(rows) == 1 and rows[0]["data"]["email"] == email

    # -- resolve -----------------------------------------------------------
    resolved = svc.resolve_by_token(raw)
    assert resolved["email"] == email
    assert resolved["status"] == "invited"

    # -- consume -----------------------------------------------------------
    consumed = svc.consume(raw)
    assert consumed["status"] == "accepted"
    assert isinstance(consumed["accepted_at"], int)

    # -- register (on the same token, after consume already accepted it) ---
    # ``register`` calls ``consume`` internally, so a fresh token is needed for
    # the account-creating path; the *already-consumed* token here must be
    # rejected before any Cognito call (R2.5 single-use, R2.3 fixed outcome).
    with pytest.raises(InviteConsumedError, match="used or has expired"):
        svc.register(raw)
    assert cognito.created == []


def test_full_lifecycle_register_creates_account_then_burns_token() -> None:
    """mint -> resolve -> register: the account is created, token then burnt."""
    svc, core, cognito = _lifecycle_service()
    email = "tia@example.com"

    raw = svc.invite(email, invited_by="owner@x.io")["raw_token"]
    assert svc.resolve_by_token(raw)["email"] == email  # valid before use

    account = svc.register(raw)
    assert account["email"] == email
    assert len(cognito.created) == 1  # CONFIRMED account created exactly once
    assert core.get(invite_pk(email), INVITE_SK)["data"]["status"] == "accepted"

    # R2.5 single-use: the burnt token is no longer resolvable/consumable and a
    # second register creates no further account (R2.3 fixed outcome).
    with pytest.raises(InviteConsumedError, match="used or has expired"):
        svc.resolve_by_token(raw)
    with pytest.raises(InviteConsumedError, match="used or has expired"):
        svc.register(raw)
    assert len(cognito.created) == 1


def test_lifecycle_rejects_expired_token_end_to_end() -> None:
    """R2.3: a token that expires mid-lifecycle is rejected at every stage."""
    svc, core, cognito = _lifecycle_service()
    email = "uma@example.com"

    raw = svc.invite(email, invited_by="owner@x.io")["raw_token"]
    _expire_invite(core, email)  # simulate TTL lapse without sleeping

    with pytest.raises(InviteConsumedError, match="used or has expired"):
        svc.resolve_by_token(raw)
    with pytest.raises(InviteConsumedError, match="used or has expired"):
        svc.consume(raw)
    with pytest.raises(InviteConsumedError, match="used or has expired"):
        svc.register(raw)
    # No account is ever created for an expired invite.
    assert cognito.created == []


def test_lifecycle_rejects_unknown_token_end_to_end() -> None:
    """R2.3: an unknown token is rejected at resolve, consume, and register."""
    svc, _, cognito = _lifecycle_service()

    for stage in (svc.resolve_by_token, svc.consume, svc.register):
        with pytest.raises(InviteConsumedError, match="used or has expired"):
            stage("never-minted-token")
    assert cognito.created == []
