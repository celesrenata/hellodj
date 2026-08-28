"""Tests for the pure backfill mapping helpers.

Feature: unified-oauth-and-token-watchdog (task 11).

Covers the side-effect-free helpers in
:mod:`migration_job.source_credential_mapping`: the legacy secret-name parser
(``hellodj/<stage>/guild/<gid>/<provider>`` -> ``(gid, provider)``, rejecting
cross-stage / non-guild / unsupported-provider / malformed names) and the
legacy-JSON -> :class:`~hellodj_platform_logic.source_refresh.TokenState`
mappers (YouTube PoToken pair carried in ``extra``; Spotify refresh-token; Tidal
status-only with a far-future expiry). These are the exact shapes the web-ui
uses on a fresh connect, so a backfilled item is indistinguishable from a freshly
connected one (R2.6, R6.5).
"""

from __future__ import annotations

from migration_job.source_credential_mapping import (
    TIDAL_STATUS_EXPIRES_AT,
    guild_owner_pk,
    guild_secret_prefix,
    legacy_secret_to_token_state,
    parse_guild_secret_name,
    sourcecred_sk,
    user_pk,
)

_STAGE = "beta"


def _guild_secret(guild_id: str, provider: str) -> str:
    return f"hellodj/{_STAGE}/guild/{guild_id}/{provider}"


# ---------------------------------------------------------------------------
# key helpers
# ---------------------------------------------------------------------------


def test_key_helpers_match_unified_store_shape() -> None:
    assert user_pk("sub-1") == "USER#sub-1"
    assert sourcecred_sk("spotify") == "SOURCECRED#spotify"
    assert guild_owner_pk("111") == "GUILD#111"
    assert guild_secret_prefix(_STAGE) == "hellodj/beta/guild/"


# ---------------------------------------------------------------------------
# name parsing
# ---------------------------------------------------------------------------


def test_parse_guild_secret_name_valid() -> None:
    assert parse_guild_secret_name(
        _guild_secret("111", "youtube"), _STAGE
    ) == ("111", "youtube")
    assert parse_guild_secret_name(
        _guild_secret("222", "youtube_music"), _STAGE
    ) == ("222", "youtube_music")


def test_parse_guild_secret_name_rejects_foreign_and_bad_shapes() -> None:
    # Wrong stage (never migrate a cross-stage secret into this table).
    assert parse_guild_secret_name(
        "hellodj/staging/guild/111/youtube", _STAGE
    ) is None
    # A non-guild leaf under the stage.
    assert parse_guild_secret_name("hellodj/beta/tidal-refresh", _STAGE) is None
    # Unsupported provider (soundcloud is search-only, no OAuth).
    assert parse_guild_secret_name(
        _guild_secret("111", "soundcloud"), _STAGE
    ) is None
    # Extra path segment.
    assert parse_guild_secret_name(
        "hellodj/beta/guild/111/youtube/extra", _STAGE
    ) is None
    # Empty guild id.
    assert parse_guild_secret_name(
        "hellodj/beta/guild//youtube", _STAGE
    ) is None


# ---------------------------------------------------------------------------
# legacy-secret -> TokenState mappers
# ---------------------------------------------------------------------------


def test_youtube_mapper_carries_potoken_pair_in_extra() -> None:
    state = legacy_secret_to_token_state(
        "youtube",
        {
            "oauth_refresh_token": "RT",
            "pot_token": "POT",
            "pot_visitor_data": "VD",
        },
    )
    assert state.refresh_token == "RT"
    assert state.access_token == ""
    assert state.expires_at == 0.0
    assert state.extra["pot_token"] == "POT"
    assert state.extra["pot_visitor_data"] == "VD"


def test_spotify_mapper_carries_refresh_and_scope() -> None:
    state = legacy_secret_to_token_state(
        "spotify",
        {"refresh_token": "RT", "scope": "playback", "expires_at": 1234.0},
    )
    assert state.refresh_token == "RT"
    assert state.scope == "playback"
    assert state.expires_at == 1234.0


def test_tidal_mapper_is_status_only_with_far_future_expiry() -> None:
    state = legacy_secret_to_token_state("tidal", {"refresh_token": "RT"})
    assert state.refresh_token == "RT"
    assert state.expires_at == TIDAL_STATUS_EXPIRES_AT
    assert state.extra["owned_by"] == "tidal-stream"


def test_mappers_tolerate_missing_fields() -> None:
    # No fields present -> empty refresh token (caller treats as "empty").
    assert legacy_secret_to_token_state("youtube", {}).refresh_token == ""
    assert legacy_secret_to_token_state("spotify", {}).refresh_token == ""
