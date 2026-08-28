"""Shared in-memory fakes + app wiring for registration-mode route tests.

Kept in one module so the display/enforcement suite
(``test_registration_mode_routes``) and the admin-change suite
(``test_registration_mode_admin_routes``) share identical fakes without either
file breaching the per-file line ceiling. Everything here is in-process — no
AWS, no Cognito, no network:

* :class:`_FakeCoreTable` implements the ``CoreTable`` surface a real
  :class:`config_store.ConfigStore` uses (``get`` / ``put_new`` /
  ``update_with_lock`` / ``query_pk_prefix``) so the config item and audit rows
  live on one inspectable table;
* :class:`_SpyCognitoAuth` records whether ``sign_up`` was invoked;
* :class:`_FakeInviteService` records which tokens reached invite handling;
* :func:`make_app` wires those onto ``app.extensions`` and seeds the mode.

Feature: registration-mode-control
"""

from __future__ import annotations

from typing import Any

from app import create_app
from cognito_auth import AuthResult
from config_store import (
    CONFIG_ENTITY_TYPE,
    CONFIG_SK,
    GLOBAL_CONFIG_PK,
    ConfigStore,
)
from registration_mode import AUDIT_SK_PREFIX, CONFIG_KEY

ADMIN_SUB = "admin-sub-1"

#: Fingerprint that only appears inside the rendered registration form body, so
#: a test can assert the CLOSED gate never rendered the form.
REGISTER_FORM_MARKER = 'name="email"'


class _FakeCoreTable:
    """In-memory ``CoreTable`` implementing the surface ``ConfigStore`` uses.

    Keys items by ``(pk, sk)`` with the same ``{PK, SK, entityType, data,
    version}`` envelope the real table stores, so a real :class:`ConfigStore`
    reads and writes through it unchanged and the route's audit ``put_new`` is
    observable. ``query_pk_prefix`` lets a test enumerate the audit rows under
    ``CONFIG#GLOBAL``.
    """

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.put_new_calls: list[dict[str, Any]] = []

    def get(self, pk: str, sk: str) -> dict[str, Any] | None:
        item = self.items.get((pk, sk))
        return dict(item) if item is not None else None

    def put_new(
        self,
        pk: str,
        sk: str,
        entity_type: str,
        data: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.put_new_calls.append(
            {"pk": pk, "sk": sk, "entity_type": entity_type, "data": dict(data)}
        )
        item = {
            "PK": pk,
            "SK": sk,
            "entityType": entity_type,
            "data": dict(data),
            "version": 1,
        }
        self.items[(pk, sk)] = item
        return dict(item)

    def update_with_lock(
        self, pk: str, sk: str, mutator: Any, *, entity_type: str | None = None
    ) -> dict[str, Any]:
        item = self.items.get((pk, sk)) or {
            "PK": pk,
            "SK": sk,
            "entityType": entity_type,
            "data": {},
            "version": 0,
        }
        data = dict(item.get("data", {}))
        new_data = dict(mutator(data))
        item = {
            "PK": pk,
            "SK": sk,
            "entityType": entity_type or item.get("entityType"),
            "data": new_data,
            "version": int(item.get("version", 0)) + 1,
        }
        self.items[(pk, sk)] = item
        return dict(item)

    def query_pk_prefix(
        self, pk: str, *, sk_prefix: str | None = None
    ) -> list[dict[str, Any]]:
        out = []
        for (ipk, isk), item in self.items.items():
            if ipk != pk:
                continue
            if sk_prefix is not None and not isk.startswith(sk_prefix):
                continue
            out.append(dict(item))
        return out

    # -- test helpers ------------------------------------------------------- #

    def audit_rows(self) -> list[dict[str, Any]]:
        """Return every registration-mode audit item under ``CONFIG#GLOBAL``."""
        return self.query_pk_prefix(GLOBAL_CONFIG_PK, sk_prefix=AUDIT_SK_PREFIX)


class _SpyCognitoAuth:
    """Spy ``CognitoAuth`` recording whether ``sign_up`` was ever invoked.

    ``handle_register`` calls ``sign_up`` on the OPEN POST path; the CLOSED gate
    must short-circuit before that, so ``sign_up_calls`` stays empty (Property
    4). On the OPEN happy path exactly one call is recorded (8.8).
    """

    def __init__(self) -> None:
        self.sign_up_calls: list[tuple[str, str]] = []

    def sign_up(self, email: str, password: str) -> AuthResult:
        self.sign_up_calls.append((email, password))
        return AuthResult(pending_confirmation=True)

    def confirm_sign_up(self, email: str, code: str) -> None:  # pragma: no cover
        return None


class _FakeInviteService:
    """Fake invite service recording which tokens reached invite handling.

    The public invite route calls ``resolve_by_token`` before rendering the
    form. Recording the token here proves the request reached invite handling
    rather than being bounced by the registration-mode gate (Property 9).
    """

    def __init__(self) -> None:
        self.resolved_tokens: list[str] = []

    def resolve_by_token(self, token: str) -> dict[str, Any]:
        self.resolved_tokens.append(token)
        return {"email": "invitee@example.com"}


def make_app(
    *,
    initial_mode: str | None = None,
    core: _FakeCoreTable | None = None,
    auth: _SpyCognitoAuth | None = None,
    invites: _FakeInviteService | None = None,
    with_store: bool = True,
):
    """Build a create_app() wired with the in-memory fakes.

    ``initial_mode`` seeds the global config item on the fake table so
    ``ConfigStore.get_global`` reports it. When ``with_store`` is False the app
    runs in no-datastore mode (``config_store`` is ``None``).
    """
    application = create_app(
        overrides={"TESTING": True, "SECRET_KEY": "t", "HELLODJ_STAGE": "beta"}
    )
    core = core if core is not None else _FakeCoreTable()
    if initial_mode is not None:
        core.items[(GLOBAL_CONFIG_PK, CONFIG_SK)] = {
            "PK": GLOBAL_CONFIG_PK,
            "SK": CONFIG_SK,
            "entityType": CONFIG_ENTITY_TYPE,
            "data": {CONFIG_KEY: initial_mode},
            "version": 1,
        }
    application.extensions["config_store"] = (
        ConfigStore(core) if with_store else None
    )
    application.extensions["cognito_auth"] = auth
    application.extensions["invite_service"] = invites
    return application


def login_admin(client) -> None:
    """Inject an authenticated admin session on the test client."""
    with client.session_transaction() as sess:
        sess["user"] = {"email": "owner@x.io", "sub": ADMIN_SUB, "is_admin": True}


def login_discord_non_admin(client) -> None:
    """Inject a Discord-authenticated non-admin session on the test client."""
    with client.session_transaction() as sess:
        sess["user"] = {
            "provider": "discord",
            "email": "user@x.io",
            "sub": "user-sub-1",
            "discord_id": "42",
            "is_admin": False,
        }
