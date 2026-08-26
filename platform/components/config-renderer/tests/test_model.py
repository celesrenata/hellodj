"""Unit tests for the config-renderer input dataclasses."""

from __future__ import annotations

from config_renderer.model import LavalinkCredentials, LavalinkSettings


def test_credentials_from_flat_keys() -> None:
    creds = LavalinkCredentials.from_secret(
        {
            "spotify_client_id": "sid",
            "spotify_client_secret": "ssecret",
            "tidal_token": "ttok",
            "ytcipher_token": "yct",
            "youtube_oauth_refresh_token": "rt",
        }
    )
    assert creds.spotify_client_id == "sid"
    assert creds.spotify_client_secret == "ssecret"
    assert creds.tidal_token == "ttok"
    assert creds.ytcipher_token == "yct"
    assert creds.youtube_oauth_refresh_token == "rt"


def test_credentials_from_legacy_dotted_keys() -> None:
    creds = LavalinkCredentials.from_secret(
        {
            "spotify.client_id": "sid",
            "tidal.td_client_id": "tcid",
            "tidal.td_client_secret": "tsec",
            "youtube.pot_token": "pot",
            "youtube.pot_visitor_data": "vd",
        }
    )
    assert creds.spotify_client_id == "sid"
    assert creds.tidal_client_id == "tcid"
    assert creds.tidal_client_secret == "tsec"
    assert creds.youtube_pot_token == "pot"
    assert creds.youtube_pot_visitor_data == "vd"


def test_credentials_default_empty() -> None:
    creds = LavalinkCredentials.from_secret({})
    assert creds.spotify_client_id == ""
    assert creds.tidal_token == ""


def test_settings_defaults_when_empty() -> None:
    settings = LavalinkSettings.from_config({})
    assert settings.host == "0.0.0.0"
    assert settings.port == 2333
    assert settings.tidal_country_code == "US"
    assert settings.lavasrc_providers[0] == "scsearch:%QUERY%"


def test_settings_overrides_and_coercion() -> None:
    settings = LavalinkSettings.from_config(
        {
            "port": "2444",
            "tidal_search_limit": "10",
            "spotify_country_code": "GB",
            "lavasrc_providers": ["ytsearch:%QUERY%"],
        }
    )
    assert settings.port == 2444
    assert settings.tidal_search_limit == 10
    assert settings.spotify_country_code == "GB"
    assert settings.lavasrc_providers == ("ytsearch:%QUERY%",)


def test_settings_bad_int_falls_back() -> None:
    settings = LavalinkSettings.from_config({"port": "not-a-number"})
    assert settings.port == 2333
