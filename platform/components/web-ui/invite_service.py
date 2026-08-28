"""Email-invite flow for onboarding new users (Platform_Owner).

An invite mints an opaque, single-use, time-limited **Invite_Token**
(``secrets.token_urlsafe``). Only its SHA-256 hash is persisted; the raw token
travels solely in the invitation link (``<public-base>/invite/<token>``) and is
never logged or stored in plaintext (R7.4). The account itself is created later,
CONFIRMED, when the invitee registers via the link — not here — so this step
sends no Cognito temp-password email.

We record an Invite item in ``hellodj-core`` so the admin panel can list pending
vs accepted invites, and so the public ``/invite/<token>`` route can resolve an
invite by its hashed token in one indexed GSI1 lookup without knowing the email.

Data model (hellodj-core single table):

* ``PK=INVITE#<email>``  ``SK=INVITE``
  ``GSI1PK=INVITETOKEN#<tokenHash>``  ``GSI1SK=INVITE`` — data={email,
  invited_by, token_hash, expires_at, status (invited|accepted|expired|revoked),
  created_at}

On registration (``register``) the winning token consumer creates a CONFIRMED
Cognito account (``admin_create_user`` with ``MessageAction=SUPPRESS`` +
``admin_set_user_password(..., Permanent=True)``) so Cognito sends no email, and
persists the user's profile bound to the Cognito subject, recording
``invited_by`` (R2.2, R2.6).

Requirements: 1.1, 1.2, 1.3, 1.5, 2.2, 2.3, 2.5, 2.6, 7.4
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from collections.abc import Mapping
from typing import Any, Protocol

from hellodj_platform_logic.data_access import CoreTable
from hellodj_platform_logic.data_access.errors import (
    ConditionalCheckFailedError,
    OptimisticLockError,
)

import invite_admin
from invite_email import InviteEmailError, InviteEmailService
from user_profile import PROFILE_SK, UserProfileService, user_pk

__all__ = [
    "InviteService",
    "InviteError",
    "InviteConsumedError",
    "invite_pk",
    "token_gsi1pk",
    "hash_token",
    "INVITE_SK",
    "INVITE_INDEX_PK",
    "invite_index_sk",
    "DEFAULT_INVITE_TTL_SECONDS",
]

INVITE_SK = "INVITE"
INVITE_ENTITY = "Invite"

#: Shared partition holding one lightweight pointer item per invite so the
#: admin panel can enumerate every invite (the invite itself lives under a
#: per-email partition, and its GSI1 slot is taken by the token lookup, so a
#: dedicated index partition is the listing access pattern). Each pointer is
#: ``PK=INVITEINDEX`` ``SK=EMAIL#<email>`` and carries no secret material.
INVITE_INDEX_PK = "INVITEINDEX"

#: Default Invite_Token time-to-live: 7 days (R1.3), overridable per service.
DEFAULT_INVITE_TTL_SECONDS = 7 * 24 * 60 * 60


class InviteError(Exception):
    """Raised when an invite cannot be created (e.g. a duplicate email)."""


class InviteConsumedError(Exception):
    """Raised when a token cannot be consumed because it is invalid.

    Covers every non-consumable outcome — an unknown, already-accepted,
    revoked, or expired token — so the caller renders the single fixed
    "used or expired" message (R2.3). The message is deliberately generic and
    never echoes the raw token (R7.4).
    """


def invite_pk(email: str) -> str:
    """Return the partition key for an invite item, normalized to lowercase."""
    return f"INVITE#{email.strip().lower()}"


def hash_token(raw_token: str) -> str:
    """Return the hex SHA-256 hash of a raw Invite_Token.

    Only this hash is persisted; the raw token lives solely in the emailed
    link so it never appears in the datastore or logs (R7.4).
    """
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def token_gsi1pk(token_hash: str) -> str:
    """Return the GSI1 partition key used to resolve an invite by token hash."""
    return f"INVITETOKEN#{token_hash}"


def invite_index_sk(email: str) -> str:
    """Return the sort key for an invite's pointer item in the index partition."""
    return f"EMAIL#{email.strip().lower()}"


def _cognito_subject(created: Mapping[str, Any]) -> str | None:
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


class _NotConsumableError(Exception):
    """Internal signal from the consume mutator that a token is not usable.

    Raised inside the ``update_with_lock`` mutator when the current status is no
    longer ``invited`` (lost race) or the token expired mid-flow, so the outer
    ``consume`` can translate it into a public :class:`InviteConsumedError`
    without committing a write.
    """


