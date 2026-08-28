"""Default-source resolution + config-form preselect (task 7, R7.1–R7.3).

The config layer exposes a single shared ``DEFAULT_SOURCE = "youtube"`` constant
and an ``effective_default_source`` resolver so an unset/empty
``default_source`` resolves to YouTube (R7.1). The config form preselects
``youtube`` when nothing is stored (R7.2). These tests pin both the pure
resolver and the rendered form.

Requirements: 7.1, 7.2
"""

from __future__ import annotations

import pytest

from config_store import DEFAULT_SOURCE, effective_default_source


def test_default_source_constant_is_youtube() -> None:
    assert DEFAULT_SOURCE == "youtube"


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"default_source": ""},
        {"default_source": "   "},
        {"default_source": None},
        {"bot_name": "DJ"},  # unrelated key, still no source
    ],
)
def test_unset_resolves_to_youtube(config: dict) -> None:
    """An unset/empty/whitespace default source resolves to youtube (R7.1)."""
    assert effective_default_source(config) == "youtube"


@pytest.mark.parametrize("stored", ["spotify", "tidal", "soundcloud", "youtube_music"])
def test_explicit_value_is_preserved(stored: str) -> None:
    """A stored non-empty value is returned unchanged (no override)."""
    assert effective_default_source({"default_source": stored}) == stored


# --------------------------------------------------------------------------- #
# Config form preselect (rendered through the /config route in degraded mode).
# --------------------------------------------------------------------------- #


def _login(client) -> None:
    with client.session_transaction() as sess:
        sess["user"] = {"provider": "discord_oauth"}


def test_config_form_preselects_youtube_when_unset(client) -> None:
    """With no stored config the default-source field preselects youtube (R7.2)."""
    _login(client)
    resp = client.get("/config")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # The default-source input carries value="youtube" when nothing is stored.
    assert 'id="f-default_source"' in html
    assert 'name="default_source"' in html
    assert 'value="youtube"' in html
