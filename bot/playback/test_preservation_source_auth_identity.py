"""Preservation PROPERTY tests (bot side) — bot-identity-and-source-auth.

Task 2 of the ``bot-identity-and-source-auth`` bugfix spec (bot side). Following
the observation-first methodology: these tests capture behavior OBSERVED on the
CURRENT (unfixed) code as a baseline (Property 2: Preservation). They MUST PASS
on unfixed code, and MUST STILL PASS after the fix (Task 6) — i.e. they guard
against regressions in the global fallback path.

Complements ``test_bug_condition_youtube_resolution.py`` (which pins the YouTube
no-global-leaf / resolve→None baseline for 3.5/3.7). Here we cover:

* **Global fallback leaves preserved (3.7):** for ARBITRARY guilds without a
  per-guild secret, ``resolve(gid, "tidal")`` resolves the global
  ``tidal-refresh`` leaf and ``resolve(gid, "spotify")`` resolves the global
  ``spotify`` leaf, and ``GLOBAL_FALLBACK_LEAVES == {"tidal": "tidal-refresh",
  "spotify": "spotify"}``.
* **Global YouTube playback preserved (3.5):** for ARBITRARY guilds without a
  YouTube secret, ``resolve(gid, "youtube")`` (and ``youtube_music``) returns
  ``None`` — so the bot uses the existing global ``push_youtube_oauth`` single
  ``POST /youtube`` path, untouched.
* **DVD-visualizer avatar read preserved (3.6):** ``bot/cogs/video.py`` still
  reads ``self.bot.user.avatar`` (static source assertion), unaffected by the
  new per-guild identity feature.

Mirrors the ``FakeSecrets`` style of ``test_guild_credentials.py``; bare imports
rely on ``bot/playback`` being on ``sys.path`` (run pytest from there).

Validates: Requirements 3.5, 3.6, 3.7
"""

from __future__ import annotations

import json
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from guild_credentials import (
    GLOBAL_FALLBACK_LEAVES,
    GuildCredentialResolver,
    guild_source_secret_name,
)

STAGE = "beta"

# Guild ids are numeric strings; keep them constrained but arbitrary.
_GID = st.integers(min_value=1, max_value=10**18).map(str)


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


# ── 3.7 — global fallback leaves preserved (property over arbitrary guilds) ─


class TestGlobalFallbackLeavesPreserved:
    def test_fallback_map_is_exactly_tidal_and_spotify(self):
        """The fallback map is EXACTLY {tidal, spotify} (3.7 constant baseline).

        The fix (Task 6) must NOT add/remove leaves — youtube stays absent
        (3.5) and tidal/spotify stay present (3.7).
        """
        assert GLOBAL_FALLBACK_LEAVES == {
            "tidal": "tidal-refresh",
            "spotify": "spotify",
        }

    @settings(max_examples=200)
    @given(gid=_GID)
    def test_tidal_falls_back_to_global_refresh_leaf(self, gid: str):
        """For any guild WITHOUT a per-guild secret, tidal → global leaf (3.7).

        Only the global ``tidal-refresh`` secret exists (no guild secret), so
        the resolver must fall through to it for every guild.
        """
        secrets = FakeSecrets(
            {f"hellodj/{STAGE}/tidal-refresh": {"refresh_token": "GLOBAL-T"}}
        )
        r = _resolver(secrets)

        tokens = r.resolve(gid, "tidal")

        assert tokens == {"refresh_token": "GLOBAL-T"}
        # Tried the guild-scoped secret first, then the global leaf.
        assert secrets.calls == [
            guild_source_secret_name(STAGE, gid, "tidal"),
            f"hellodj/{STAGE}/tidal-refresh",
        ]

    @settings(max_examples=200)
    @given(gid=_GID)
    def test_spotify_falls_back_to_global_spotify_leaf(self, gid: str):
        """For any guild WITHOUT a per-guild secret, spotify → global leaf (3.7)."""
        secrets = FakeSecrets(
            {f"hellodj/{STAGE}/spotify": {"refresh_token": "GLOBAL-S"}}
        )
        r = _resolver(secrets)

        tokens = r.resolve(gid, "spotify")

        assert tokens == {"refresh_token": "GLOBAL-S"}
        assert secrets.calls == [
            guild_source_secret_name(STAGE, gid, "spotify"),
            f"hellodj/{STAGE}/spotify",
        ]


# ── 3.5 — global YouTube path preserved (resolve → None w/o guild secret) ───


class TestGlobalYouTubePathPreserved:
    @settings(max_examples=200)
    @given(gid=_GID)
    def test_youtube_resolve_is_none_without_guild_secret(self, gid: str):
        """For any guild without a YouTube secret, resolve → None (3.5).

        A ``None`` resolution is exactly what makes the bot fall through to the
        existing GLOBAL ``push_youtube_oauth`` single ``POST /youtube`` path.
        Even when a global tidal/spotify leaf exists, YouTube has no leaf, so it
        must resolve to ``None`` for every guild.
        """
        secrets = FakeSecrets(
            {
                f"hellodj/{STAGE}/tidal-refresh": {"t": 1},
                f"hellodj/{STAGE}/spotify": {"t": 1},
            }
        )
        r = _resolver(secrets)

        assert r.resolve(gid, "youtube") is None
        # Only the guild-scoped youtube secret is attempted; no global leaf.
        assert secrets.calls == [guild_source_secret_name(STAGE, gid, "youtube")]

    @settings(max_examples=200)
    @given(gid=_GID)
    def test_youtube_music_resolve_is_none_without_guild_secret(self, gid: str):
        """Same as above for youtube_music (3.5)."""
        secrets = FakeSecrets({})
        r = _resolver(secrets)

        assert r.resolve(gid, "youtube_music") is None
        assert secrets.calls == [
            guild_source_secret_name(STAGE, gid, "youtube_music")
        ]


# ── 3.6 — DVD-visualizer avatar read preserved (static source assertion) ────


class TestDvdVisualizerAvatarReadPreserved:
    def test_video_cog_still_reads_bot_user_avatar(self):
        """``bot/cogs/video.py`` still reads ``self.bot.user.avatar`` (3.6).

        The global avatar read that feeds the DVD-visualizer must remain
        untouched by the per-guild identity feature. Asserted statically
        against the source so the baseline holds without booting a Discord bot.
        """
        # test file lives at bot/playback/; video cog at bot/cogs/video.py
        video_py = Path(__file__).resolve().parents[1] / "cogs" / "video.py"
        assert video_py.is_file(), f"expected {video_py} to exist"
        source = video_py.read_text(encoding="utf-8")
        assert "self.bot.user.avatar" in source, (
            "regression: bot/cogs/video.py no longer reads "
            "self.bot.user.avatar (3.6)"
        )
