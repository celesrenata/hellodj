"""Cognito seeding for the migrated admin bootstrap credential.

The only data carried forward by the clean-slate migration is the
``Admin_Bootstrap_Credential`` (R19.1). This module seeds that single credential
into the Cognito user pool and adds the user to the ``admins`` group so the
Platform_Owner can authenticate as the administrator for the first time on AWS
through Cognito (R19.3).

The Cognito client is injectable (a ``boto3`` ``cognito-idp`` client by default,
imported lazily) so the seeding flow is unit-testable with a mock client and the
module imports without AWS libraries present. Seeding is idempotent: if the user
or group membership already exists the seeder treats it as success so re-running
the one-time Job is safe.

Requirements: 19.1, 19.3
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from hellodj_platform_logic.types import LegacyRecord, LegacyRecordType

__all__ = [
    "AdminBootstrapCredential",
    "CognitoClient",
    "CognitoAdminSeeder",
    "build_cognito_client",
    "DEFAULT_ADMIN_GROUP",
]

#: The Cognito group the migrated bootstrap user is added to (R19.3).
DEFAULT_ADMIN_GROUP = "admins"


@dataclass(frozen=True)
class AdminBootstrapCredential:
    """The minimal admin credential parsed from the migrated legacy record.

    Attributes:
        username: The Cognito username to create for the Platform_Owner.
        email: Optional email attribute seeded on the user (used for recovery).
    """

    username: str
    email: str | None = None

    @classmethod
    def from_record(cls, record: LegacyRecord) -> AdminBootstrapCredential:
        """Build a credential from a migrated legacy record.

        The record's ``payload`` is treated as an opaque legacy blob that may be
        JSON with ``username``/``email`` fields. When the payload is absent or
        not JSON, the record's ``record_id`` is used as the username so a
        minimal legacy export still yields a usable bootstrap user.

        Args:
            record: A legacy record whose ``record_type`` must be
                :attr:`~hellodj_platform_logic.types.LegacyRecordType.ADMIN_BOOTSTRAP_CREDENTIAL`.

        Returns:
            The parsed :class:`AdminBootstrapCredential`.

        Raises:
            ValueError: If the record is not an admin bootstrap credential or no
                username can be derived from it.
        """
        if record.record_type is not LegacyRecordType.ADMIN_BOOTSTRAP_CREDENTIAL:
            raise ValueError(
                "record is not an ADMIN_BOOTSTRAP_CREDENTIAL: "
                f"{record.record_type!r}"
            )

        username = record.record_id
        email: str | None = None
        if record.payload:
            try:
                parsed = json.loads(record.payload)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                username = str(parsed.get("username") or username)
                raw_email = parsed.get("email")
                email = str(raw_email) if raw_email else None

        username = username.strip()
        if not username:
            raise ValueError(
                "admin bootstrap credential has no username (empty record_id "
                "and no 'username' in payload)"
            )
        return cls(username=username, email=email)


class CognitoClient(Protocol):
    """Minimal subset of the boto3 ``cognito-idp`` client interface."""

    def admin_create_user(self, **kwargs: Any) -> dict[str, Any]:
        """Create (or surface an existing) user in the pool."""
        ...

    def admin_add_user_to_group(self, **kwargs: Any) -> dict[str, Any]:
        """Add a user to a group in the pool."""
        ...


def build_cognito_client(region_name: str | None = None) -> CognitoClient:
    """Create a real boto3 ``cognito-idp`` client (imported lazily)."""
    import boto3

    return boto3.client("cognito-idp", region_name=region_name)


def _is_already_exists_error(error: Exception) -> bool:
    """Return True when a boto3 error means the resource already exists.

    Detected by the ``UsernameExistsException`` error code without importing
    botocore, so seeding stays idempotent when the one-time Job is re-run.
    """
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if code == "UsernameExistsException":
            return True
    return type(error).__name__ == "UsernameExistsException"


class CognitoAdminSeeder:
    """Seeds the migrated admin bootstrap credential into Cognito.

    Args:
        user_pool_id: The Cognito user pool id to seed the admin into.
        client: An injected ``cognito-idp`` client. Defaults to a real boto3
            client created via :func:`build_cognito_client`.
        admin_group: The group the bootstrap user is added to (default
            :data:`DEFAULT_ADMIN_GROUP`).
        region_name: Region used when creating the default client.
    """

    def __init__(
        self,
        user_pool_id: str,
        *,
        client: CognitoClient | None = None,
        admin_group: str = DEFAULT_ADMIN_GROUP,
        region_name: str | None = None,
    ) -> None:
        if not user_pool_id:
            raise ValueError("user_pool_id is required")
        self._user_pool_id = user_pool_id
        self._admin_group = admin_group
        self._client = client or build_cognito_client(region_name)

    def _build_attributes(
        self, credential: AdminBootstrapCredential
    ) -> list[dict[str, str]]:
        """Build the Cognito user attribute list for the bootstrap user."""
        attributes: list[dict[str, str]] = []
        if credential.email:
            attributes.append({"Name": "email", "Value": credential.email})
            attributes.append({"Name": "email_verified", "Value": "true"})
        return attributes

    def seed(self, credential: AdminBootstrapCredential) -> None:
        """Create the bootstrap admin user and add it to the admin group.

        The operation is idempotent: an already-existing user is treated as
        success and group membership is (re)asserted regardless, so re-running
        the migration Job does not fail (R19.3).

        Args:
            credential: The parsed admin bootstrap credential to seed.
        """
        create_kwargs: dict[str, Any] = {
            "UserPoolId": self._user_pool_id,
            "Username": credential.username,
            # The Platform_Owner completes their own password on first login;
            # suppress the default Cognito invitation email during migration.
            "MessageAction": "SUPPRESS",
        }
        attributes = self._build_attributes(credential)
        if attributes:
            create_kwargs["UserAttributes"] = attributes

        try:
            self._client.admin_create_user(**create_kwargs)
        except Exception as error:  # noqa: BLE001 - idempotent re-run handling
            if not _is_already_exists_error(error):
                raise

        self._client.admin_add_user_to_group(
            UserPoolId=self._user_pool_id,
            Username=credential.username,
            GroupName=self._admin_group,
        )
