"""Bug-condition EXPLORATION test — no per-guild YouTube resolution (1.5).

Task 1 of the ``bot-identity-and-source-auth`` bugfix spec (bot side). Unlike
the web-ui / infra exploration assertions (which FAIL on unfixed code to prove
the defect), this test DOCUMENTS requirement 1.5 by pinning the current
resolver behavior for YouTube:

* ``GLOBAL_FALLBACK_LEAVES`` has NO ``youtube`` / ``youtube_music`` key, and
* ``GuildCredentialResolver.resolve(gid, "youtube")`` returns ``None`` when the
  guild has no per-guild YouTube secret (using ``FakeSecrets({})``).

This specific no-global-leaf behavior is INTENTIONAL and MUST STAY true for
guilds without a per-guild secret (Requirements 3.5 / 3.7): a guild that has
not connected its own YouTube account plays via the untouched GLOBAL
credential-store ``push_youtube_oauth`` path. So this test PASSES on unfixed
code and is re-used as a preservation baseline in Task 2 — the fix (Task 6)
must NOT add a youtube global fallback leaf.

Mirrors the ``FakeSecrets`` style of ``test_guild_credentials.py``; bare imports
rely on ``bot/playback`` being on ``sys.path`` (run pytest from there).

Validates: Requirements 1.5, 3.5, 3.7
"""

from __future__ import annotations

import json

import pytest
from guild_credentials import (
    GLOBAL_FALLBACK_LEAVES,
    GuildCredentialResolver,
)

STAGE = "beta"


class FakeSecrets:
    """Fake secretsmanager client backed by an in-memory name → dict store.

    Matches ``test_guild_credentials.FakeSecrets``: records every requested
    secret name and raises (as boto3 would) when a name is absent.
    """

    def __init__(self, store: dict[str, object] | None = None) -> None:
        self.store: dict[str, object] = dict(store or {})
        self.calls: list[str] = []

    def get_secret_value(self, **kwargs: object) -> dict[str, object]:
        name = str(kwargs["SecretId"])
        self.calls.append(name)
        if name not in self.store:
            raise KeyError(f"Secrets Manager can't find {name}")
        value = self.store[name]
        raw = value if isinstance(value, str) else json.dumps(value)
        return {"SecretString": raw}


def _resolver(secrets: FakeSecrets) -> GuildCredentialResolver:
    return GuildCredentialResolver(secrets, stage=STAGE)


# ── 1.5 / 3.5 / 3.7 — no per-guild YouTube resolution, no global leaf ──────


class TestNoPerGuildYouTubeResolution:
    @pytest.mark.parametrize("provider", ["youtube", "youtube_music"])
    def test_no_global_fallback_leaf_for_youtube(self, provider):
        """YouTube providers have no global fallback leaf (documents 1.5).

        This MUST STAY true after the fix (3.5/3.7): a guild without its own
        YouTube secret must fall through to the global push path, not a
        resolver global leaf.
        """
        assert provider not in GLOBAL_FALLBACK_LEAVES

    @pytest.mark.parametrize("provider", ["youtube", "youtube_music"])
    def test_resolve_returns_none_without_guild_secret(self, provider):
        """resolve(gid, youtube) → None when no per-guild secret exists (1.5).

        With an empty secret store the resolver attempts only the guild-scoped
        secret and returns ``None`` — the guild's YouTube playback credentials
        cannot be resolved per-guild today. Preserved for guilds without a
        secret (3.5).
        """
        secrets = FakeSecrets({})
        r = _resolver(secrets)

        assert r.resolve("111", provider) is None
        # Only the guild secret was attempted; no global YouTube lookup exists.
        assert secrets.calls == [f"hellodj/{STAGE}/guild/111/{provider}"]

    def test_global_fallback_leaves_exactly_tidal_and_spotify(self):
        """The fallback map is exactly {tidal, spotify} (3.7 baseline)."""
        assert GLOBAL_FALLBACK_LEAVES == {
            "tidal": "tidal-refresh",
            "spotify": "spotify",
        }
