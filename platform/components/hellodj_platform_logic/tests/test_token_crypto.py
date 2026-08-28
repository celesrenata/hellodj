"""Unit + property tests for ``token_crypto`` envelope encryption.

Feature: unified-oauth-and-token-watchdog.

Covers the two correctness properties for the shared envelope-encryption module:

    * **Property 1 (crypto round-trip)** — for any token blob ``b``,
      ``decrypt_blob(encrypt_blob(b)) == b``; any tamper of the ciphertext,
      nonce, or wrapped key makes ``decrypt_blob`` *fail* rather than return
      wrong plaintext. **Validates: Requirements 3.2, 3.3**
    * **Property 2 (no plaintext leak)** — no plaintext token material appears
      in the :class:`EncryptedBlob` fields or its ``repr``; the object persisted
      to the item carries only opaque ciphertext/wrapped-key/nonce.
      **Validates: Requirements 2.3, 3.3**

Plus focused unit tests for the failure paths (R3.4): a KMS decrypt failure and
an empty/absent data key are normalized to :class:`DecryptionError` /
:class:`TokenCryptoError` with no token material in the message.

The tests inject a :class:`FakeKms` that models KMS envelope semantics locally
(no AWS): ``generate_data_key`` returns a random plaintext key plus a reversible
"wrapped" form, and ``decrypt`` unwraps it. This lets the property test exercise
the real AES-GCM crypto path deterministically.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.token_crypto import (
    DATA_KEY_SPEC,
    NONCE_LENGTH,
    DecryptionError,
    EncryptedBlob,
    KmsClient,
    TokenCryptoError,
    decrypt_blob,
    encrypt_blob,
)

# ---------------------------------------------------------------------------
# Fake KMS modeling envelope semantics (no AWS)
# ---------------------------------------------------------------------------

#: A fixed prefix used to model KMS wrapping: the "wrapped" data key is this
#: prefix followed by the plaintext key. ``decrypt`` strips the prefix. This is
#: NOT real encryption; it only lets tests round-trip the data key locally while
#: the *token blob* is protected by real AES-GCM.
_WRAP_PREFIX = b"wrapped::"


@dataclass
class FakeKms:
    """Deterministic in-process KMS that models envelope wrap/unwrap.

    Records calls so tests can assert the key id and key spec used. A wrapped
    key that does not carry the wrap prefix (a tampered ``enc_key``) fails to
    unwrap, mirroring a real KMS rejecting a corrupt ciphertext blob.
    """

    key_id: str = "arn:aws:kms:us-east-1:000000000000:key/source-creds"
    generate_calls: list[dict[str, object]] = field(default_factory=list)
    decrypt_calls: list[dict[str, object]] = field(default_factory=list)

    def generate_data_key(self, **kwargs: object) -> dict[str, object]:
        self.generate_calls.append(dict(kwargs))
        plaintext = os.urandom(32)  # AES-256 data key
        return {
            "Plaintext": plaintext,
            "CiphertextBlob": _WRAP_PREFIX + plaintext,
            "KeyId": kwargs.get("KeyId", self.key_id),
        }

    def decrypt(self, **kwargs: object) -> dict[str, object]:
        self.decrypt_calls.append(dict(kwargs))
        blob = kwargs["CiphertextBlob"]
        assert isinstance(blob, bytes)
        if not blob.startswith(_WRAP_PREFIX):
            raise ValueError("invalid ciphertext blob")
        return {"Plaintext": blob[len(_WRAP_PREFIX):]}


class RaisingDecryptKms(FakeKms):
    """Fake whose ``decrypt`` always raises (models a KMS access denial)."""

    def decrypt(self, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("kms decrypt denied")


class EmptyKeyKms(FakeKms):
    """Fake that returns an empty plaintext data key from generate/decrypt."""

    def generate_data_key(self, **kwargs: object) -> dict[str, object]:
        return {"Plaintext": b"", "CiphertextBlob": _WRAP_PREFIX}


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Arbitrary token blobs, including empty and large payloads.
_blobs = st.binary(min_size=0, max_size=4096)

# A recognizable secret-looking token used for the "no leak" assertions.
_SECRET = b'{"refresh_token": "SUPER-SECRET-REFRESH-abc123", "access_token": "AT"}'


# ---------------------------------------------------------------------------
# Property 1 — crypto round-trip
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(plaintext=_blobs)
def test_round_trip_returns_original_plaintext(plaintext: bytes) -> None:
    """Property 1: ``decrypt_blob(encrypt_blob(b)) == b`` for any blob.

    **Validates: Requirements 3.2, 3.3**
    """
    kms = FakeKms()

    enc = encrypt_blob(plaintext, kms, kms.key_id)
    assert isinstance(enc, EncryptedBlob)

    recovered = decrypt_blob(enc, kms)
    assert recovered == plaintext

    # The data key was requested as AES-256 under the given CMK id.
    assert kms.generate_calls == [{"KeyId": kms.key_id, "KeySpec": DATA_KEY_SPEC}]


@settings(max_examples=150)
@given(
    plaintext=st.binary(min_size=1, max_size=1024),
    flip=st.integers(min_value=0, max_value=1023),
)
def test_ciphertext_tamper_fails_decrypt(plaintext: bytes, flip: int) -> None:
    """Property 1: tampering the ciphertext makes decrypt fail (R3.4).

    A flipped ciphertext byte must raise, never return wrong plaintext.
    **Validates: Requirements 3.3**
    """
    kms = FakeKms()
    enc = encrypt_blob(plaintext, kms, kms.key_id)

    idx = flip % len(enc.ciphertext)
    tampered_ct = bytearray(enc.ciphertext)
    tampered_ct[idx] ^= 0x01
    tampered = EncryptedBlob(
        ciphertext=bytes(tampered_ct),
        wrapped_key=enc.wrapped_key,
        key_id=enc.key_id,
        nonce=enc.nonce,
    )

    with pytest.raises(DecryptionError):
        decrypt_blob(tampered, kms)


@settings(max_examples=150)
@given(plaintext=st.binary(min_size=1, max_size=1024))
def test_nonce_tamper_fails_decrypt(plaintext: bytes) -> None:
    """Property 1: tampering the nonce makes decrypt fail, not mis-decrypt.

    **Validates: Requirements 3.3**
    """
    kms = FakeKms()
    enc = encrypt_blob(plaintext, kms, kms.key_id)

    tampered_nonce = bytearray(enc.nonce)
    tampered_nonce[0] ^= 0xFF
    tampered = EncryptedBlob(
        ciphertext=enc.ciphertext,
        wrapped_key=enc.wrapped_key,
        key_id=enc.key_id,
        nonce=bytes(tampered_nonce),
    )

    with pytest.raises(DecryptionError):
        decrypt_blob(tampered, kms)


@settings(max_examples=150)
@given(plaintext=st.binary(min_size=1, max_size=1024))
def test_wrapped_key_tamper_fails_decrypt(plaintext: bytes) -> None:
    """Property 1: tampering the wrapped data key makes decrypt fail (R3.4).

    A corrupt wrapped key either fails to unwrap or unwraps to a different key
    whose AES-GCM tag will not verify. Either way decrypt raises rather than
    returning wrong plaintext.
    **Validates: Requirements 3.3**
    """
    kms = FakeKms()
    enc = encrypt_blob(plaintext, kms, kms.key_id)

    tampered = EncryptedBlob(
        ciphertext=enc.ciphertext,
        wrapped_key=b"garbage-not-wrapped",
        key_id=enc.key_id,
        nonce=enc.nonce,
    )

    with pytest.raises(DecryptionError):
        decrypt_blob(tampered, kms)


def test_encrypt_uses_fresh_nonce_and_key_each_call() -> None:
    """Each encrypt uses a fresh nonce; the ciphertext is non-deterministic.

    Two encryptions of the same plaintext must differ (fresh data key + nonce),
    and each must still round-trip.
    """
    kms = FakeKms()
    a = encrypt_blob(_SECRET, kms, kms.key_id)
    b = encrypt_blob(_SECRET, kms, kms.key_id)

    assert a.nonce != b.nonce
    assert a.ciphertext != b.ciphertext
    assert len(a.nonce) == NONCE_LENGTH
    assert decrypt_blob(a, kms) == _SECRET
    assert decrypt_blob(b, kms) == _SECRET


# ---------------------------------------------------------------------------
# Property 2 — no plaintext leak
# ---------------------------------------------------------------------------


def test_no_plaintext_token_in_blob_or_repr() -> None:
    """Property 2: the encrypted blob (and its repr) contains no plaintext.

    **Validates: Requirements 2.3, 3.3**
    """
    kms = FakeKms()
    enc = encrypt_blob(_SECRET, kms, kms.key_id)

    # The recognizable secret substring must not appear in any stored field.
    assert _SECRET not in enc.ciphertext
    assert _SECRET not in enc.wrapped_key
    assert _SECRET not in enc.nonce

    # ...nor anywhere in the repr (which is what would land in a log).
    text = repr(enc)
    assert "SUPER-SECRET-REFRESH" not in text
    assert "refresh_token" not in text


# ---------------------------------------------------------------------------
# Failure paths (R3.4) — normalized, message-safe errors
# ---------------------------------------------------------------------------


def test_kms_decrypt_failure_is_normalized() -> None:
    """A KMS decrypt failure raises DecryptionError with no token material."""
    kms = RaisingDecryptKms()
    enc = encrypt_blob(_SECRET, FakeKms(key_id=kms.key_id), kms.key_id)

    with pytest.raises(DecryptionError) as excinfo:
        decrypt_blob(enc, kms)

    assert "SUPER-SECRET-REFRESH" not in str(excinfo.value)


def test_empty_data_key_on_encrypt_raises() -> None:
    """An empty KMS data key on encrypt raises a message-safe error."""
    kms = EmptyKeyKms()
    with pytest.raises(TokenCryptoError):
        encrypt_blob(_SECRET, kms, kms.key_id)


def test_fake_kms_satisfies_protocol() -> None:
    """The fake KMS structurally satisfies the injectable KmsClient protocol."""
    assert isinstance(FakeKms(), KmsClient)
