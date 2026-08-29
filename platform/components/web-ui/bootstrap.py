"""Runtime service bootstrap for the web-ui.

Builds the DynamoDB-backed :class:`CoreTable` and the services layered on it
(config, user profiles, guild admin, per-guild sources, invites) from the
environment. Every builder degrades to ``None`` when its backing resource isn't
configured, so the app still imports and renders in tests / partial deploys
without AWS credentials.

Env:
* ``HELLODJ_CORE_TABLE``           DynamoDB table name (hellodj-core).
* ``HELLODJ_COGNITO_USER_POOL_ID`` Cognito pool for invites/admin.
* ``HELLODJ_STAGE``                Stage for per-guild secret naming.
* ``AWS_REGION``                   Region for boto3 clients.
* ``INVITE_SENDER``                Verified SES sender identity for invites.
* ``HELLODJ_PUBLIC_BASE_URL``      Site origin for the ``/invite/<token>`` link.
* ``HELLODJ_ASSETS_BUCKET``        S3 bucket for per-guild bot-avatar bytes.
* ``HELLODJ_SOURCE_CREDS_KMS_KEY_ID`` CMK for envelope-encrypting source tokens.
"""

from __future__ import annotations

import os
from typing import Any

from account_admin_service import AccountAdminService
from bot_app_pool import BotAppAssignmentService, BotAppPool
from bot_identity import BotIdentityService
from config_store import ConfigStore
from entitlement_service import EntitlementService
from guild_activation_service import GuildActivationService
from guild_admin_service import GuildAdminService
from guild_sources import GuildSourcesService
from invite_email import InviteEmailService
from invite_service import InviteService
from source_credential_service import SourceCredentialService
from user_profile import UserProfileService

__all__ = ["build_services"]


def _core_table() -> Any | None:
    """Build a CoreTable from HELLODJ_CORE_TABLE, or None in degraded mode."""
    table_name = os.getenv("HELLODJ_CORE_TABLE", "")
    if not table_name:
        return None
    try:
        import boto3
        from hellodj_platform_logic.data_access import CoreTable

        ddb = boto3.resource(
            "dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1")
        )
        return CoreTable(ddb.Table(table_name))
    except Exception:  # noqa: BLE001 - degrade to no datastore
        return None


def _cognito_client() -> Any | None:
    """Build a cognito-idp client, or None when boto3 is unavailable."""
    try:
        import boto3

        return boto3.client(
            "cognito-idp", region_name=os.getenv("AWS_REGION", "us-east-1")
        )
    except Exception:  # noqa: BLE001
        return None


def _secrets_client() -> Any | None:
    """Build a secretsmanager client, or None when boto3 is unavailable."""
    try:
        import boto3

        return boto3.client(
            "secretsmanager", region_name=os.getenv("AWS_REGION", "us-east-1")
        )
    except Exception:  # noqa: BLE001
        return None


def _kms_client() -> Any | None:
    """Build a KMS client, or None when boto3 is unavailable."""
    try:
        import boto3

        return boto3.client(
            "kms", region_name=os.getenv("AWS_REGION", "us-east-1")
        )
    except Exception:  # noqa: BLE001
        return None


def _ses_client() -> Any | None:
    """Build an SES client, or None when boto3 is unavailable."""
    try:
        import boto3

        return boto3.client(
            "ses", region_name=os.getenv("AWS_REGION", "us-east-1")
        )
    except Exception:  # noqa: BLE001
        return None


def _s3_client() -> Any | None:
    """Build an S3 client, or None when boto3 is unavailable."""
    try:
        import boto3

        return boto3.client(
            "s3", region_name=os.getenv("AWS_REGION", "us-east-1")
        )
    except Exception:  # noqa: BLE001
        return None


