"""One-shot backfill: legacy per-guild secrets -> encrypted DynamoDB creds.

Migration & Rollout step 3 of the unified-oauth-and-token-watchdog spec: the
legacy platform stored each guild+provider's OAuth tokens as ONE AWS Secrets
Manager secret per guild+provider::

    hellodj/<stage>/guild/<guildId>/<provider>

The unified feature moves those into the ``hellodj-core`` DynamoDB table as one
**envelope-encrypted** ``SourceCredential`` item per user+provider, keyed by the
guild owner's Cognito subject (``PK=USER#<sub>`` / ``SK=SOURCECRED#<provider>``,
entityType ``SourceCredential``) so a single identity spans web-ui, watchdog,
and bot. This module reads the legacy secrets and writes those encrypted items,
so nothing is lost when the Secrets Manager write grant is later dropped
(R2.6, R6.5).

Guild -> owner mapping
----------------------

A credential item is keyed by the OWNER's ``sub``, not the guild. The owning
subject lives in the ``GUILD#<gid>`` / ``OWNER`` item's ``data.owner_sub`` (the
same item ``guild_admin_service`` writes). The backfill resolves it per guild;
a guild with no resolvable owner is **skipped and counted** (there is no user
partition to write the credential under).

Legacy secret shapes are mapped to a
:class:`~hellodj_platform_logic.source_refresh.TokenState` by the pure
:mod:`migration_job.source_credential_mapping` helpers (YouTube PoToken pair in
``extra``; Spotify refresh-token-centric; Tidal status-only), reusing the exact
shapes the web-ui uses on a fresh connect.

Idempotency + safety (R2.6)
---------------------------

The write is the SAME optimistic-lock upsert :class:`SourceCredentialService`
uses (via :class:`_CredentialWriter` mirroring its item shape), so re-running
the backfill overwrites/merges the existing item rather than erroring or
duplicating: ``connected_at`` is preserved across a re-run while ``updated_at``
advances, and the ``version`` simply increments. Each written item is verified
by reading its plaintext status back (never decrypting, never logging a token).

Only **counts** are logged — never token material, never a decrypted blob, never
the secret string. ``boto3`` is imported lazily inside the client factory so the
module imports for tests / ``py_compile`` without AWS libraries present, and
every AWS client is injectable so the flow is unit-testable without live AWS.

Requirements: 2.6, 6.5
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from hellodj_platform_logic.data_access import CoreTable
from hellodj_platform_logic.source_refresh import TokenState
from hellodj_platform_logic.token_crypto import KmsClient, encrypt_blob

from .source_credential_mapping import (
    OWNER_SK,
    REFRESH_STATUS_OK,
    SOURCECRED_ENTITY_TYPE,
    SOURCECRED_SK_PREFIX,
    SUPPORTED_PROVIDERS,
    TIDAL_STATUS_EXPIRES_AT,
    guild_owner_pk,
    guild_secret_prefix,
    legacy_secret_to_token_state,
    parse_guild_secret_name,
    sourcecred_sk,
    user_pk,
)

__all__ = [
    "SUPPORTED_PROVIDERS",
    "SOURCECRED_SK_PREFIX",
    "SOURCECRED_ENTITY_TYPE",
    "REFRESH_STATUS_OK",
    "TIDAL_STATUS_EXPIRES_AT",
    "SecretsListReader",
    "BackfillResult",
    "SourceCredentialBackfill",
    "guild_secret_prefix",
    "parse_guild_secret_name",
    "legacy_secret_to_token_state",
    "user_pk",
    "sourcecred_sk",
    "guild_owner_pk",
    "build_secrets_client",
]

log = logging.getLogger("migration_job")


class SecretsListReader(Protocol):
    """Read-only subset of the boto3 ``secretsmanager`` client used here.

    The backfill only ever LISTS the legacy secrets by name prefix and GETS each
    one's value; it never creates, updates, or deletes a secret (the
    Secrets Manager write path is retired separately, R2.6).
    """

    def list_secrets(self, **kwargs: Any) -> dict[str, Any]:
        """Return a page of secret metadata (``SecretList`` + ``NextToken``)."""
        ...

    def get_secret_value(self, **kwargs: Any) -> dict[str, Any]:
        """Return a mapping containing ``SecretString`` for ``SecretId``."""
        ...


def build_secrets_client(region_name: str | None = None) -> SecretsListReader:
    """Create a real boto3 ``secretsmanager`` client (imported lazily)."""
    import boto3

    return boto3.client("secretsmanager", region_name=region_name)


@dataclass
class BackfillResult:
    """Summary of a completed backfill run (counts only — never token material).

    Attributes:
        secrets_scanned: Legacy secrets enumerated under the guild prefix.
        items_written: Encrypted ``SourceCredential`` items written (or merged).
        items_verified: Written items whose plaintext status read back ``ok``.
        skipped_no_owner: Guild secrets skipped because the guild had no
            resolvable owner ``sub`` (no user partition to write under).
        skipped_unparseable: Secrets under the prefix whose name did not match
            ``guild/<gid>/<provider>`` or named an unsupported provider.
        skipped_empty: Secrets that carried no usable token material.
    """

    secrets_scanned: int = 0
    items_written: int = 0
    items_verified: int = 0
    skipped_no_owner: int = 0
    skipped_unparseable: int = 0
    skipped_empty: int = 0
    _owner_cache: dict[str, str | None] = field(default_factory=dict, repr=False)


def _b64e(raw: bytes) -> str:
    """Base64-encode opaque bytes for a DynamoDB string field."""
    return base64.b64encode(raw).decode("ascii")


def _token_state_to_json_bytes(state: TokenState) -> bytes:
    """Serialize a :class:`TokenState` to the JSON blob bytes to encrypt.

    Mirrors ``source_credential_service._token_state_to_json_bytes`` so the
    backfilled blob is byte-compatible with what the reader/watchdog expect.
    """
    return json.dumps(
        {
            "access_token": state.access_token,
            "refresh_token": state.refresh_token,
            "expires_at": state.expires_at,
            "scope": state.scope,
            "extra": dict(state.extra),
        },
        separators=(",", ":"),
    ).encode("utf-8")


class _CredentialWriter:
    """Write/verify an encrypted ``SourceCredential`` item on ``hellodj-core``.

    Mirrors ``SourceCredentialService.store`` item shape EXACTLY (the migration
    component does not import the web-ui package — the two are separate
    deployables — so the shape is a deliberate shared copy). Uses the optimistic
    -lock read-modify-write so a re-run merges rather than duplicates or errors
    (idempotent, R2.6): ``connected_at`` is preserved, ``updated_at`` advances,
    ``version`` increments.
    """

    def __init__(
        self,
        core_table: CoreTable,
        kms: KmsClient,
        kms_key_id: str,
        *,
        clock: Any = time.time,
    ) -> None:
        self._core = core_table
        self._kms = kms
        self._kms_key_id = kms_key_id
        self._clock = clock

    def store(self, sub: str, provider: str, state: TokenState) -> None:
        """Envelope-encrypt ``state`` and upsert the credential item."""
        enc = encrypt_blob(
            _token_state_to_json_bytes(state), self._kms, self._kms_key_id
        )
        now = float(self._clock())

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            connected_at = data.get("connected_at", now)
            new = dict(data)
            new.update(
                {
                    "connected": True,
                    "connected_by": sub,
                    "connected_at": connected_at,
                    "updated_at": now,
                    "expires_at": state.expires_at,
                    "scope": state.scope,
                    "refresh_status": REFRESH_STATUS_OK,
                    "refresh_error": "",
                    "enc_blob": _b64e(enc.ciphertext),
                    "enc_key": _b64e(enc.wrapped_key),
                    "enc_nonce": _b64e(enc.nonce),
                    "kms_key_id": enc.key_id,
                }
            )
            return new

        self._core.update_with_lock(
            user_pk(sub),
            sourcecred_sk(provider),
            _mutate,
            entity_type=SOURCECRED_ENTITY_TYPE,
        )

    def verify(self, sub: str, provider: str) -> bool:
        """Read the item's plaintext status back; ``True`` when connected+ok.

        Verifies WITHOUT decrypting (never touches the blob, never logs a
        token) — it just confirms the encrypted item is present and marked
        connected with an encrypted blob.
        """
        item = self._core.get(user_pk(sub), sourcecred_sk(provider))
        if item is None:
            return False
        data = item.get("data", {})
        return bool(data.get("connected")) and bool(data.get("enc_blob"))


class SourceCredentialBackfill:
    """One-shot, idempotent backfill of legacy secrets into encrypted items.

    Args:
        secrets: A :class:`SecretsListReader` (a boto3 ``secretsmanager`` client
            in production) used to list + read the legacy per-guild secrets.
        core_table: The ``hellodj-core`` :class:`CoreTable`.
        kms: The injected KMS client used to envelope-encrypt each blob.
        kms_key_id: The source-credentials CMK id used for new writes.
        stage: The deployment stage (``beta``/``staging``/``production``) used to
            build + validate the legacy secret name prefix.
        clock: Injectable epoch-seconds clock (for deterministic tests).
    """

    def __init__(
        self,
        secrets: SecretsListReader,
        core_table: CoreTable,
        kms: KmsClient,
        kms_key_id: str,
        *,
        stage: str,
        clock: Any = time.time,
    ) -> None:
        self._secrets = secrets
        self._core = core_table
        self._stage = stage
        self._writer = _CredentialWriter(
            core_table, kms, kms_key_id, clock=clock
        )

    def run(self) -> BackfillResult:
        """Enumerate legacy secrets and write encrypted items; return counts.

        Steps per legacy secret:

        1. parse ``(guild_id, provider)`` from the name (skip+count if it does
           not match the guild shape / an unsupported provider);
        2. resolve the guild's owner ``sub`` (skip+count if none — no user
           partition to write under);
        3. read + JSON-parse the secret value (skip+count if empty/unusable);
        4. map to a :class:`TokenState` and envelope-encrypt+upsert the item;
        5. verify the written item's plaintext status.

        Only counts are logged — never a token, a decrypted blob, or a secret
        string.
        """
        result = BackfillResult()

        for name in self._iter_secret_names():
            parsed = parse_guild_secret_name(name, self._stage)
            if parsed is None:
                result.skipped_unparseable += 1
                continue
            result.secrets_scanned += 1
            guild_id, provider = parsed

            owner_sub = self._resolve_owner(guild_id, result)
            if not owner_sub:
                result.skipped_no_owner += 1
                log.info(
                    "backfill: guild %s provider %s has no resolvable owner "
                    "— skipped", guild_id, provider,
                )
                continue

            tokens = self._read_secret(name)
            if not tokens:
                result.skipped_empty += 1
                continue

            state = legacy_secret_to_token_state(provider, tokens)
            if not self._has_material(provider, state):
                result.skipped_empty += 1
                continue

            self._writer.store(owner_sub, provider, state)
            result.items_written += 1
            if self._writer.verify(owner_sub, provider):
                result.items_verified += 1
            else:
                log.warning(
                    "backfill: guild %s provider %s wrote item that did not "
                    "verify", guild_id, provider,
                )

        log.info(
            "backfill complete: %d secret(s) scanned, %d encrypted item(s) "
            "written, %d verified; skipped %d no-owner, %d unparseable, "
            "%d empty",
            result.secrets_scanned,
            result.items_written,
            result.items_verified,
            result.skipped_no_owner,
            result.skipped_unparseable,
            result.skipped_empty,
        )
        return result

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _has_material(provider: str, state: TokenState) -> bool:
        """Return whether ``state`` carries something worth persisting.

        Tidal is a status-only connection (the sidecar owns its token), so a
        Tidal state is always worth recording; every other provider needs a
        non-empty refresh token to be usable.
        """
        if provider == "tidal":
            return True
        return bool(state.refresh_token)

    def _iter_secret_names(self):
        """Yield every secret name under the guild prefix (paginated)."""
        prefix = guild_secret_prefix(self._stage)
        kwargs: dict[str, Any] = {
            "Filters": [{"Key": "name", "Values": [prefix]}],
            "MaxResults": 100,
        }
        while True:
            response = self._secrets.list_secrets(**kwargs)
            for entry in response.get("SecretList", []):
                name = entry.get("Name")
                if name:
                    yield name
            token = response.get("NextToken")
            if not token:
                return
            kwargs["NextToken"] = token

    def _resolve_owner(
        self, guild_id: str, result: BackfillResult
    ) -> str | None:
        """Resolve a guild's owner ``sub`` from ``GUILD#<gid>`` / ``OWNER``.

        Cached per run so multiple providers under one guild resolve the owner
        once. Any read failure resolves to ``None`` (the secret is skipped +
        counted) rather than aborting the whole backfill.
        """
        if guild_id in result._owner_cache:
            return result._owner_cache[guild_id]
        owner: str | None = None
        try:
            item = self._core.get(guild_owner_pk(guild_id), OWNER_SK)
            if item is not None:
                owner = item.get("data", {}).get("owner_sub") or None
        except Exception as exc:  # noqa: BLE001 - unresolved owner → skip+count
            log.debug(
                "backfill: owner lookup failed for guild %s (%s)",
                guild_id, exc,
            )
            owner = None
        result._owner_cache[guild_id] = owner
        return owner

    def _read_secret(self, name: str) -> dict[str, Any] | None:
        """Fetch + JSON-parse a secret; ``None`` if absent/unusable.

        Never logs the secret value — only its name and a failure reason.
        """
        try:
            resp = self._secrets.get_secret_value(SecretId=name)
        except Exception as exc:  # noqa: BLE001 - absent/denied → skip
            log.debug("backfill: secret %s not readable (%s)", name, exc)
            return None
        raw = resp.get("SecretString")
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            log.warning("backfill: secret %s is not valid JSON", name)
            return None
        if not isinstance(parsed, dict):
            log.warning("backfill: secret %s is not a JSON object", name)
            return None
        return parsed
