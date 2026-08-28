"""Shared KMS envelope encryption for source-credential token blobs.

This module is the single implementation of application-layer envelope
encryption for the unified-oauth-and-token-watchdog feature. It is imported by
the web-ui (write path), the token-refresh watchdog (read + write), and the
playback readers (read path), so all three share one crypto contract and one
ciphertext format.

Behavior (Requirements 3.2, 3.3, 3.4):

    * :func:`encrypt_blob` requests a fresh AES-256 data key from KMS
      (``GenerateDataKey``), encrypts the plaintext token blob with AES-GCM
      using the *plaintext* data key, immediately discards that plaintext key,
      and returns only the ciphertext, the KMS-*wrapped* data key, the CMK id,
      and the AES-GCM nonce (R3.2). The wrapped data key never leaves the
      returned :class:`EncryptedBlob`; the plaintext data key is never stored.
    * :func:`decrypt_blob` recovers the data key via ``kms.decrypt`` and
      AES-GCM-decrypts the ciphertext (R3.3). AES-GCM authenticates the
      ciphertext, so any tamper of the ciphertext, nonce, or wrapped key makes
      decryption fail rather than returning wrong plaintext (R3.4).
    * Plaintext token material is **never** logged and never appears in an
      exception message or ``repr`` (R3.3). :class:`EncryptedBlob` holds only
      ciphertext/wrapped-key/nonce (all opaque), and errors carry only a static
      message.

Purity / testability: the KMS client is injected as a :class:`KmsClient`
Protocol (``generate_data_key`` / ``decrypt``), so the module performs no live
AWS calls and can be exercised directly by property-based tests with a fake KMS
that models envelope semantics.

Design reference: design.md "Envelope encryption (``token_crypto``)" and
Correctness Property 1 (crypto round-trip) / Property 2 (no plaintext leak).

Requirements: 3.2, 3.3, 3.4
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: KMS key spec requested for the per-item data key. AES-256 yields a 32-byte
#: plaintext data key, matching AES-GCM's 256-bit key size.
DATA_KEY_SPEC = "AES_256"

#: AES-GCM nonce length in bytes. 96 bits is the AES-GCM recommended nonce size.
NONCE_LENGTH = 12


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TokenCryptoError(Exception):
    """Base error for envelope encryption/decryption.

    Error messages are deliberately static and carry no token material, so a
    log line or traceback can never leak plaintext (R3.3).
    """


class DecryptionError(TokenCryptoError):
    """Raised when a token blob cannot be authentically decrypted (R3.4).

    This covers a tampered ciphertext/nonce/wrapped key (AES-GCM tag mismatch)
    as well as a KMS decrypt failure. Callers treat the credential as unusable
    and refresh or surface, rather than crashing. The message never contains
    ciphertext, key material, or plaintext.
    """


# ---------------------------------------------------------------------------
# KMS client protocol (injectable)
# ---------------------------------------------------------------------------


@runtime_checkable
class KmsClient(Protocol):
    """Injectable protocol for the subset of KMS used by envelope encryption.

    Mirrors the boto3 ``kms`` client shape so a real client satisfies it, while
    tests inject a deterministic fake. Only the two calls this module needs are
    declared.
    """

    def generate_data_key(self, **kwargs: Any) -> dict[str, Any]:
        """Return ``{"Plaintext": bytes, "CiphertextBlob": bytes, ...}``."""
        ...

    def decrypt(self, **kwargs: Any) -> dict[str, Any]:
        """Return ``{"Plaintext": bytes, ...}`` for a wrapped data key."""
        ...


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EncryptedBlob:
    """Immutable envelope-encrypted token blob.

    Every field is opaque ciphertext or an identifier; none of them is
    plaintext token material, so it is safe to persist the whole object into a
    DynamoDB item and safe to ``repr`` (R3.2, Property 2).

    Attributes:
        ciphertext: AES-GCM ciphertext of the token blob (includes the GCM
            authentication tag appended by :class:`AESGCM`).
        wrapped_key: The KMS-encrypted (wrapped) data key. Only a principal
            holding the KMS decrypt grant can unwrap it.
        key_id: The KMS CMK id/ARN used, retained for decrypt routing and key
            rotation.
        nonce: The AES-GCM nonce used for this ciphertext. Unique per encrypt.
    """

    ciphertext: bytes
    wrapped_key: bytes
    key_id: str
    nonce: bytes


# ---------------------------------------------------------------------------
# Encrypt / decrypt
# ---------------------------------------------------------------------------


def encrypt_blob(plaintext: bytes, kms: KmsClient, key_id: str) -> EncryptedBlob:
    """Envelope-encrypt ``plaintext`` with a fresh KMS data key (R3.2).

    Requests an AES-256 data key from KMS, encrypts ``plaintext`` with AES-GCM
    using the plaintext data key, discards the plaintext data key, and returns
    an :class:`EncryptedBlob` carrying only the ciphertext, the wrapped data
    key, the CMK id, and the nonce.

    Args:
        plaintext: The token blob bytes to encrypt (never logged).
        kms: The injected KMS client.
        key_id: The source-credentials CMK id/ARN to generate the data key under.

    Returns:
        An :class:`EncryptedBlob` with no plaintext token material.

    Raises:
        TokenCryptoError: If KMS does not return a usable data key. The message
            carries no token or key material (R3.3).
    """
    try:
        response = kms.generate_data_key(KeyId=key_id, KeySpec=DATA_KEY_SPEC)
        data_key: bytes = response["Plaintext"]
        wrapped_key: bytes = response["CiphertextBlob"]
    except TokenCryptoError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize to a message-safe error
        raise TokenCryptoError("failed to generate a KMS data key") from exc

    if not data_key:
        raise TokenCryptoError("KMS returned an empty data key")

    nonce = os.urandom(NONCE_LENGTH)
    try:
        ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, None)
    finally:
        # Best-effort scrub of the plaintext data key reference; the object is
        # discarded regardless so it is never persisted (R3.2).
        del data_key

    return EncryptedBlob(
        ciphertext=ciphertext,
        wrapped_key=wrapped_key,
        key_id=key_id,
        nonce=nonce,
    )


def decrypt_blob(enc: EncryptedBlob, kms: KmsClient) -> bytes:
    """Recover the plaintext token blob from an :class:`EncryptedBlob` (R3.3).

    Unwraps the data key via ``kms.decrypt`` and AES-GCM-decrypts the
    ciphertext. Because AES-GCM authenticates the ciphertext, any tamper of the
    ciphertext, nonce, or wrapped key raises :class:`DecryptionError` instead of
    returning wrong plaintext (R3.4).

    Args:
        enc: The envelope-encrypted blob.
        kms: The injected KMS client (must hold the decrypt grant).

    Returns:
        The decrypted token blob bytes.

    Raises:
        DecryptionError: If the wrapped key cannot be unwrapped or the
            ciphertext fails authentication. The message carries no token, key,
            or ciphertext material (R3.3, R3.4).
    """
    try:
        response = kms.decrypt(CiphertextBlob=enc.wrapped_key, KeyId=enc.key_id)
        data_key: bytes = response["Plaintext"]
    except Exception as exc:  # noqa: BLE001 - normalize; never leak material
        raise DecryptionError("failed to unwrap the data key") from exc

    if not data_key:
        raise DecryptionError("KMS returned an empty data key")

    try:
        return AESGCM(data_key).decrypt(enc.nonce, enc.ciphertext, None)
    except InvalidTag as exc:
        # Tamper of ciphertext/nonce/wrapped-key: authentication failed. Never
        # return wrong plaintext (R3.4). Message carries no material.
        raise DecryptionError("token blob failed authentication") from exc
    except Exception as exc:  # noqa: BLE001 - defensive; still message-safe
        raise DecryptionError("token blob could not be decrypted") from exc
    finally:
        del data_key
