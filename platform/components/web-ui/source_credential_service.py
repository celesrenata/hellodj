"""Unified per-user source-credential store over the ``hellodj-core`` table.

:class:`SourceCredentialService` is the single service that owns a user's
source-provider OAuth credentials for the unified-oauth-and-token-watchdog
feature. It is a web-ui module but is deliberately dependency-light (it imports
only the shared :mod:`hellodj_platform_logic` package) so the durable
token-refresh **watchdog** hosted in ``playback-orchestrator`` can import and
use the exact same store — one identity (``sub``) spans web-ui, watchdog, and
the playback readers.

Item (hellodj-core single table):

* Credential: ``PK=USER#<sub>``  ``SK=SOURCECRED#<provider>``  entityType
  ``SourceCredential``.

The item splits **plaintext status** from the **envelope-encrypted token blob**
(design.md "Storage model"). Plaintext ``data`` status fields (no decrypt to
read): ``connected`` (bool), ``connected_at``/``updated_at``/``last_refresh_at``
(epoch seconds), ``expires_at`` (access-token expiry the watchdog reads without
decrypting), ``scope``, ``refresh_status`` (``ok``/``failed``), and
``refresh_error`` (short reason, never token material). Encrypted-blob fields:
``enc_blob`` (base64 AES-GCM ciphertext of the token JSON), ``enc_key`` (base64
KMS-wrapped data key), ``enc_nonce`` (base64 AES-GCM nonce), and ``kms_key_id``
(CMK id for decrypt routing + rotation).

Rationale for the split (design.md): the watchdog and UI enumerate/read status
(``expires_at``, ``refresh_status``) **without** a KMS call; only
:meth:`load_token` and :meth:`record_refresh` (success) touch the blob. This
keeps KMS traffic proportional to refreshes, not to every list/render.

The token blob is the :class:`~hellodj_platform_logic.source_refresh.TokenState`
serialized to JSON bytes before
:func:`~hellodj_platform_logic.token_crypto.encrypt_blob`, and parsed back into a
``TokenState`` after :func:`~hellodj_platform_logic.token_crypto.decrypt_blob`.
Tokens are never logged; the ``repr`` of the stored ``EncryptedBlob`` carries
only opaque ciphertext. A decrypt failure surfaces as
:class:`~hellodj_platform_logic.token_crypto.DecryptionError` so a caller treats
the credential as unusable rather than using a dead token.

Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 3.2, 3.3
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from typing import Any

from hellodj_platform_logic.data_access import CoreTable
from hellodj_platform_logic.source_refresh import TokenState, needs_refresh
from hellodj_platform_logic.token_crypto import (
    EncryptedBlob,
    KmsClient,
    decrypt_blob,
    encrypt_blob,
)

__all__ = [
    "SOURCECRED_SK_PREFIX",
    "SOURCECRED_ENTITY_TYPE",
    "REFRESH_STATUS_OK",
    "REFRESH_STATUS_FAILED",
    "user_pk",
    "sourcecred_sk",
    "NearExpiryCredential",
    "SourceCredentialService",
]

#: Sort-key prefix shared by every per-user source-credential item.
SOURCECRED_SK_PREFIX = "SOURCECRED#"

#: ``entityType`` discriminator for the credential item.
SOURCECRED_ENTITY_TYPE = "SourceCredential"

#: Plaintext ``refresh_status`` values.
REFRESH_STATUS_OK = "ok"
REFRESH_STATUS_FAILED = "failed"


def user_pk(sub: str) -> str:
    """Return the ``hellodj-core`` partition key for a user's items.

    Source credentials are keyed by the stable Cognito subject (``sub``), not a
    username, so a single identity spans the web-ui, the watchdog, and the bot
    (mirrors :mod:`entitlement_service`).
    """
    return f"USER#{sub}"


def sourcecred_sk(provider: str) -> str:
    """Return the sort key for a user's per-provider source-credential item."""
    return f"{SOURCECRED_SK_PREFIX}{provider}"


def _now_s() -> float:
    """Return the current time in epoch seconds."""
    return time.time()


