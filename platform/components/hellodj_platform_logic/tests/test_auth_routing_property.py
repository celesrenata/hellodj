"""Property test for auth-routing by purpose (task 3.2).

Property 2 (design "Auth routing rule (invariant)"): every authentication
request, characterized by ``(purpose, user_type)``, is routed by *purpose*:

* administrator authentication, initial registration and account recovery
  route to :attr:`AuthProvider.COGNITO`;
* day-to-day login of a *registered* or *appointed* user routes to
  :attr:`AuthProvider.DISCORD_OAUTH` (an ineligible user type -- admin or
  anonymous -- raises :class:`ValueError` rather than routing anywhere); and
* Tidal source authentication routes to :attr:`AuthProvider.TIDAL_FIRST_PARTY`
  and SHALL never route to Cognito.

The routing function is pure, so the property is exercised directly over the
full ``(purpose, user_type)`` space with Hypothesis ``sampled_from`` generators
(>=100 iterations).

Feature: aws-saas-replatform, Property 2

Validates: Requirements 8.2, 8.3, 8.4, 8.5, 8.6, 9.5
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.auth_routing import route_auth
from hellodj_platform_logic.types import AuthProvider, AuthPurpose, UserType

# Purposes that must resolve to Cognito regardless of user type
# (R8.2 admin, R8.3 registration, R8.5/R8.6 recovery).
_COGNITO_PURPOSES = frozenset(
    {
        AuthPurpose.ADMIN_AUTH,
        AuthPurpose.INITIAL_REGISTRATION,
        AuthPurpose.ACCOUNT_RECOVERY,
    }
)

# User types eligible for default day-to-day Discord OAuth login (R8.4).
_DISCORD_LOGIN_USER_TYPES = frozenset({UserType.REGISTERED, UserType.APPOINTED})

_purposes = st.sampled_from(list(AuthPurpose))
_user_types = st.sampled_from(list(UserType))


@settings(max_examples=200)
@given(purpose=_purposes, user_type=_user_types)
def test_auth_routing_by_purpose(
    purpose: AuthPurpose, user_type: UserType
) -> None:
    """Routing obeys the by-purpose invariant across all (purpose, user_type).

    Feature: aws-saas-replatform, Property 2

    Validates: Requirements 8.2, 8.3, 8.4, 8.5, 8.6, 9.5
    """
    # Tidal source auth is independent of Cognito and always routes to the
    # first-party Tidal OAuth -- it must NEVER route to Cognito (R9.5).
    if purpose is AuthPurpose.TIDAL_SOURCE_AUTH:
        result = route_auth(purpose, user_type)
        assert result is AuthProvider.TIDAL_FIRST_PARTY
        assert result is not AuthProvider.COGNITO
        return

    # Admin auth / initial registration / account recovery -> Cognito for every
    # user type (R8.2, R8.3, R8.5, R8.6).
    if purpose in _COGNITO_PURPOSES:
        assert route_auth(purpose, user_type) is AuthProvider.COGNITO
        return

    # Day-to-day login: registered/appointed -> Discord OAuth (R8.4); an
    # ineligible user type (admin/anonymous) raises rather than routing to
    # Cognito or anywhere else.
    assert purpose is AuthPurpose.DAY_TO_DAY_LOGIN
    if user_type in _DISCORD_LOGIN_USER_TYPES:
        assert route_auth(purpose, user_type) is AuthProvider.DISCORD_OAUTH
    else:
        with pytest.raises(ValueError):
            route_auth(purpose, user_type)


@settings(max_examples=200)
@given(user_type=_user_types)
def test_tidal_source_auth_never_routes_to_cognito(
    user_type: UserType,
) -> None:
    """Tidal source auth never resolves to Cognito for any user type (R9.5).

    Feature: aws-saas-replatform, Property 2

    Validates: Requirements 9.5
    """
    assert (
        route_auth(AuthPurpose.TIDAL_SOURCE_AUTH, user_type)
        is not AuthProvider.COGNITO
    )
