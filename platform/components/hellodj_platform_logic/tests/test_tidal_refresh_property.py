"""Property-based test for Property 14: Tidal token refresh via first-party path.

Feature: aws-saas-replatform, Property 14

*For any* Tidal token state, when the token is expired the refresh operation
SHALL produce a non-expired token obtained through the HelloDJ-owned first-party
OAuth integration (single application id), and SHALL never obtain the token
through the legacy two-client-id key-split path.

**Validates: Requirements 9.4**

The test drives :func:`hellodj_platform_logic.tidal_refresh.refresh_tidal` with a
deterministic fake :class:`FirstPartyRefreshClient` that mints a fresh
non-expired token via the single-app-id config. It asserts three things for the
expired case:

    1. the refreshed token is non-expired at ``now`` (R9.4),
    2. it was obtained through the first-party single-app-id path (the fake
       client is configured with ``FIRST_PARTY_SINGLE_APP_ID_MODE`` and records
       every refresh call), and
    3. a client configured with ``LEGACY_KEY_SPLIT_MODE`` can never mint a
       token: :func:`refresh_tidal` raises ``LegacyKeySplitRejectedError`` and
       the legacy client's ``refresh`` is never invoked (R9.3 guarding R9.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.tidal_refresh import (
    FIRST_PARTY_SINGLE_APP_ID_MODE,
    LEGACY_KEY_SPLIT_MODE,
    FirstPartyClientConfig,
    FirstPartyRefreshClient,
    LegacyKeySplitRejectedError,
    TidalTokenState,
    refresh_tidal,
)

# ---------------------------------------------------------------------------
# Fake first-party refresh client (records how the token was obtained)
# ---------------------------------------------------------------------------


@dataclass
class FakeFirstPartyClient:
    """Deterministic fake first-party client used by the property test.

    Mints a fresh token that expires ``ttl`` seconds after the supplied ``now``
    via the single-app-id first-party config, and records every ``refresh``
    call so the test can assert the token was obtained through this path.
    """

    _config: FirstPartyClientConfig
    ttl: float
    minted_refresh_token: str
    refresh_calls: list[tuple[str, float]] = field(default_factory=list)

    @property
    def config(self) -> FirstPartyClientConfig:
        return self._config

    def refresh(self, refresh_token: str, now: float) -> TidalTokenState:
        self.refresh_calls.append((refresh_token, now))
        return TidalTokenState(
            access_token=f"fresh-access-for-{self._config.app_id}",
            refresh_token=self.minted_refresh_token,
            expires_at=now + self.ttl,
        )


# A fake configured for the removed legacy key-split path. Its ``refresh`` must
# never be reached because the guard rejects the config first (R9.3).
@dataclass
class LegacyKeySplitClient:
    """Fake client advertising the legacy two-client-id key-split mode."""

    _config: FirstPartyClientConfig
    refresh_calls: list[tuple[str, float]] = field(default_factory=list)

    @property
    def config(self) -> FirstPartyClientConfig:
        return self._config

    def refresh(self, refresh_token: str, now: float) -> TidalTokenState:
        # Should never be invoked; recording lets the test prove it.
        self.refresh_calls.append((refresh_token, now))
        raise AssertionError("legacy key-split refresh must never be called")


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Finite, well-behaved epoch-second timestamps.
_times = st.floats(
    min_value=0.0,
    max_value=4.0e9,
    allow_nan=False,
    allow_infinity=False,
)

# Non-empty identifier-like strings for app ids and refresh tokens.
_ids = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
    min_size=1,
    max_size=24,
)

# Strictly-positive margin added on top of the skew window so the minted token
# is non-expired even after the skew is subtracted: a fresh token must outlast
# the "about to expire" window used to decide refresh.
_ttl_margins = st.floats(
    min_value=1.0,
    max_value=1.0e6,
    allow_nan=False,
    allow_infinity=False,
)

# Non-negative skew applied when deciding expiry.
_skews = st.floats(
    min_value=0.0,
    max_value=3600.0,
    allow_nan=False,
    allow_infinity=False,
)


@st.composite
def _expired_token_states(draw: st.DrawFn) -> tuple[TidalTokenState, float, float]:
    """Generate an (expired token, now, skew) triple.

    ``expires_at`` is placed at or before ``now + skew`` so the token is
    guaranteed expired under :meth:`TidalTokenState.is_expired`.
    """
    now = draw(_times)
    skew = draw(_skews)
    # Offset back from the expiry boundary so is_expired(now, skew) is True.
    back_off = draw(
        st.floats(
            min_value=0.0,
            max_value=1.0e6,
            allow_nan=False,
            allow_infinity=False,
        )
    )
    expires_at = (now + skew) - back_off
    state = TidalTokenState(
        access_token=draw(st.text(max_size=24)),
        refresh_token=draw(_ids),
        expires_at=expires_at,
    )
    return state, now, skew


# ---------------------------------------------------------------------------
# Property 14
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(
    triple=_expired_token_states(),
    app_id=_ids,
    minted_refresh=_ids,
    ttl_margin=_ttl_margins,
)
def test_expired_token_refreshes_via_first_party_single_app_id(
    triple: tuple[TidalTokenState, float, float],
    app_id: str,
    minted_refresh: str,
    ttl_margin: float,
) -> None:
    """Feature: aws-saas-replatform, Property 14.

    When the token is expired, the refresh yields a non-expired token obtained
    through the first-party single-app-id path.
    """
    token_state, now, skew = triple

    # Precondition of this branch: the supplied token really is expired.
    assert token_state.is_expired(now, skew)

    # A fresh token must outlast the skew window used to decide expiry, so the
    # minted TTL is strictly greater than the skew (skew + positive margin).
    ttl = skew + ttl_margin

    config = FirstPartyClientConfig(
        app_id=app_id,
        callback_url="https://hellodj.bot/oauth/tidal/callback",
        auth_mode=FIRST_PARTY_SINGLE_APP_ID_MODE,
    )
    client = FakeFirstPartyClient(
        _config=config,
        ttl=ttl,
        minted_refresh_token=minted_refresh,
    )
    assert isinstance(client, FirstPartyRefreshClient)

    result = refresh_tidal(token_state, client, now, skew_seconds=skew)

    # 1. The refreshed token must be non-expired at ``now`` (R9.4).
    assert not result.is_expired(now, skew)

    # 2. It was obtained through the first-party single-app-id path: the
    #    single-app-id client's refresh was invoked exactly once with the prior
    #    refresh token, and the minted access token carries that single app id.
    assert client.config.auth_mode == FIRST_PARTY_SINGLE_APP_ID_MODE
    assert client.refresh_calls == [(token_state.refresh_token, now)]
    assert result.access_token == f"fresh-access-for-{app_id}"

    # A usable refresh token is carried forward for the next cycle (R9.4).
    assert result.refresh_token


@settings(max_examples=200)
@given(
    triple=_expired_token_states(),
    app_id=_ids,
)
def test_legacy_key_split_never_mints_a_token(
    triple: tuple[TidalTokenState, float, float],
    app_id: str,
) -> None:
    """Feature: aws-saas-replatform, Property 14.

    An expired token must NEVER be refreshed through the legacy two-client-id
    key-split path: the guard rejects it and the legacy client is never called.
    """
    token_state, now, skew = triple
    assert token_state.is_expired(now, skew)

    legacy_config = FirstPartyClientConfig(
        app_id=app_id,
        callback_url="https://hellodj.bot/oauth/tidal/callback",
        auth_mode=LEGACY_KEY_SPLIT_MODE,
    )
    legacy_client = LegacyKeySplitClient(_config=legacy_config)

    with pytest.raises(LegacyKeySplitRejectedError):
        refresh_tidal(token_state, legacy_client, now, skew_seconds=skew)

    # The legacy path must never reach the token-minting call.
    assert legacy_client.refresh_calls == []