def _b64e(raw: bytes) -> str:
    """Base64-encode opaque bytes for storage in a DynamoDB string field."""
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    """Decode a base64 string field back to opaque bytes."""
    return base64.b64decode(text.encode("ascii"))


class NearExpiryCredential:
    """Identity + plaintext status for a near-expiry credential (no token).

    Yielded by :meth:`SourceCredentialService.iter_near_expiry` so the watchdog
    can decide what to refresh WITHOUT decrypting anything. It deliberately
    carries no token material — only the keys and the plaintext status fields
    the enumeration projection returns.
    """

    __slots__ = ("sub", "provider", "expires_at", "refresh_status")

    def __init__(
        self,
        sub: str,
        provider: str,
        expires_at: float,
        refresh_status: str,
    ) -> None:
        self.sub = sub
        self.provider = provider
        self.expires_at = expires_at
        self.refresh_status = refresh_status

    def __repr__(self) -> str:  # pragma: no cover - trivial, carries no token
        return (
            "NearExpiryCredential("
            f"sub={self.sub!r}, provider={self.provider!r}, "
            f"expires_at={self.expires_at!r}, "
            f"refresh_status={self.refresh_status!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, NearExpiryCredential):
            return NotImplemented
        return (
            self.sub == other.sub
            and self.provider == other.provider
            and self.expires_at == other.expires_at
            and self.refresh_status == other.refresh_status
        )


def _token_state_to_json_bytes(state: TokenState) -> bytes:
    """Serialize a :class:`TokenState` to the JSON blob bytes to encrypt.

    The blob is the ONLY place token values (access/refresh token, expiry,
    scope, provider-specific ``extra``) live, always encrypted before storage.
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


def _token_state_from_json_bytes(raw: bytes) -> TokenState:
    """Parse decrypted blob bytes back into a :class:`TokenState`."""
    parsed = json.loads(raw.decode("utf-8"))
    return TokenState(
        access_token=parsed.get("access_token", ""),
        refresh_token=parsed.get("refresh_token", ""),
        expires_at=float(parsed.get("expires_at", 0.0)),
        scope=parsed.get("scope", ""),
        extra=dict(parsed.get("extra", {})),
    )


class SourceCredentialService:
    """Read/write per-user source credentials on the ``hellodj-core`` table.

    Wraps :class:`~hellodj_platform_logic.data_access.CoreTable` for storage and
    :mod:`hellodj_platform_logic.token_crypto` for envelope encryption. The KMS
    client and CMK id are injected (like the crypto module), so the service is
    unit-testable with a fake table + fake KMS and no live AWS.

    Args:
        core_table: An initialized :class:`CoreTable` bound to ``hellodj-core``.
        kms: The injected KMS client used to wrap/unwrap the per-item data key.
        kms_key_id: The source-credentials CMK id/ARN used for new writes.
        clock: Injectable epoch-seconds clock (for tests).
    """

    def __init__(
        self,
        core_table: CoreTable,
        kms: KmsClient,
        kms_key_id: str,
        *,
        clock: Any = _now_s,
    ) -> None:
        self._core = core_table
        self._kms = kms
        self._kms_key_id = kms_key_id
        self._clock = clock

    # -- writes -------------------------------------------------------------

    def store(
        self,
        sub: str,
        provider: str,
        token_state: TokenState,
        *,
        connected_by: str,
    ) -> None:
        """Encrypt and persist a user's credential for a provider (R2.1-R2.3).

        Envelope-encrypts the token blob (R3.2) and upserts the credential item
        with the optimistic-lock read-modify-write. Only the encrypted blob
        fields carry token material; the plaintext status fields (``connected``,
        ``expires_at``, ``scope`` ...) are set so the UI and watchdog can read
        status without a decrypt (R2.2). ``connected_at`` is preserved across a
        re-store (first-connect time), while ``updated_at`` advances.

        Args:
            sub: The connecting user's Cognito subject.
            provider: The source provider (``youtube``/``spotify``/...).
            token_state: The freshly exchanged token to persist.
            connected_by: The acting principal recorded on the item.
        """
        enc = encrypt_blob(
            _token_state_to_json_bytes(token_state), self._kms, self._kms_key_id
        )
        now = float(self._clock())

        def _mutate(data: dict[str, Any]) -> dict[str, Any]:
            connected_at = data.get("connected_at", now)
            new = dict(data)
            new.update(
                {
                    "connected": True,
                    "connected_by": connected_by,
                    "connected_at": connected_at,
                    "updated_at": now,
                    "expires_at": token_state.expires_at,
                    "scope": token_state.scope,
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

    def disconnect(self, sub: str, provider: str) -> None:
        """Delete a user's credential item for one provider (R2.5).

        Deletes only that provider's ``SOURCECRED#<provider>`` item; every other
        credential (and every other item under the user's partition) is
        untouched.
        """
        self._core.delete(user_pk(sub), sourcecred_sk(provider))

    def record_refresh(
        self,
        sub: str,
        provider: str,
        *,
        new_state: TokenState | None = None,
        error: str | None = None,
    ) -> None:
        """Write back a refresh outcome under the optimistic lock (R5.3-R5.5).

        On **success** (``new_state`` given): re-encrypts the new blob and sets
        ``refresh_status="ok"``, ``last_refresh_at``, ``expires_at``, ``scope``,
        and the new ``enc_blob``/``enc_key``/``enc_nonce``/``kms_key_id``.

        On **failure** (``error`` given): sets ``refresh_status="failed"`` plus a
        short ``refresh_error`` and leaves the prior encrypted blob intact so the
        next tick can retry from the still-stored refresh token (R5.4). The
        ``error`` string must never contain token material — callers pass a short
        reason.

        The write is an optimistic-lock read-modify-write, so a concurrent
        watchdog replica cannot corrupt the item: a losing writer re-reads and
        re-applies (R5.5).

        Raises:
            ValueError: If neither ``new_state`` nor ``error`` is supplied.
        """
        if new_state is None and error is None:
            raise ValueError("record_refresh requires new_state or error")

        now = float(self._clock())

        if new_state is not None:
            enc = encrypt_blob(
                _token_state_to_json_bytes(new_state),
                self._kms,
                self._kms_key_id,
            )

            def _ok(data: dict[str, Any]) -> dict[str, Any]:
                new = dict(data)
                new.update(
                    {
                        "connected": True,
                        "updated_at": now,
                        "last_refresh_at": now,
                        "refresh_status": REFRESH_STATUS_OK,
                        "refresh_error": "",
                        "expires_at": new_state.expires_at,
                        "scope": new_state.scope,
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
                _ok,
                entity_type=SOURCECRED_ENTITY_TYPE,
            )
            return

        # Failure path: leave the prior blob intact (R5.4).
        def _failed(data: dict[str, Any]) -> dict[str, Any]:
            new = dict(data)
            new.update(
                {
                    "updated_at": now,
                    "last_refresh_at": now,
                    "refresh_status": REFRESH_STATUS_FAILED,
                    "refresh_error": error or "",
                }
            )
            return new

        self._core.update_with_lock(
            user_pk(sub),
            sourcecred_sk(provider),
            _failed,
            entity_type=SOURCECRED_ENTITY_TYPE,
        )

    # -- reads --------------------------------------------------------------

    def status(self, sub: str) -> list[dict[str, Any]]:
        """Return per-provider plaintext status for a user (no decrypt) (R2.2).

        Enumerates the user's ``SOURCECRED#`` items and returns their plaintext
        status fields only. Never touches the encrypted blob and never returns a
        token value, so the config/account UI can render status cheaply.
        """
        rows = self._core.query_pk_prefix(
            user_pk(sub), sk_prefix=SOURCECRED_SK_PREFIX
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            provider = row["SK"].split(SOURCECRED_SK_PREFIX, 1)[1]
            data = row.get("data", {})
            out.append(self._status_view(provider, data))
        out.sort(key=lambda r: r["provider"])
        return out

    def status_for(self, sub: str, provider: str) -> dict[str, Any] | None:
        """Return one provider's plaintext status, or ``None`` if absent (R2.2).

        No decrypt; never returns a token value.
        """
        item = self._core.get(user_pk(sub), sourcecred_sk(provider))
        if item is None:
            return None
        return self._status_view(provider, item.get("data", {}))

    def load_token(self, sub: str, provider: str) -> TokenState | None:
        """Decrypt and return a user's token for a provider, or ``None`` (R3.3).

        Loads the credential item, decrypts the envelope blob with the injected
        KMS client, and parses it back into a :class:`TokenState`. Returns
        ``None`` when the item (or its blob) is absent. A decrypt failure raises
        :class:`~hellodj_platform_logic.token_crypto.DecryptionError` so the
        caller treats the credential as unusable rather than using a dead token
        (R3.4); the plaintext is never logged.
        """
        item = self._core.get(user_pk(sub), sourcecred_sk(provider))
        if item is None:
            return None
        data = item.get("data", {})
        enc_blob = data.get("enc_blob")
        enc_key = data.get("enc_key")
        enc_nonce = data.get("enc_nonce")
        kms_key_id = data.get("kms_key_id")
        if not (enc_blob and enc_key and enc_nonce and kms_key_id):
            return None
        enc = EncryptedBlob(
            ciphertext=_b64d(enc_blob),
            wrapped_key=_b64d(enc_key),
            key_id=kms_key_id,
            nonce=_b64d(enc_nonce),
        )
        plaintext = decrypt_blob(enc, self._kms)
        return _token_state_from_json_bytes(plaintext)

    def iter_near_expiry(
        self, now: float, threshold: float
    ) -> Iterator[NearExpiryCredential]:
        """Yield credentials whose access token expires within ``threshold`` (R5.2).

        Used by the watchdog. Enumerates every ``SourceCredential`` item via the
        key-projected :meth:`CoreTable.scan_entity` (which excludes ``enc_blob``,
        so enumeration never decrypts) and yields a :class:`NearExpiryCredential`
        for each item whose ``expires_at`` is at or within ``threshold`` seconds
        of ``now``. It yields identity + plaintext status ONLY — never the
        decrypted blob (the watchdog decrypts per-item via :meth:`load_token`
        only when it actually refreshes).

        Args:
            now: Current time as epoch seconds.
            threshold: How far ahead (seconds) counts as "near expiry"; an item
                with ``expires_at <= now + threshold`` is yielded.
        """
        for item in self._core.scan_entity(SOURCECRED_ENTITY_TYPE):
            sk = item.get("SK", "")
            if not sk.startswith(SOURCECRED_SK_PREFIX):
                continue
            pk = item.get("PK", "")
            if not pk.startswith("USER#"):
                continue
            sub = pk.split("USER#", 1)[1]
            provider = sk.split(SOURCECRED_SK_PREFIX, 1)[1]
            data = item.get("data", {})
            expires_at = float(data.get("expires_at", 0.0))
            refresh_status = data.get("refresh_status", "")
            # Reuse the shared predicate: a token expiring within ``threshold``
            # of ``now`` needs a refresh (skew == threshold here).
            probe = TokenState(
                access_token="", refresh_token="", expires_at=expires_at
            )
            if needs_refresh(probe, now, threshold):
                yield NearExpiryCredential(
                    sub=sub,
                    provider=provider,
                    expires_at=expires_at,
                    refresh_status=refresh_status,
                )

    # -- internal -----------------------------------------------------------

    @staticmethod
    def _status_view(provider: str, data: dict[str, Any]) -> dict[str, Any]:
        """Project an item's ``data`` to the plaintext status view (no token).

        Explicitly whitelists the plaintext status fields so no encrypted blob
        field (``enc_blob`` etc.) can ever leak into a status response (R2.2,
        R8.3).
        """
        return {
            "provider": provider,
            "connected": bool(data.get("connected", False)),
            "connected_at": data.get("connected_at", 0),
            "updated_at": data.get("updated_at", 0),
            "expires_at": data.get("expires_at", 0),
            "scope": data.get("scope", ""),
            "last_refresh_at": data.get("last_refresh_at", 0),
            "refresh_status": data.get("refresh_status", ""),
            "refresh_error": data.get("refresh_error", ""),
        }
