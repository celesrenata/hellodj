"""Cognito JWT (id / access token) verification for the first-party auth forms.

The first-party login/registration/recovery forms (see ``auth.py`` +
``cognito_auth.py``) call Cognito ``InitiateAuth`` /
``RespondToAuthChallenge`` server-side and receive JWTs. Unlike the retired
hosted-UI callback — which read the ``cognito:groups`` claim from a token that
came straight from the token endpoint over TLS in the same request WITHOUT
verifying its signature — the custom-forms path broadens the trust surface, so
this module verifies every token's RS256 signature against the pool JWKS and
checks the standard claims BEFORE any claim (notably group membership) is
trusted. (custom-auth-forms design; Requirements 4.1, 4.2, 4.3.)

Dependency choice (task 1): ``PyJWT`` (with ``cryptography`` as its RS256
backend) is used rather than a hand-rolled RS256 verify. ``PyJWKClient`` fetches
the pool's ``.well-known/jwks.json`` and caches signing keys by ``kid`` with a
bounded lifespan, refetching on a ``kid`` miss — exactly the caching behaviour
the design requires, from a maintained library rather than bespoke crypto.

The verifier degrades to ``None`` when the pool id/region are unconfigured, so
the auth routes render an "auth unavailable" state (matching the other service
modules) rather than crashing.

Requirements: 4.1, 4.2, 4.3
"""

from __future__ import annotations

from typing import Any

import jwt
from jwt import InvalidTokenError, PyJWKClient

__all__ = [
    "CognitoJwtError",
    "CognitoJwtVerifier",
    "build_verifier",
    "ADMIN_GROUP",
]

#: The Cognito group whose members are administrators (mirrors admin_directory).
ADMIN_GROUP = "admins"

#: JWKS signing-key cache lifespan (seconds). PyJWKClient refreshes on a kid
#: miss regardless, so a rotated key is picked up without waiting this out.
_JWKS_CACHE_TTL_SECONDS = 3600


class CognitoJwtError(Exception):
    """Raised when a Cognito JWT fails signature or claim verification.

    The message is intentionally generic and never contains the token, so a
    caller can surface a non-enumerating auth error without leaking material.
    """


class CognitoJwtVerifier:
    """Verify Cognito id/access tokens against the pool JWKS (RS256).

    Args:
        user_pool_id: The Cognito user pool id (e.g. ``us-east-1_C6xFPZt4x``).
        region: The pool's AWS region.
        client_id: The app client id, checked against ``aud`` (id token) or
            ``client_id`` (access token).
        jwk_client: Injected :class:`PyJWKClient` (tests supply a fake); one is
            built from the derived issuer JWKS URL when omitted.
    """

    def __init__(
        self,
        *,
        user_pool_id: str,
        region: str,
        client_id: str,
        jwk_client: PyJWKClient | None = None,
    ) -> None:
        self._issuer = (
            f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        )
        self._client_id = client_id
        self._jwks = jwk_client or PyJWKClient(
            f"{self._issuer}/.well-known/jwks.json",
            cache_keys=True,
            lifespan=_JWKS_CACHE_TTL_SECONDS,
        )

    def verify(self, token: str, *, expected_use: str) -> dict[str, Any]:
        """Verify ``token`` and return its claims, or raise.

        Verifies the RS256 signature against the pool JWKS and the standard
        claims: ``iss`` matches the pool issuer, ``exp`` is in the future,
        ``token_use`` equals ``expected_use`` (``id`` or ``access``), and the
        audience matches the app client id (``aud`` for id tokens, ``client_id``
        for access tokens — access tokens carry no ``aud``).

        Args:
            token: The compact-serialized JWT from Cognito.
            expected_use: The required ``token_use`` (``"id"`` or ``"access"``).

        Returns:
            The verified claim set.

        Raises:
            CognitoJwtError: On any signature or claim failure. The message is
                generic and never includes the token.
        """
        if not token:
            raise CognitoJwtError("no token to verify")
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            # Access tokens have no `aud`; verify audience manually via the
            # `client_id` claim below, so disable PyJWT's aud check for them.
            verify_aud = expected_use == "id"
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._client_id if verify_aud else None,
                options={
                    "require": ["exp", "iss"],
                    "verify_aud": verify_aud,
                    "verify_exp": True,
                    "verify_iss": True,
                },
            )
        except InvalidTokenError as error:
            # Normalize every PyJWT failure (bad sig, wrong iss/aud, expired,
            # malformed) to one generic error; never echo the token.
            raise CognitoJwtError("token verification failed") from error
        except Exception as error:  # noqa: BLE001 - JWKS fetch / kid miss / etc.
            raise CognitoJwtError("token verification failed") from error

        if claims.get("token_use") != expected_use:
            raise CognitoJwtError("unexpected token_use")
        # Access tokens carry the app client id in `client_id`, not `aud`.
        if expected_use == "access" and claims.get("client_id") != self._client_id:
            raise CognitoJwtError("unexpected client_id")
        return claims

    def groups(self, claims: dict[str, Any]) -> list[str]:
        """Return the ``cognito:groups`` claim as a list (verified claims only).

        Pass ONLY a claim set returned by :meth:`verify`; reading groups from an
        unverified token would defeat the admin gate.
        """
        raw = claims.get("cognito:groups", [])
        return list(raw) if isinstance(raw, list) else []

    def is_admin(self, claims: dict[str, Any]) -> bool:
        """Return whether the verified claims place the user in ``admins``."""
        return ADMIN_GROUP in self.groups(claims)


def build_verifier(
    *, user_pool_id: str, region: str, client_id: str
) -> CognitoJwtVerifier | None:
    """Build a :class:`CognitoJwtVerifier`, or ``None`` when unconfigured.

    Returns ``None`` (degraded mode — auth routes render "auth unavailable")
    unless a user pool id, region, and client id are all present.
    """
    if not user_pool_id or not region or not client_id:
        return None
    return CognitoJwtVerifier(
        user_pool_id=user_pool_id, region=region, client_id=client_id
    )
