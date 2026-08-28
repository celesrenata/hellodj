"""Cognito account creation for the invite-registration flow.

Extracted from :mod:`invite_service` (keeping both under the per-file line
ceiling, R13.3) so the account-minting mechanics live in one focused, unit-
testable place. :func:`create_confirmed_account` performs the Cognito side of
``InviteService.register`` AFTER the single-use token has been consumed:

* uses the invitee's **chosen name as the Cognito ``Username``** so they can log
  in with it (the pool signs in by ``username``); this is required because the
  pool's ``AliasAttributes`` are IMMUTABLE — ``preferred_username`` cannot be
  made a sign-in alias on the existing pool without replacing it (and losing all
  users). When no name is chosen (Discord-only login) a random UUID username is
  used instead. Cognito rejects a ``Username`` shaped like an email when the
  pool has an email alias, but ``register_policy`` already forbids that shape;
* sets ``email`` verified and mirrors the chosen name into
  ``preferred_username`` for display (R2.2);
* sets the password ``Permanent=True`` (a random one when omitted),
  ``MessageAction=SUPPRESS`` so Cognito sends no email;
* maps a create-time ``UsernameExistsException`` (same-instant name race) to
  :class:`UsernameTakenError`.

The chosen name's availability is pre-checked (before the token is consumed) by
:class:`InviteService`; this module owns only the authoritative create.

Requirements: 2.2, 2.6
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

__all__ = [
    "UsernameTakenError",
    "CognitoAccountClient",
    "create_confirmed_account",
    "cognito_subject",
]


class UsernameTakenError(Exception):
    """Raised when the chosen name is already in use (R2.2).

    The chosen name becomes the Cognito ``Username``, so a duplicate is rejected
    authoritatively at ``admin_create_user`` with ``UsernameExistsException``.
    :class:`InviteService` pre-checks availability and raises this BEFORE
    consuming the token so the invitee can pick another name and retry on the
    same link; the create-time catch here is a last-resort for a same-instant
    race (the token is already consumed in that rare case).
    """


class CognitoAccountClient(Protocol):
    """Subset of the boto3 ``cognito-idp`` client the account create uses."""

    def admin_create_user(self, **kwargs: Any) -> dict[str, Any]: ...

    def admin_set_user_password(self, **kwargs: Any) -> dict[str, Any]: ...


def _error_code(error: Exception) -> str:
    """Extract the Cognito error code from a botocore ClientError-shaped exc."""
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if isinstance(code, str):
            return code
    return type(error).__name__


def cognito_subject(created: Mapping[str, Any]) -> str | None:
    """Extract the Cognito ``sub`` from an ``admin_create_user`` response.

    The subject is the stable account identifier the user profile is bound to.
    Returns ``None`` when the response omits it (e.g. a minimal fake), letting
    the caller fall back to the username.
    """
    attributes = created.get("User", {}).get("Attributes", [])
    for attribute in attributes:
        if attribute.get("Name") == "sub":
            return attribute.get("Value")
    return None


def create_confirmed_account(
    cognito: CognitoAccountClient,
    *,
    user_pool_id: str,
    email: str,
    chosen_name: str,
    password: str | None,
) -> tuple[str, str]:
    """Create a CONFIRMED Cognito account and return ``(username, sub)``.

    ``username`` is the invitee's ``chosen_name`` (so they can sign in with it)
    or a freshly minted opaque UUID when no name is chosen. ``email`` is created
    verified and the chosen name is mirrored into ``preferred_username`` for
    display. The account is created silently (``MessageAction=SUPPRESS``) with a
    permanent password (a random one when ``password`` is ``None``).

    Raises:
        UsernameTakenError: If Cognito rejects the ``Username`` as already in
            use (``UsernameExistsException``).
    """
    username = chosen_name or str(uuid.uuid4())
    attributes = [
        {"Name": "email", "Value": email},
        {"Name": "email_verified", "Value": "true"},
    ]
    if chosen_name:
        attributes.append({"Name": "preferred_username", "Value": chosen_name})
    try:
        created = cognito.admin_create_user(
            UserPoolId=user_pool_id,
            Username=username,
            MessageAction="SUPPRESS",
            UserAttributes=attributes,
        )
    except Exception as error:  # noqa: BLE001 - narrow on Cognito code
        if _error_code(error) == "UsernameExistsException":
            raise UsernameTakenError(
                f"the name {chosen_name!r} is already taken"
            ) from error
        raise
    cognito.admin_set_user_password(
        UserPoolId=user_pool_id,
        Username=username,
        Password=password or secrets.token_urlsafe(24),
        Permanent=True,
    )
    return username, cognito_subject(created) or username