def _invite_email() -> InviteEmailService | None:
    """Build the branded SES invitation sender, or None when unconfigured.

    Returns ``None`` unless both a verified sender identity (``INVITE_SENDER``)
    and boto3/SES are available, so the invite flow degrades to record-only.
    """
    sender = os.getenv("INVITE_SENDER", "").strip()
    if not sender:
        return None
    ses = _ses_client()
    if ses is None:
        return None
    public_base_url = os.getenv("HELLODJ_PUBLIC_BASE_URL", "")
    return InviteEmailService(
        ses, sender=sender, public_base_url=public_base_url
    )


def build_services() -> dict[str, Any]:
    """Return the runtime services keyed for ``app.extensions``.

    Keys: ``config_store``, ``user_profiles``, ``guild_admin``,
    ``account_admin``, ``guild_sources``, ``source_credentials``,
    ``guild_identity_service``, ``invite_service``, ``entitlement_service``.
    Any service whose backing resource is unavailable is ``None`` and the routes
    degrade gracefully.
    """
    core = _core_table()
    stage = os.getenv("HELLODJ_STAGE", "beta")
    pool_id = os.getenv("HELLODJ_COGNITO_USER_POOL_ID", "")

    services: dict[str, Any] = {
        "config_store": None,
        "user_profiles": None,
        "guild_admin": None,
        "account_admin": None,
        "guild_activation": None,
        "guild_sources": None,
        "source_credentials": None,
        "guild_identity_service": None,
        "invite_service": None,
        "entitlement_service": None,
        "bot_app_assignment": None,
    }
    if core is None:
        return services

    user_profiles = UserProfileService(core)
    services["config_store"] = ConfigStore(core)
    services["user_profiles"] = user_profiles
    services["guild_admin"] = GuildAdminService(core)
    services["account_admin"] = AccountAdminService(core)
    services["guild_activation"] = GuildActivationService(core)
    services["entitlement_service"] = EntitlementService(core)

    secrets = _secrets_client()
    if secrets is not None:
        services["guild_sources"] = GuildSourcesService(
            core, secrets, stage=stage
        )
        # Global Discord bot-application pool (Secrets Manager) + per-guild
        # claim/assignment for multi-bot playback + invite links. The Primary
        # bot application (DISCORD_CLIENT_ID) is excluded from the pool so it is
        # never handed out as an assignable secondary.
        services["bot_app_assignment"] = BotAppAssignmentService(
            core,
            BotAppPool(
                secrets,
                stage=stage,
                primary_client_id=os.getenv("DISCORD_CLIENT_ID", ""),
            ),
        )

    # Unified per-user source-credential store (encrypted DynamoDB). Requires
    # both a KMS client and a CMK id; degrades to None (like every other
    # service) when either is unconfigured so new writes simply don't happen
    # and the legacy per-guild secret path remains the fallback (R2.6).
    kms = _kms_client()
    kms_key_id = os.getenv("HELLODJ_SOURCE_CREDS_KMS_KEY_ID", "").strip()
    if kms is not None and kms_key_id:
        services["source_credentials"] = SourceCredentialService(
            core, kms, kms_key_id
        )

    s3 = _s3_client()
    avatar_bucket = os.getenv("HELLODJ_ASSETS_BUCKET", "").strip()
    if s3 is not None and avatar_bucket:
        services["guild_identity_service"] = BotIdentityService(
            core, s3, stage=stage, avatar_bucket=avatar_bucket
        )

    cognito = _cognito_client()
    if cognito is not None and pool_id:
        services["invite_service"] = InviteService(
            core,
            cognito,
            user_pool_id=pool_id,
            token_ttl_seconds=_invite_ttl_seconds(),
            user_profiles=user_profiles,
            invite_email=_invite_email(),
        )
    return services


def _invite_ttl_seconds() -> int:
    """Return the Invite_Token TTL from env, defaulting to 7 days."""
    from invite_service import DEFAULT_INVITE_TTL_SECONDS

    raw = os.getenv("INVITE_TOKEN_TTL", "").strip()
    if not raw:
        return DEFAULT_INVITE_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_INVITE_TTL_SECONDS
    return value if value > 0 else DEFAULT_INVITE_TTL_SECONDS
