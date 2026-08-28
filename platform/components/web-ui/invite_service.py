"""Email-invite flow for onboarding new users (Platform_Owner).

An invite uses Cognito's built-in invitation: ``admin_create_user`` creates the
account and Cognito emails a temporary password. The user then verifies and sets
a permanent password (FORCE_CHANGE_PASSWORD) on first login. We additionally
record an Invite item in ``hellodj-core`` so the admin panel can list pending vs
accepted invites and reject duplicates.

Data model: ``PK=INVITE#<email>``, ``SK=INVITE``, data={invited_by, status}.

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5
"""

from __future__ import annotations

from typing import Any, Protocol

from hellodj_platform_logic.data_access import CoreTable

__all__ = ["InviteService", "InviteError", "invite_pk", "INVITE_SK"]

INVITE_SK = "INVITE"
INVITE_ENTITY = "Invite"


class InviteError(Exception):
    """Raised when an invite cannot be created (e.g. a duplicate email)."""


def invite_pk(email: str) -> str:
    """Return the partition key for an invite item, normalized to lowercase."""
    return f"INVITE#{email.strip().lower()}"


class CognitoInviteClient(Protocol):
    """Subset of the boto3 ``cognito-idp`` client the invite flow uses."""

    def admin_create_user(self, **kwargs: Any) -> dict[str, Any]: ...


class InviteService:
    """Create Cognito-backed email invites and track their status."""

    def __init__(
        self,
        core_table: CoreTable,
        cognito_client: CognitoInviteClient,
        *,
        user_pool_id: str,
    ) -> None:
        self._core = core_table
        self._cognito = cognito_client
        self._user_pool_id = user_pool_id

    def invite(self, email: str, *, invited_by: str) -> dict[str, Any]:
        """Invite a user by email; Cognito sends the invitation email.

        Rejects a duplicate email (R1.5). Records the invite for the admin
        panel (R1.2). Cognito's ``admin_create_user`` (default DELIVERY via
        email) sends the temporary password and puts the account in
        FORCE_CHANGE_PASSWORD until the user sets a permanent password (R1.1,
        R1.3).
        """
        email_norm = email.strip().lower()
        if not email_norm or "@" not in email_norm:
            raise InviteError("a valid email is required")
        if self._core.get(invite_pk(email_norm), INVITE_SK) is not None:
            raise InviteError(f"{email_norm} has already been invited")

        self._cognito.admin_create_user(
            UserPoolId=self._user_pool_id,
            Username=email_norm,
            UserAttributes=[
                {"Name": "email", "Value": email_norm},
                {"Name": "email_verified", "Value": "false"},
            ],
            DesiredDeliveryMediums=["EMAIL"],
        )
        self._core.put_new(
            invite_pk(email_norm),
            INVITE_SK,
            INVITE_ENTITY,
            {"email": email_norm, "invited_by": invited_by, "status": "invited"},
        )
        return {"email": email_norm, "status": "invited"}

    def is_invited(self, email: str) -> bool:
        """Return whether an invite record exists for ``email``.

        Account status (invited vs verified) is read live from Cognito by the
        admin panel's ``AdminDirectory`` (FORCE_CHANGE_PASSWORD = invited,
        CONFIRMED = verified), so the invite record here is used only for
        duplicate detection (R1.5) rather than as the status source of truth.
        """
        return self._core.get(invite_pk(email), INVITE_SK) is not None
