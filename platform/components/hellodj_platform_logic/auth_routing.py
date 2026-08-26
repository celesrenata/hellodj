"""Auth-routing-by-purpose decision logic for the HelloDJ AWS platform.

Every authentication request is routed by its *purpose* (the auth-routing
invariant from the design). This module is the single source of truth for that
routing so both the CDK infrastructure layer and the runtime ``web-ui``
component agree on which identity provider handles which request.

Routing rules (design "Auth routing rule (invariant)"):

* Administrator authentication, initial registration, and account recovery
  route to :attr:`AuthProvider.COGNITO`.
* Day-to-day login of a registered or appointed user routes to
  :attr:`AuthProvider.DISCORD_OAUTH` by default.
* Tidal source authentication routes to :attr:`AuthProvider.TIDAL_FIRST_PARTY`
  and is fully independent of Cognito -- it MUST never route to Cognito.

The function is pure: it depends only on its inputs and performs no live AWS
calls, so it can be imported anywhere and exercised directly by property tests
(Property 2).

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 9.5
"""

from __future__ import annotations

from .types import AuthProvider, AuthPurpose, UserType

__all__ = ["route_auth"]

#: Purposes that always resolve to Cognito regardless of user type: the
#: administrator authentication, initial registration and account recovery
#: flows the platform retains on Cognito (R8.2, R8.3, R8.5, R8.6).
_COGNITO_PURPOSES: frozenset[AuthPurpose] = frozenset(
    {
        AuthPurpose.ADMIN_AUTH,
        AuthPurpose.INITIAL_REGISTRATION,
        AuthPurpose.ACCOUNT_RECOVERY,
    }
)

#: User types eligible for default day-to-day Discord OAuth login: registered
#: users and appointed users (R8.4).
_DISCORD_LOGIN_USER_TYPES: frozenset[UserType] = frozenset(
    {
        UserType.REGISTERED,
        UserType.APPOINTED,
    }
)


def route_auth(purpose: AuthPurpose, user_type: UserType) -> AuthProvider:
    """Route an authentication request to its identity provider by purpose.

    The request is characterized by its ``purpose`` and the ``user_type`` it is
    made on behalf of. Routing is driven primarily by ``purpose``; ``user_type``
    only selects the day-to-day login provider.

    Args:
        purpose: Why the authentication is being performed.
        user_type: The category of user the request is made for.

    Returns:
        The identity provider that SHALL handle the request:

        * :attr:`~.types.AuthProvider.COGNITO` for administrator
          authentication, initial registration and account recovery
          (R8.2, R8.3, R8.5, R8.6).
        * :attr:`~.types.AuthProvider.DISCORD_OAUTH` for day-to-day login of a
          registered or appointed user (R8.4).
        * :attr:`~.types.AuthProvider.TIDAL_FIRST_PARTY` for Tidal source
          authentication, which operates independently of Cognito and never
          routes to it (R9.5).

    Raises:
        ValueError: If ``purpose`` is :attr:`~.types.AuthPurpose.DAY_TO_DAY_LOGIN`
            for a user type that is not eligible for day-to-day login (for
            example an anonymous user), or if an unrecognized purpose is given.
    """
    # Tidal source auth is fully independent of Cognito and always routes to the
    # first-party Tidal OAuth (R9.5). Checked first so it can never fall through
    # to any Cognito branch.
    if purpose is AuthPurpose.TIDAL_SOURCE_AUTH:
        return AuthProvider.TIDAL_FIRST_PARTY

    # Admin auth, initial registration and account recovery route to Cognito
    # regardless of user type (R8.2, R8.3, R8.5, R8.6).
    if purpose in _COGNITO_PURPOSES:
        return AuthProvider.COGNITO

    # Default day-to-day login of a registered or appointed user routes to
    # Discord OAuth (R8.4).
    if purpose is AuthPurpose.DAY_TO_DAY_LOGIN:
        if user_type in _DISCORD_LOGIN_USER_TYPES:
            return AuthProvider.DISCORD_OAUTH
        raise ValueError(
            "day-to-day login is only available to registered or appointed "
            f"users, not {user_type.value!r}"
        )

    raise ValueError(f"unrecognized authentication purpose: {purpose!r}")