class CognitoInviteClient(Protocol):
    """Subset of the boto3 ``cognito-idp`` client the invite flow uses."""

    def list_users(self, **kwargs: Any) -> dict[str, Any]: ...

    def admin_create_user(self, **kwargs: Any) -> dict[str, Any]: ...

    def admin_set_user_password(self, **kwargs: Any) -> dict[str, Any]: ...


class InviteService:
    """Mint single-use Invite_Tokens and track their status in ``hellodj-core``.

    The Cognito client detects an already-registered email at invite time
    (R1.5) and, at registration time, creates the CONFIRMED account
    (``admin_create_user`` + ``admin_set_user_password``) so Cognito sends no
    temp-password email. When a :class:`UserProfileService` is supplied the
    newly created account's profile is persisted bound to its Cognito subject
    (R2.2, R2.6).

    When an :class:`InviteEmailService` is supplied, :meth:`invite` sends the
    branded link and rolls the record back on send failure (R1.1); with no
    sender wired it degrades to recording the invite without an email.
    """

    def __init__(
        self,
        core_table: CoreTable,
        cognito_client: CognitoInviteClient,
        *,
        user_pool_id: str,
        token_ttl_seconds: int = DEFAULT_INVITE_TTL_SECONDS,
        user_profiles: UserProfileService | None = None,
        invite_email: InviteEmailService | None = None,
    ) -> None:
        if token_ttl_seconds <= 0:
            raise ValueError("token_ttl_seconds must be > 0")
        self._core = core_table
        self._cognito = cognito_client
        self._user_pool_id = user_pool_id
        self._token_ttl_seconds = token_ttl_seconds
        self._user_profiles = user_profiles
        self._invite_email = invite_email

    def invite(self, email: str, *, invited_by: str) -> dict[str, Any]:
        """Mint an Invite_Token for ``email`` and record the pending invite.

        Rejects duplicates (R1.5): if a pending invite already exists for the
        email, or the email is already registered in Cognito, no new invite is
        created and a clear :class:`InviteError` is raised.

        Returns the invite metadata plus the freshly minted ``raw_token`` so the
        caller (the email sender) can build the ``/invite/<token>`` link. The
        raw token is not persisted — only its hash (R7.4).
        """
        email_norm = email.strip().lower()
        if not email_norm or "@" not in email_norm:
            raise InviteError("a valid email is required")

        if self._is_registered(email_norm):
            raise InviteError(f"{email_norm} is already registered")

        existing = self._core.get(invite_pk(email_norm), INVITE_SK)
        if existing is not None and invite_admin.blocks_new_invite(existing):
            raise InviteError(f"{email_norm} already has a pending invite")
        # A stale record (terminal/expired/old-flow) does not block; clear it so
        # put_new succeeds and a deleted account can be re-invited (R1.5).
        if existing is not None:
            self._core.delete(invite_pk(email_norm), INVITE_SK)
            self._core.delete(INVITE_INDEX_PK, invite_index_sk(email_norm))

        raw_token = secrets.token_urlsafe(32)
        token_hash = hash_token(raw_token)
        now = int(time.time())
        expires_at = now + self._token_ttl_seconds
        data = {
            "email": email_norm,
            "invited_by": invited_by,
            "token_hash": token_hash,
            "expires_at": expires_at,
            "status": "invited",
            "created_at": now,
        }
        try:
            self._core.put_new(
                invite_pk(email_norm),
                INVITE_SK,
                INVITE_ENTITY,
                data,
                gsi1pk=token_gsi1pk(token_hash),
                gsi1sk=INVITE_SK,
            )
        except ConditionalCheckFailedError as error:
            # A concurrent invite for the same email won the create race.
            raise InviteError(
                f"{email_norm} already has a pending invite"
            ) from error

        # Register a pointer in the shared index partition so the admin panel
        # can enumerate this invite (see INVITE_INDEX_PK). Best-effort: an
        # existing pointer from a prior invite of the same email is fine — the
        # invite record itself is the source of truth read back during listing.
        self._put_index_pointer(email_norm)

        # Send the branded invitation email carrying the /invite/<raw_token>
        # link. If sending fails, roll back the just-created invite record so a
        # retry starts clean and no half-created invite lingers (R1.1). When no
        # email sender is wired, the invite is recorded without an email.
        if self._invite_email is not None:
            try:
                self._invite_email.send(email_norm, raw_token)
            except InviteEmailError as error:
                self._core.delete(invite_pk(email_norm), INVITE_SK)
                self._core.delete(INVITE_INDEX_PK, invite_index_sk(email_norm))
                raise InviteError(
                    f"could not send the invitation email to {email_norm}"
                ) from error

        return {
            "email": email_norm,
            "status": "invited",
            "expires_at": expires_at,
            "raw_token": raw_token,
        }

    def is_invited(self, email: str) -> bool:
        """Return whether a pending (``invited``) invite record exists."""
        item = self._core.get(invite_pk(email), INVITE_SK)
        return bool(item) and item.get("data", {}).get("status") == "invited"

    def list_invites(self) -> list[dict[str, Any]]:
        """Return every recorded invite with an effective display status (R1.4).

        Delegates to :func:`invite_admin.list_invites`; a stored ``invited``
        invite past ``expires_at`` is surfaced as ``expired`` without mutating
        the record, and rows are ordered newest first.
        """
        return invite_admin.list_invites(
            self._core,
            index_pk=INVITE_INDEX_PK,
            invite_pk=invite_pk,
            invite_sk=INVITE_SK,
        )

    def revoke(self, email: str) -> dict[str, Any]:
        """Revoke a pending invite so its single-use token can no longer be used.

        Delegates to :func:`invite_admin.revoke_invite`, which flips a still-
        ``invited`` invite to ``revoked`` (``resolve_by_token`` / ``consume``
        then reject it). A non-pending or unknown invite raises a clear
        :class:`InviteError` (R1.4).
        """
        try:
            return invite_admin.revoke_invite(
                self._core,
                invite_pk=invite_pk,
                invite_sk=INVITE_SK,
                email=email,
            )
        except invite_admin.RevokeError as error:
            raise InviteError(str(error)) from error

    def delete(self, email: str) -> dict[str, Any]:
        """Permanently delete an invite record (any status) and its pointer.

        Unlike :meth:`revoke` (which keeps a ``revoked`` row), this removes the
        invite entirely so it drops off :meth:`list_invites`; idempotent (R1.4).
        """
        return invite_admin.delete_invite(
            self._core,
            index_pk=INVITE_INDEX_PK,
            invite_pk=invite_pk,
            invite_sk=INVITE_SK,
            invite_index_sk=invite_index_sk,
            email=email,
        )

    def resend(self, email: str, *, invited_by: str) -> dict[str, Any]:
        """Re-send an invite by minting a fresh single-use token (R1.4).

        Deletes the prior invite record (whatever its status), then mints and
        sends a brand-new invite via :meth:`invite`, so the old token is
        invalidated and the invitee gets a working link. An already-registered
        email is rejected with a clear message (R1.5).
        """
        email_norm = email.strip().lower()
        if self._is_registered(email_norm):
            raise InviteError(f"{email_norm} is already registered")
        # Drop the prior record so invite()'s duplicate-pending guard passes and
        # the previous token stops resolving.
        self._core.delete(invite_pk(email_norm), INVITE_SK)
        return self.invite(email_norm, invited_by=invited_by)

    def _put_index_pointer(self, email_norm: str) -> None:
        """Record/refresh the invite's pointer in the shared index partition."""
        invite_admin.put_index_pointer(
            self._core, INVITE_INDEX_PK, invite_index_sk(email_norm), email_norm
        )

    def resolve_by_token(self, raw_token: str) -> dict[str, Any]:
        """Resolve a still-valid invite from its raw token, else raise.

        Hashes ``raw_token`` and resolves the invite via the GSI1 token index
        in one lookup, without knowing the email. Returns the invite's ``data``
        payload only when it exists, is still ``invited``, and has not expired
        (``expires_at`` in the future).

        Raises:
            InviteConsumedError: If the token is unknown, already consumed,
                revoked, or expired (R2.3). The error carries no token detail.
        """
        item = self._get_by_token(raw_token)
        if item is None:
            raise InviteConsumedError("invitation link has been used or has expired")
        data = item.get("data", {})
        if not self._is_valid(data):
            raise InviteConsumedError("invitation link has been used or has expired")
        return dict(data)

    def consume(self, raw_token: str) -> dict[str, Any]:
        """Atomically consume a valid token, flipping ``invited -> accepted``.

        Resolves the invite by token hash, then flips its status to
        ``accepted`` (stamping ``accepted_at``) via
        :meth:`CoreTable.update_with_lock`. The optimistic-lock ``version``
        condition guarantees single-use under concurrency (R2.5): of two racing
        callers exactly one observes ``invited`` and commits ``accepted``; the
        loser re-reads a non-``invited`` status and is rejected.

        Returns the consumed invite's ``data`` payload (now ``accepted``) so the
        caller can proceed to create the CONFIRMED Cognito account.

        Raises:
            InviteConsumedError: If the token is unknown, already consumed,
                revoked, or expired (R2.3). The error carries no token detail.
        """
        item = self._get_by_token(raw_token)
        if item is None:
            raise InviteConsumedError("invitation link has been used or has expired")
        pk, sk = item["PK"], item["SK"]

        def _accept(data: dict[str, Any]) -> dict[str, Any]:
            if not self._is_valid(data):
                # Someone else won the race, or the token expired mid-flow.
                raise _NotConsumableError
            return {
                **data,
                "status": "accepted",
                "accepted_at": int(time.time()),
            }

        try:
            committed = self._core.update_with_lock(pk, sk, _accept)
        except _NotConsumableError as error:
            raise InviteConsumedError(
                "invitation link has been used or has expired"
            ) from error
        except OptimisticLockError as error:
            # Persistent version contention: treat as a lost single-use race.
            raise InviteConsumedError(
                "invitation link has been used or has expired"
            ) from error
        return dict(committed["data"])

    def register(self, raw_token: str) -> dict[str, Any]:
        """Consume a valid token and create the CONFIRMED Cognito account.

        First :meth:`consume` flips the invite ``invited -> accepted``
        atomically (single-use, R2.5); no account is created unless the consume
        wins the race, so a bad/expired/used token raises
        :class:`InviteConsumedError` *before* any Cognito call (R2.3).

        The account is created with ``admin_create_user``
        (``MessageAction=SUPPRESS`` so Cognito emails nothing, a random UUID
        username, and a verified ``email`` attribute) then
        ``admin_set_user_password(..., Permanent=True)`` — a CONFIRMED account
        with no temporary password (R2.2). The user's profile is persisted
        bound to the Cognito subject, recording ``invited_by`` (R2.6).

        Returns the created account descriptor: ``email``, ``sub`` (Cognito
        subject), ``username``, and ``invited_by``. The registration step
        itself grants no session — the user links Discord next (R2.4).
        """
        invite = self.consume(raw_token)
        email = invite["email"]
        invited_by = invite.get("invited_by", "")

        username = str(uuid.uuid4())
        created = self._cognito.admin_create_user(
            UserPoolId=self._user_pool_id,
            Username=username,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
        )
        self._cognito.admin_set_user_password(
            UserPoolId=self._user_pool_id,
            Username=username,
            Password=secrets.token_urlsafe(24),
            Permanent=True,
        )

        sub = _cognito_subject(created) or username
        if self._user_profiles is not None:
            self._user_profiles.ensure(sub, email=email)
            self._record_invited_by(sub, invited_by)
        return {
            "email": email,
            "sub": sub,
            "username": username,
            "invited_by": invited_by,
        }

    def _record_invited_by(self, sub: str, invited_by: str) -> None:
        """Stamp ``invited_by`` onto the just-ensured profile, best-effort."""
        if not invited_by:
            return
        try:
            self._core.update_with_lock(
                user_pk(sub),
                PROFILE_SK,
                lambda data: {**data, "invited_by": invited_by},
            )
        except OptimisticLockError:
            # A concurrent profile write won; invited_by is non-critical.
            return

    def _get_by_token(self, raw_token: str) -> dict[str, Any] | None:
        """Resolve the raw invite item by token hash via GSI1, or ``None``."""
        token_hash = hash_token(raw_token)
        rows = self._core.query_gsi1(
            token_gsi1pk(token_hash), sk_prefix=INVITE_SK
        )
        return rows[0] if rows else None

    def _is_valid(self, data: Mapping[str, Any]) -> bool:
        """Return whether an invite payload is ``invited`` and unexpired.

        Expiry uses :func:`invite_admin.is_unexpired` (accepts the ``Decimal``
        live DynamoDB returns for Number attrs).
        """
        return data.get("status") == "invited" and invite_admin.is_unexpired(
            data.get("expires_at")
        )

    def _is_registered(self, email_norm: str) -> bool:
        """Return whether a Cognito account already exists for ``email``.

        Uses a filtered ``list_users`` query on the ``email`` attribute. A
        lookup failure degrades to ``False`` (treat as not-registered) so a
        transient Cognito error never silently blocks all invites.
        """
        try:
            resp = self._cognito.list_users(
                UserPoolId=self._user_pool_id,
                Filter=f'email = "{email_norm}"',
                Limit=1,
            )
        except Exception:  # noqa: BLE001 - degrade to "not registered"
            return False
        return bool(resp.get("Users"))
