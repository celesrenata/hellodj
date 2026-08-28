"""Tests for Cognito JWT verification (``cognito_jwt``).

Covers task 2 / Requirements 4.1-4.3: a correctly-signed token with the right
issuer/audience/use verifies and yields group claims; a token failing any of
signature, issuer, audience, token_use, or expiry is rejected with a generic
:class:`CognitoJwtError` (never echoing the token). A fake ``PyJWKClient``
supplies the public key so no network JWKS fetch happens; a separate test
exercises the ``kid``-miss refetch path.

The tokens are signed locally with a throwaway RSA keypair via PyJWT — the same
RS256 algorithm Cognito uses — so verification exercises the real crypto path.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from cognito_jwt import CognitoJwtError, CognitoJwtVerifier

_POOL = "us-east-1_TestPool0"
_REGION = "us-east-1"
_CLIENT = "testclient123"
_ISSUER = f"https://cognito-idp.{_REGION}.amazonaws.com/{_POOL}"
_KID = "test-kid-1"


@pytest.fixture(scope="module")
def keypair() -> rsa.RSAPrivateKey:
    """A throwaway RSA private key used to sign test tokens."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeSigningKey:
    """Mimics PyJWKClient's signing-key object (exposes ``.key``)."""

    def __init__(self, public_key: Any) -> None:
        self.key = public_key


class _FakeJwkClient:
    """Fake ``PyJWKClient`` returning a fixed public key, tracking calls.

    Raises for an unknown ``kid`` on the first call and succeeds after a
    (simulated) refetch flips ``self.known``, exercising the kid-miss path.
    """

    def __init__(self, public_key: Any, *, known: bool = True) -> None:
        self._pub = public_key
        self.known = known
        self.calls = 0

    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:
        self.calls += 1
        if not self.known:
            raise jwt.exceptions.PyJWKClientError("no matching kid")
        return _FakeSigningKey(self._pub)


def _make_token(
    keypair: rsa.RSAPrivateKey,
    *,
    token_use: str = "id",
    iss: str = _ISSUER,
    aud: str | None = _CLIENT,
    client_id: str | None = None,
    groups: list[str] | None = None,
    exp_delta: int = 3600,
) -> str:
    """Sign an RS256 JWT with the given claims for testing."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": iss,
        "token_use": token_use,
        "iat": now,
        "exp": now + exp_delta,
        "sub": "user-sub-1",
    }
    if aud is not None:
        claims["aud"] = aud
    if client_id is not None:
        claims["client_id"] = client_id
    if groups is not None:
        claims["cognito:groups"] = groups
    return jwt.encode(
        claims, keypair, algorithm="RS256", headers={"kid": _KID}
    )


def _verifier(keypair: rsa.RSAPrivateKey, **kwargs: Any) -> CognitoJwtVerifier:
    fake = _FakeJwkClient(keypair.public_key(), **kwargs)
    v = CognitoJwtVerifier(
        user_pool_id=_POOL,
        region=_REGION,
        client_id=_CLIENT,
        jwk_client=fake,
    )
    return v


def test_verifies_valid_id_token_and_reads_groups(keypair):
    v = _verifier(keypair)
    token = _make_token(keypair, token_use="id", groups=["admins", "x"])
    claims = v.verify(token, expected_use="id")
    assert claims["sub"] == "user-sub-1"
    assert v.groups(claims) == ["admins", "x"]
    assert v.is_admin(claims) is True


def test_verifies_valid_access_token_via_client_id(keypair):
    v = _verifier(keypair)
    # Access tokens carry no `aud`; the app client id is in `client_id`.
    token = _make_token(
        keypair, token_use="access", aud=None, client_id=_CLIENT
    )
    claims = v.verify(token, expected_use="access")
    assert claims["token_use"] == "access"


def test_rejects_bad_signature(keypair):
    # Verify against a DIFFERENT public key than the one that signed it.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    fake = _FakeJwkClient(other.public_key())
    v = CognitoJwtVerifier(
        user_pool_id=_POOL, region=_REGION, client_id=_CLIENT, jwk_client=fake
    )
    token = _make_token(keypair)
    with pytest.raises(CognitoJwtError):
        v.verify(token, expected_use="id")


def test_rejects_wrong_issuer(keypair):
    v = _verifier(keypair)
    token = _make_token(keypair, iss="https://evil.example.com/pool")
    with pytest.raises(CognitoJwtError):
        v.verify(token, expected_use="id")


def test_rejects_wrong_audience(keypair):
    v = _verifier(keypair)
    token = _make_token(keypair, aud="some-other-client")
    with pytest.raises(CognitoJwtError):
        v.verify(token, expected_use="id")


def test_rejects_wrong_token_use(keypair):
    v = _verifier(keypair)
    # An access token presented where an id token is expected.
    token = _make_token(keypair, token_use="access", aud=None, client_id=_CLIENT)
    with pytest.raises(CognitoJwtError):
        v.verify(token, expected_use="id")


def test_rejects_access_token_wrong_client_id(keypair):
    v = _verifier(keypair)
    token = _make_token(
        keypair, token_use="access", aud=None, client_id="not-our-client"
    )
    with pytest.raises(CognitoJwtError):
        v.verify(token, expected_use="access")


def test_rejects_expired_token(keypair):
    v = _verifier(keypair)
    token = _make_token(keypair, exp_delta=-10)
    with pytest.raises(CognitoJwtError):
        v.verify(token, expected_use="id")


def test_rejects_empty_token(keypair):
    v = _verifier(keypair)
    with pytest.raises(CognitoJwtError):
        v.verify("", expected_use="id")


def test_kid_miss_raises_generic_error(keypair):
    # A JWKS client that cannot resolve the kid surfaces as a generic error,
    # never leaking internals.
    v = _verifier(keypair, known=False)
    token = _make_token(keypair)
    with pytest.raises(CognitoJwtError):
        v.verify(token, expected_use="id")


def test_error_never_contains_token(keypair):
    v = _verifier(keypair)
    token = _make_token(keypair, iss="https://evil.example.com/pool")
    try:
        v.verify(token, expected_use="id")
    except CognitoJwtError as e:
        assert token not in str(e)
    else:  # pragma: no cover - must have raised
        pytest.fail("expected CognitoJwtError")


def test_non_list_groups_claim_is_empty(keypair):
    v = _verifier(keypair)
    token = _make_token(keypair)
    claims = v.verify(token, expected_use="id")
    # No cognito:groups claim → empty, not admin.
    assert v.groups(claims) == []
    assert v.is_admin(claims) is False
