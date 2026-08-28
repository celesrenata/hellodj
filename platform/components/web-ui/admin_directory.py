"""Cognito-backed user directory for the admin panel.

An administrator account is not a standard user account: it administers **all**
other accounts. This module wraps the Cognito ``cognito-idp`` admin APIs the
web-ui admin panel uses to:

* list every user in the pool with their status, enabled flag, email, and
  whether they are in the ``admins`` group;
* promote a user into / demote a user out of the ``admins`` group;
* disable / enable an account.

The AWS client is injectable so the flow is unit-testable without AWS, and the
web-ui runs in a degraded (no-directory) mode when Cognito isn't configured —
the admin routes then render an empty directory rather than erroring.

Requirements: 8.2 (admin auth manages accounts), 6.5 (web admin UI).
"""

from __future__ import annotations

import os
from typing import Any, Protocol

__all__ = ["AdminDirectory", "CognitoClient", "build_admin_directory"]

#: The Cognito group that marks an account as an administrator.
ADMIN_GROUP = "admins"


class CognitoClient(Protocol):
    """The subset of the boto3 ``cognito-idp`` client the directory uses."""

    def list_users(self, **kwargs: Any) -> dict[str, Any]: ...

    def admin_list_groups_for_user(self, **kwargs: Any) -> dict[str, Any]: ...

    def admin_add_user_to_group(self, **kwargs: Any) -> dict[str, Any]: ...

    def admin_remove_user_from_group(self, **kwargs: Any) -> dict[str, Any]: ...

    def admin_enable_user(self, **kwargs: Any) -> dict[str, Any]: ...

    def admin_disable_user(self, **kwargs: Any) -> dict[str, Any]: ...

    def admin_delete_user(self, **kwargs: Any) -> dict[str, Any]: ...


class AdminDirectory:
    """Manage all platform accounts through the Cognito user pool."""

    def __init__(self, client: CognitoClient, user_pool_id: str) -> None:
        self._client = client
        self._user_pool_id = user_pool_id

    def list_users(self) -> list[dict[str, Any]]:
        """Return every account with email, status, enabled flag, admin flag."""
        rows: list[dict[str, Any]] = []
        pagination: dict[str, Any] = {}
        while True:
            resp = self._client.list_users(
                UserPoolId=self._user_pool_id, Limit=60, **pagination
            )
            for user in resp.get("Users", []):
                rows.append(self._row(user))
            token = resp.get("PaginationToken")
            if not token:
                break
            pagination = {"PaginationToken": token}
        rows.sort(key=lambda r: r["username"].lower())
        return rows

    def set_admin(self, username: str, make_admin: bool) -> None:
        """Add the user to / remove the user from the ``admins`` group."""
        if make_admin:
            self._client.admin_add_user_to_group(
                UserPoolId=self._user_pool_id,
                Username=username,
                GroupName=ADMIN_GROUP,
            )
        else:
            self._client.admin_remove_user_from_group(
                UserPoolId=self._user_pool_id,
                Username=username,
                GroupName=ADMIN_GROUP,
            )

    def set_enabled(self, username: str, enabled: bool) -> None:
        """Enable or disable the account."""
        if enabled:
            self._client.admin_enable_user(
                UserPoolId=self._user_pool_id, Username=username
            )
        else:
            self._client.admin_disable_user(
                UserPoolId=self._user_pool_id, Username=username
            )

    def delete_user(self, username: str) -> None:
        """Permanently delete the account from the Cognito user pool.

        Unlike :meth:`set_enabled` (a reversible disable), this removes the
        account outright. Irreversible: the user is gone from Cognito and must
        be re-invited to return. Group membership is dropped automatically with
        the user.
        """
        self._client.admin_delete_user(
            UserPoolId=self._user_pool_id, Username=username
        )

    def _row(self, user: dict[str, Any]) -> dict[str, Any]:
        """Normalize a Cognito user object into a template-friendly row.

        The pool creates invited accounts with an opaque UUID ``Username`` and
        stores the name the invitee picked as the ``preferred_username``
        attribute. The raw ``Username`` is therefore an internal id, not a
        display name — prefer ``preferred_username`` (then ``email``) for the
        shown name, and only fall back to the raw ``Username`` when neither
        attribute exists. The stable Cognito ``sub`` attribute is surfaced as
        ``sub`` so per-user entitlements (keyed by subject, spanning web-ui and
        bot) can be linked from the picker. ``login`` keeps the raw Cognito
        ``Username`` for the admin actions that address the account by it
        (enable/disable, group membership, delete).
        """
        login = user.get("Username", "")
        attrs = {
            a["Name"]: a["Value"] for a in user.get("Attributes", [])
        }
        display = attrs.get("preferred_username") or attrs.get("email") or login
        return {
            "username": display,
            "login": login,
            "sub": attrs.get("sub", ""),
            "email": attrs.get("email", ""),
            "status": user.get("UserStatus", ""),
            "enabled": bool(user.get("Enabled", True)),
            "is_admin": self._is_admin(login),
        }

    def _is_admin(self, username: str) -> bool:
        """Return whether the user is in the ``admins`` group."""
        try:
            resp = self._client.admin_list_groups_for_user(
                UserPoolId=self._user_pool_id, Username=username
            )
        except Exception:  # noqa: BLE001 - treat lookup failure as non-admin
            return False
        return any(
            g.get("GroupName") == ADMIN_GROUP for g in resp.get("Groups", [])
        )


def build_admin_directory() -> AdminDirectory | None:
    """Construct an :class:`AdminDirectory` from the environment.

    Returns ``None`` (degraded mode — empty directory) when the user pool id or
    boto3 is unavailable, so the web-ui still renders without a live Cognito
    backend (e.g. in tests or a partial deploy).
    """
    user_pool_id = os.getenv("HELLODJ_COGNITO_USER_POOL_ID", "")
    if not user_pool_id:
        return None
    try:
        import boto3

        client = boto3.client(
            "cognito-idp", region_name=os.getenv("AWS_REGION", "us-east-1")
        )
    except Exception:  # noqa: BLE001
        return None
    return AdminDirectory(client, user_pool_id)
