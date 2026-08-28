"""Property + unit tests for the unified source refresh contract + clients.

Feature: unified-oauth-and-token-watchdog.

Covers **Correctness Property 4 (refresh soundness)**, mirroring the Tidal
Property 14 test (``test_tidal_refresh_property.py``) for the generalized
contract and each concrete provider client:

    * :func:`apply_refresh` returns a non-expired token or raises; a provider
      that does not rotate its refresh token keeps the prior one; an
      already-expired result is a failure, not a stored success.
      **Validates: Requirements 4.3, 4.4**
    * :class:`GoogleRefreshClient` and :class:`SpotifyRefreshClient` build a
      fresh :class:`TokenState` from a ``grant_type=refresh_token`` form POST
      (injected, no network), preserving the prior refresh token when the
      provider does not rotate. **Validates: Requirements 4.2, 4.3**
    * :class:`TidalRefreshClient` delegates to the EXISTING first-party
      ``refresh_tidal`` so Tidal behavior is untouched. **Validates: Requirements 4.5, 10.2**

The injected fakes model each provider deterministically so the property test
exercises the real decision path with no live network calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.source_refresh import (
    GOOGLE_TOKEN_URL,
    PROVIDER_SPOTIFY,
    PROVIDER_TIDAL,
    PROVIDER_YOUTUBE,
    SPOTIFY_TOKEN_URL,
    GoogleRefreshClient,
    RefreshClient,
    RefreshFailedError,
    SpotifyRefreshClient,
    TidalRefreshClient,
    TokenState,
    apply_refresh,
    needs_refresh,
)
from hellodj_platform_logic.tidal_refresh import (
    FIRST_PARTY_SINGLE_APP_ID_MODE,
    FirstPartyClientConfig,
    TidalTokenState,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class FakeClient:
    """Deterministic unified RefreshClient that mints a token ``ttl`` in future.

    ``rotate`` controls whether it returns a fresh refresh token (rotation) or
    an empty one (so ``apply_refresh`` must preserve the prior token).
    """

    provider: str
    ttl: float
    rotate: bool = True
    minted_refresh_token: str = "rotated-refresh"
    refresh_calls: list[tuple[str, float]] = field(default_factory=list)

    def refresh(self, refresh_token: str, now: float) -> TokenState:
        self.refresh_calls.append((refresh_token, now))
        return TokenState(
            access_token=f"fresh-access-{self.provider}",
            refresh_token=self.minted_refresh_token if self.rotate else "",
            expires_at=now + self.ttl,
        )


@dataclass
class FakeFormPoster:
    """Records the URL + form fields and returns a canned OAuth token response."""

    response: dict[str, object]
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def __call__(self, url: str, fields: dict[str, str]) -> dict[str, object]:
        self.calls.append((url, dict(fields)))
        return dict(self.response)


@dataclass
class FakeFirstPartyTidalClient:
    """Fake first-party Tidal client mirroring the Tidal property-test fake."""

    _config: FirstPartyClientConfig
    ttl: float
    minted_refresh_token: str = "tidal-refresh"
    refresh_calls: list[tuple[str, float]] = field(default_factory=list)

    @property
    def config(self) -> FirstPartyClientConfig:
        return self._config

    def refresh(self, refresh_token: str, now: float) -> TidalTokenState:
        self.refresh_calls.append((refresh_token, now))
        return TidalTokenState(
            access_token=f"tidal-access-{self._config.app_id}",
            refresh_token=self.minted_refresh_token,
            expires_at=now + self.ttl,
        )


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_times = st.floats(min_value=0.0, max_value=4.0e9, allow_nan=False, allow_infinity=False)
_ids = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
    min_size=1,
    max_size=24,
)
_ttl_margins = st.floats(min_value=1.0, max_value=1.0e6, allow_nan=False, allow_infinity=False)
_skews = st.floats(min_value=0.0, max_value=3600.0, allow_nan=False, allow_infinity=False)


@st.composite
def _expired_token_states(draw: st.DrawFn) -> tuple[TokenState, float, float]:
    """Generate an (expired token, now, skew) triple guaranteed expired."""
    now = draw(_times)
    skew = draw(_skews)
    back_off = draw(
        st.floats(min_value=0.0, max_value=1.0e6, allow_nan=False, allow_infinity=False)
    )
    expires_at = (now + skew) - back_off
    state = TokenState(
        access_token=draw(st.text(max_size=24)),
        refresh_token=draw(_ids),
        expires_at=expires_at,
    )
    return state, now, skew


# ---------------------------------------------------------------------------
# Property 4 — apply_refresh soundness (contract level)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(triple=_expired_token_states(), ttl_margin=_ttl_margins, rotate=st.booleans())
def test_expired_token_refreshes_to_non_expired(
    triple: tuple[TokenState, float, float],
    ttl_margin: float,
    rotate: bool,
) -> None:
    """Property 4: an expired token refreshes to a non-expired token.

    A provider that does not rotate keeps the prior refresh token (R4.3).
    **Validates: Requirements 4.3, 4.4**
    """
    state, now, skew = triple
    assert needs_refresh(state, now, skew)

    ttl = skew + ttl_margin
    client = FakeClient(provider=PROVIDER_YOUTUBE, ttl=ttl, rotate=rotate)
    assert isinstance(client, RefreshClient)

    result = apply_refresh(state, client, now, skew=skew)

    # Non-expired at now (R4.4).
    assert not result.is_expired(now, skew)
    # The client was called once with the prior refresh token.
    assert client.refresh_calls == [(state.refresh_token, now)]
    # Non-rotating providers keep the prior refresh token (R4.3).
    if rotate:
        assert result.refresh_token == "rotated-refresh"
    else:
        assert result.refresh_token == state.refresh_token
    # A usable refresh token is always carried forward.
    assert result.refresh_token


@settings(max_examples=200)
@given(triple=_expired_token_states())
def test_expired_result_is_a_failure(triple: tuple[TokenState, float, float]) -> None:
    """Property 4: an already-expired minted token is a failure, not success.

    **Validates: Requirements 4.4**
    """
    state, now, skew = triple
    # ttl=0 with skew>=0 → minted token expires at exactly now, i.e. expired.
    client = FakeClient(provider=PROVIDER_SPOTIFY, ttl=0.0)
    with pytest.raises(RefreshFailedError):
        apply_refresh(state, client, now, skew=skew)


@settings(max_examples=200)
@given(now=_times, ttl_margin=_ttl_margins, skew=_skews, refresh=_ids)
def test_valid_token_fast_paths_without_calling_client(
    now: float,
    ttl_margin: float,
    skew: float,
    refresh: str,
) -> None:
    """Property 4: a still-valid token is returned unchanged (no client call)."""
    # Token expires strictly beyond the skew window → still valid.
    state = TokenState(
        access_token="live",
        refresh_token=refresh,
        expires_at=now + skew + ttl_margin,
    )
    client = FakeClient(provider=PROVIDER_YOUTUBE, ttl=ttl_margin)
    result = apply_refresh(state, client, now, skew=skew)
    assert result is state
    assert client.refresh_calls == []


@settings(max_examples=100)
@given(now=_times, ttl_margin=_ttl_margins, skew=_skews, refresh=_ids)
def test_force_refreshes_even_when_valid(
    now: float,
    ttl_margin: float,
    skew: float,
    refresh: str,
) -> None:
    """Property 4: ``force=True`` refreshes even a still-valid token."""
    state = TokenState(
        access_token="live",
        refresh_token=refresh,
        expires_at=now + skew + ttl_margin,
    )
    client = FakeClient(provider=PROVIDER_YOUTUBE, ttl=skew + ttl_margin)
    result = apply_refresh(state, client, now, skew=skew, force=True)
    assert client.refresh_calls == [(refresh, now)]
    assert not result.is_expired(now, skew)


# ---------------------------------------------------------------------------
# Google / Spotify clients — form-post + response mapping
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(refresh=_ids, expires_in=st.integers(min_value=1, max_value=100000), now=_times)
def test_google_client_builds_token_from_form_post(
    refresh: str,
    expires_in: int,
    now: float,
) -> None:
    """Google client posts the refresh grant and maps the response (R4.2).

    Google does not rotate the refresh token, so the prior one is preserved
    (R4.3). **Validates: Requirements 4.2, 4.3**
    """
    poster = FakeFormPoster(
        response={"access_token": "at-google", "expires_in": expires_in, "scope": "yt"}
    )
    client = GoogleRefreshClient(
        client_id="cid",
        client_secret="csec",
        provider=PROVIDER_YOUTUBE,
        http_post=poster,
    )
    result = client.refresh(refresh, now)

    url, fields = poster.calls[0]
    assert url == GOOGLE_TOKEN_URL
    assert fields["grant_type"] == "refresh_token"
    assert fields["refresh_token"] == refresh
    assert fields["client_id"] == "cid"
    assert result.access_token == "at-google"
    assert result.expires_at == now + float(expires_in)
    # No rotated refresh_token in the response → prior preserved (R4.3).
    assert result.refresh_token == refresh
    assert result.scope == "yt"


@settings(max_examples=200)
@given(
    refresh=_ids,
    rotated=_ids,
    expires_in=st.integers(min_value=1, max_value=100000),
    now=_times,
)
def test_spotify_client_uses_rotated_refresh_when_present(
    refresh: str,
    rotated: str,
    expires_in: int,
    now: float,
) -> None:
    """Spotify client uses a rotated refresh token when the response has one."""
    poster = FakeFormPoster(
        response={
            "access_token": "at-spotify",
            "refresh_token": rotated,
            "expires_in": expires_in,
        }
    )
    client = SpotifyRefreshClient(client_id="cid", client_secret="csec", http_post=poster)
    result = client.refresh(refresh, now)

    assert poster.calls[0][0] == SPOTIFY_TOKEN_URL
    assert client.provider == PROVIDER_SPOTIFY
    assert result.refresh_token == rotated
    assert result.access_token == "at-spotify"


def test_client_response_without_access_token_raises() -> None:
    """A provider response missing ``access_token`` raises ProviderTokenError."""
    from hellodj_platform_logic.source_refresh import ProviderTokenError

    poster = FakeFormPoster(response={"expires_in": 3600})
    client = GoogleRefreshClient(client_id="c", client_secret="s", http_post=poster)
    with pytest.raises(ProviderTokenError):
        client.refresh("refresh", 0.0)


# ---------------------------------------------------------------------------
# Tidal adapter — delegates to existing first-party logic (R4.5, R10.2)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(triple=_expired_token_states(), app_id=_ids, ttl_margin=_ttl_margins)
def test_tidal_adapter_delegates_to_first_party(
    triple: tuple[TokenState, float, float],
    app_id: str,
    ttl_margin: float,
) -> None:
    """Property 7/4: the Tidal adapter routes through ``refresh_tidal`` (R4.5).

    **Validates: Requirements 4.5, 10.2**
    """
    state, now, skew = triple
    ttl = skew + ttl_margin
    config = FirstPartyClientConfig(
        app_id=app_id,
        callback_url="https://hellodj.bot/oauth/tidal/callback",
        auth_mode=FIRST_PARTY_SINGLE_APP_ID_MODE,
    )
    first_party = FakeFirstPartyTidalClient(_config=config, ttl=ttl)
    client = TidalRefreshClient(first_party_client=first_party)
    assert isinstance(client, RefreshClient)
    assert client.provider == PROVIDER_TIDAL

    result = apply_refresh(state, client, now, skew=skew)

    # The first-party client was invoked with the prior refresh token (R4.5).
    assert first_party.refresh_calls == [(state.refresh_token, now)]
    assert result.access_token == f"tidal-access-{app_id}"
    assert not result.is_expired(now, skew)
    assert result.refresh_token == "tidal-refresh"
