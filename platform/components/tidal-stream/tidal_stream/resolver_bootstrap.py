"""Wire the real unified-store resolver + owner lookup for the sidecar.

Constructs the shared
:class:`~hellodj_platform_logic.user_credential_resolver.UserCredentialResolver`
and its guild→owner :class:`~hellodj_platform_logic.user_credential_resolver.OwnerLookup`
over a live ``CoreTable`` + ``token_crypto`` decrypt seam (multi-tenant-source-
streaming task 3.1). ``boto3`` and ``hellodj_platform_logic.data_access`` /
``token_crypto`` are imported lazily so this module stays import-safe in
environments without AWS libraries and unit-testable with in-memory fakes
(mirrors the bot's ``guild_credentials.build_dynamo_credential_resolver``).

The reader is READ-ONLY on tokens: it holds a table read grant + KMS Decrypt-only
(R2.1, R9.2). The guild→owner mapping reads the ``GUILD#<gid>`` / ``OWNER`` item's
``data.owner_sub`` (the item the web-ui ``guild_admin_service`` writes) over the
same table, so the sidecar resolves ownership exactly as the web-ui and bot do
without depending on the web-ui package.

Requirements: 1.1, 2.1, 5.1, 9.2
"""

from __future__ import annotations

import logging

from hellodj_platform_logic.user_credential_resolver import (
    OwnerLookup,
    UserCredentialResolver,
    guild_pk,
)

from .config import TidalStreamSettings

__all__ = ["build_owner_lookup", "build_user_credential_resolver"]

log = logging.getLogger(__name__)

#: Sort key of a guild's ownership item (mirrors ``guild_admin_service.OWNER_SK``).
_OWNER_SK = "OWNER"


def build_user_credential_resolver(
    settings: TidalStreamSettings,
) -> tuple[UserCredentialResolver, OwnerLookup] | None:
    """Wire a real resolver + owner lookup over ``CoreTable`` + KMS, or ``None``.

    Lazily imports ``boto3`` and the shared ``data_access`` / ``token_crypto``
    modules. On ANY construction failure (boto3 missing, no credentials, package
    unavailable) it logs and returns ``None`` so the caller can start the health
    server in a degraded, observably not-ready state rather than crash (R7.5) —
    the same non-fatal convention as the bot's resolver bootstrap.

    Returns:
        A ``(resolver, owner_lookup)`` pair sharing one ``CoreTable``, or
        ``None`` when the unified store cannot be wired.
    """
    try:
        import boto3  # lazy — only present/needed in the SaaS deployment
        from hellodj_platform_logic.data_access import CoreTable
        from hellodj_platform_logic.token_crypto import (
            EncryptedBlob,
            decrypt_blob,
        )

        kms = boto3.client("kms", region_name=settings.region_name)
        ddb = boto3.resource("dynamodb", region_name=settings.region_name)
        core = CoreTable(ddb.Table(settings.core_table))

        owner_lookup = _CoreOwnerLookup(core)

        def _decrypt(
            *,
            ciphertext: bytes,
            wrapped_key: bytes,
            key_id: str,
            nonce: bytes,
        ) -> bytes:
            enc = EncryptedBlob(
                ciphertext=ciphertext,
                wrapped_key=wrapped_key,
                key_id=key_id,
                nonce=nonce,
            )
            return decrypt_blob(enc, kms)

        resolver = UserCredentialResolver(
            core,
            owner_lookup,
            _decrypt,
            expiry_skew_seconds=settings.expiry_skew_seconds,
        )
        log.info(
            "tidal-stream: unified per-user credential resolver wired "
            "(table=%s)", settings.core_table,
        )
        return resolver, owner_lookup
    except Exception as exc:  # noqa: BLE001 - non-fatal: sidecar degrades honestly
        log.warning(
            "tidal-stream: unified credential resolver unavailable (%s) — "
            "per-user Tidal streaming is not ready", exc,
        )
        return None


def build_owner_lookup(core) -> OwnerLookup:
    """Return an :class:`OwnerLookup` over an existing ``CoreTable``."""
    return _CoreOwnerLookup(core)


class _CoreOwnerLookup:
    """:class:`OwnerLookup` over the ``GUILD#<gid>`` / ``OWNER`` item."""

    def __init__(self, core) -> None:
        self._core = core

    def owner_of(self, guild_id: str) -> str | None:
        """Return the guild's owning Cognito ``sub`` (``data.owner_sub``)."""
        item = self._core.get(guild_pk(str(guild_id)), _OWNER_SK)
        if item is None:
            return None
        return item.get("data", {}).get("owner_sub")
