"""Password policy + chosen-username rules for the invite registration flow.

Split out of :mod:`invite_service` so that module stays under the per-file line
ceiling (R13.3) and so the SAME policy drives both the server-side guard in
``InviteService.register`` and the live client-side checklist rendered on the
registration page (the template reads :data:`PASSWORD_RULES` and re-implements
each rule's regex in Alpine so the user watches requirements check off as they
type — the display and the enforcement never drift).

The password policy MIRRORS the Cognito user-pool policy (min length 12 +
uppercase + lowercase + number + symbol). Cognito enforces it authoritatively at
``admin_set_user_password``; we validate first only to surface a clean,
enumerated error instead of a raw Cognito ``InvalidPasswordException``.

The chosen display name is stored as the Cognito ``preferred_username``
attribute (the immutable account ``Username`` stays an opaque UUID). Because
``preferred_username`` is not a pool alias attribute, Cognito does not itself
enforce uniqueness, so :func:`username_taken` checks availability with a
filtered ``list_users`` lookup — best-effort for the live "as you type" hint,
re-checked at registration time.

Requirements: 2.2
"""

from __future__ import annotations

import re
from typing import Any, Protocol

__all__ = [
    "PASSWORD_MIN_LENGTH",
    "PASSWORD_RULES",
    "PasswordPolicyError",
    "UsernamePolicyError",
    "validate_password",
    "normalize_username",
    "validate_username",
    "username_taken",
    "UsernameLookupClient",
]

#: Minimum password length — mirrors the Cognito pool ``MinimumLength``.
PASSWORD_MIN_LENGTH = 12

#: The ordered password requirements. Each is ``(id, label, predicate)`` where
#: ``predicate(password) -> bool``. ``id`` is the stable key the template's
#: Alpine checklist uses; ``label`` is the human copy shown next to each check.
#: Keep these in lockstep with the Cognito pool password policy and with the
#: regexes re-implemented in ``invite_register.html`` (same ids).
PASSWORD_RULES: list[tuple[str, str, Any]] = [
    ("length", f"At least {PASSWORD_MIN_LENGTH} characters",
     lambda p: len(p) >= PASSWORD_MIN_LENGTH),
    ("upper", "An uppercase letter", lambda p: bool(re.search(r"[A-Z]", p))),
    ("lower", "A lowercase letter", lambda p: bool(re.search(r"[a-z]", p))),
    ("number", "A number", lambda p: bool(re.search(r"[0-9]", p))),
    ("symbol", "A symbol", lambda p: bool(re.search(r"[^A-Za-z0-9]", p))),
]

#: Username: 3–32 chars, letters/numbers/._- , must start with a letter/number.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")


class PasswordPolicyError(Exception):
    """Raised when a chosen password fails one or more policy rules.

    Carries the ordered list of *unmet* rule labels so the caller can surface a
    clean, enumerated message (never a raw Cognito exception).
    """

    def __init__(self, unmet: list[str]) -> None:
        self.unmet = unmet
        super().__init__("password does not meet the requirements: " + ", ".join(unmet))


class UsernamePolicyError(Exception):
    """Raised when a chosen username is malformed or already taken."""


class UsernameLookupClient(Protocol):
    """Subset of the boto3 ``cognito-idp`` client used for availability."""

    def list_users(self, **kwargs: Any) -> dict[str, Any]: ...


def validate_password(password: str) -> None:
    """Validate ``password`` against :data:`PASSWORD_RULES`.

    Raises:
        PasswordPolicyError: If any rule is unmet, listing every failing rule.
    """
    unmet = [label for _id, label, ok in PASSWORD_RULES if not ok(password)]
    if unmet:
        raise PasswordPolicyError(unmet)


def normalize_username(username: str) -> str:
    """Return the trimmed username (case preserved; comparisons are lowercased)."""
    return (username or "").strip()


def validate_username(username: str) -> str:
    """Return the normalized username, or raise if it is malformed.

    Raises:
        UsernamePolicyError: If the username does not match the allowed shape
            (3–32 chars, alphanumerics plus ``. _ -``, leading alphanumeric).
    """
    name = normalize_username(username)
    if not _USERNAME_RE.match(name):
        raise UsernamePolicyError(
            "Username must be 3–32 characters: letters, numbers, and . _ - "
            "(starting with a letter or number)."
        )
    return name


def username_taken(
    client: UsernameLookupClient,
    *,
    user_pool_id: str,
    username: str,
) -> bool:
    """Return whether ``username`` is already used as a ``preferred_username``.

    Best-effort: a filtered ``list_users`` lookup. A lookup failure degrades to
    ``False`` (treat as available) so a transient Cognito error never blocks the
    live hint; registration re-validates authoritatively.
    """
    name = normalize_username(username)
    if not name:
        return False
    try:
        resp = client.list_users(
            UserPoolId=user_pool_id,
            Filter=f'preferred_username = "{name}"',
            Limit=1,
        )
    except Exception:  # noqa: BLE001 - degrade to "available"
        return False
    return bool(resp.get("Users"))
