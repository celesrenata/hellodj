"""Regression: live-DynamoDB ``expires_at`` is a ``Decimal``, not an ``int``.

The in-memory ``_FakeTable`` used by the other invite tests stores
``expires_at`` as a Python ``int``, but real DynamoDB deserializes Number
(``N``) attributes to :class:`decimal.Decimal`. ``Decimal`` is a
``numbers.Number`` but NOT a ``numbers.Real`` and NOT an ``int`` — so the old
``isinstance(expires_at, int)`` validity guard rejected every freshly minted
live invite as expired (the "invite immediately expired" bug).

These tests model the real backend type by rewriting the stored ``expires_at``
to a ``Decimal`` and assert the (unexpired) invite still resolves, consumes, and
lists as ``invited`` rather than ``expired``.
"""

from __future__ import annotations

import time
from decimal import Decimal

from hellodj_platform_logic.data_access import CoreTable
from test_invite_admin import _service as _admin_service
from test_invite_service import _FakeCognito, _FakeTable

from invite_service import INVITE_SK, InviteService, invite_pk
from user_profile import UserProfileService


def _set_expires_at_decimal(core: CoreTable, email: str, epoch: int) -> None:
    """Rewrite an invite's ``expires_at`` to a ``Decimal`` (live-DDB type)."""
    stored = core.get(invite_pk(email), INVITE_SK)
    core.update_with_lock(
        stored["PK"],
        stored["SK"],
        lambda d: {**d, "expires_at": Decimal(epoch)},
    )


def test_resolve_and_consume_accept_decimal_expires_at() -> None:
    core = CoreTable(_FakeTable())
    profiles = UserProfileService(core)
    svc = InviteService(
        core, _FakeCognito(), user_pool_id="pool-1", user_profiles=profiles
    )
    raw = svc.invite("dec@example.com", invited_by="owner@x.io")["raw_token"]
    _set_expires_at_decimal(core, "dec@example.com", int(time.time()) + 3600)

    assert svc.resolve_by_token(raw)["email"] == "dec@example.com"
    # The single-use consume path shares the same validity check.
    assert svc.consume(raw)["status"] == "accepted"


def test_list_invites_shows_invited_for_decimal_expires_at() -> None:
    svc, core, _, _ = _admin_service()
    svc.invite("dec-list@example.com", invited_by="owner@x.io")
    _set_expires_at_decimal(core, "dec-list@example.com", int(time.time()) + 3600)

    assert svc.list_invites()[0]["status"] == "invited"
