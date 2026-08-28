"""Task 6.2 — resolver + just-in-time YouTube injector unit + property tests.

Covers the per-guild YouTube credential swap wired in Task 6.1
(:class:`YouTubeCredentialInjector` + :func:`youtube_oauth_payload` in
``guild_credentials.py``), using a FAKE Lavalink ``/youtube`` endpoint and the
``FakeSecrets`` style from ``test_guild_credentials.py``. No live AWS / Lavalink.

What is asserted (Requirements 2.5, preservation 3.5):

* **Resolver returns the three fields (2.5):** given a fake per-guild youtube
  secret, ``resolve(gid, "youtube")`` returns ``oauth_refresh_token`` +
  ``pot_token`` + ``pot_visitor_data``.
* **Injector POSTs exactly that guild's creds (2.5):** the just-in-time injector
  POSTs exactly that guild's ``{oauth_refresh_token, pot_token,
  pot_visitor_data}`` to the FAKE Lavalink ``/youtube`` (mapped onto the plugin's
  ``refreshToken`` / ``poToken`` / ``visitorData`` payload fields).
* **No secret → no swap → global path preserved (3.5):** a guild with no
  per-guild youtube secret triggers NO swap (``inject_for_guild`` returns
  ``False`` and the fake Lavalink receives nothing), so the untouched global
  ``push_youtube_oauth`` single ``POST /youtube`` path remains in effect.
* **Property test (hypothesis):** random stored per-guild youtube secrets
  round-trip through resolver + injector with NO cross-guild leakage — the creds
  landing on the node for guild G are always exactly G's stored creds.

Bare imports rely on ``bot/playback`` being on ``sys.path`` (run pytest from
there).

Validates: Requirements 2.5
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from guild_credentials import (
    GuildCredentialResolver,
    YouTubeCredentialInjector,
    guild_source_secret_name,
    youtube_oauth_payload,
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


class FakeLavalink:
    """Fake Lavalink ``/youtube`` endpoint.

    Stands in for the real aiohttp ``POST {LAVALINK_URI}/youtube`` seam
    (``YouTubePush``). Records every payload pushed so tests can assert the exact
    ``{refreshToken, poToken, visitorData}`` a guild's swap wrote to the node, and
    models last-writer-wins by keeping the most recent payload as ``current``.
    """

    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.pushes: list[dict[str, object]] = []
        self.current: dict[str, object] | None = None

    async def push(self, payload: dict[str, object]) -> bool:
        # The plugin replaces ALL fields on each call — model that here.
        self.pushes.append(dict(payload))
        if self.ok:
            self.current = dict(payload)
        return self.ok


def _resolver(secrets: FakeSecrets) -> GuildCredentialResolver:
    return GuildCredentialResolver(secrets, stage=STAGE)


def _guild_youtube_secret(refresh: str, pot: str, visitor: str) -> dict[str, str]:
    """The exact stored shape the web-ui writes for a per-guild youtube secret."""
    return {
        "provider": "youtube",
        "oauth_refresh_token": refresh,
        "pot_token": pot,
        "pot_visitor_data": visitor,
    }


# ── 2.5 — resolver returns the three fields for a per-guild youtube secret ──


class TestResolverReturnsThreeFields:
    def test_resolve_youtube_returns_all_three_fields(self):
        secret = _guild_youtube_secret("1//0g-A", "MnQ-A", "Cgs-A")
        secrets = FakeSecrets(
            {guild_source_secret_name(STAGE, "111", "youtube"): secret}
        )
        r = _resolver(secrets)

        tokens = r.resolve("111", "youtube")

        assert tokens is not None
        assert tokens["oauth_refresh_token"] == "1//0g-A"
        assert tokens["pot_token"] == "MnQ-A"
        assert tokens["pot_visitor_data"] == "Cgs-A"
        # requested exactly the guild-scoped youtube secret name (no global leaf)
        assert secrets.calls == [guild_source_secret_name(STAGE, "111", "youtube")]

    def test_injector_resolve_youtube_passes_through_full_dict(self):
        secret = _guild_youtube_secret("1//0g-A", "MnQ-A", "Cgs-A")
        secrets = FakeSecrets(
            {guild_source_secret_name(STAGE, "111", "youtube"): secret}
        )
        injector = YouTubeCredentialInjector(_resolver(secrets), FakeLavalink().push)

        tokens = injector.resolve_youtube("111", "youtube")

        assert tokens is not None
        assert tokens["oauth_refresh_token"] == "1//0g-A"
        assert tokens["pot_token"] == "MnQ-A"
        assert tokens["pot_visitor_data"] == "Cgs-A"

    def test_resolve_youtube_none_without_refresh_token(self):
        # A secret lacking a refresh token is not a usable per-guild swap.
        secrets = FakeSecrets(
            {
                guild_source_secret_name(STAGE, "111", "youtube"): {
                    "provider": "youtube",
                    "pot_token": "MnQ",
                    "pot_visitor_data": "Cgs",
                }
            }
        )
        injector = YouTubeCredentialInjector(_resolver(secrets), FakeLavalink().push)
        assert injector.resolve_youtube("111", "youtube") is None

    def test_resolve_youtube_ignores_non_youtube_provider(self):
        secrets = FakeSecrets(
            {guild_source_secret_name(STAGE, "111", "tidal"): {"refresh_token": "T"}}
        )
        injector = YouTubeCredentialInjector(_resolver(secrets), FakeLavalink().push)
        # tidal is not per-guild-swappable via the YouTube injector
        assert injector.resolve_youtube("111", "tidal") is None


# ── 2.5 — injector POSTs exactly that guild's creds to a fake Lavalink ──────


class TestInjectorPostsGuildCreds:
    @pytest.mark.asyncio
    async def test_inject_posts_exact_guild_creds(self):
        secret = _guild_youtube_secret("1//0g-A", "MnQ-A", "Cgs-A")
        secrets = FakeSecrets(
            {guild_source_secret_name(STAGE, "111", "youtube"): secret}
        )
        lava = FakeLavalink()
        injector = YouTubeCredentialInjector(_resolver(secrets), lava.push)

        swapped = await injector.inject_for_guild("111", "youtube")

        assert swapped is True
        assert len(lava.pushes) == 1
        pushed = lava.pushes[0]
        # The three stored fields map onto the plugin payload fields, sent TOGETHER.
        assert pushed["refreshToken"] == "1//0g-A"
        assert pushed["poToken"] == "MnQ-A"
        assert pushed["visitorData"] == "Cgs-A"
        # matches the shared payload builder exactly
        assert pushed == youtube_oauth_payload(secret, skip_initialization=False)

    @pytest.mark.asyncio
    async def test_inject_youtube_music_posts_its_own_secret(self):
        secret = {
            "provider": "youtube_music",
            "oauth_refresh_token": "1//ytm",
            "pot_token": "MnQ-ytm",
            "pot_visitor_data": "Cgs-ytm",
        }
        secrets = FakeSecrets(
            {guild_source_secret_name(STAGE, "111", "youtube_music"): secret}
        )
        lava = FakeLavalink()
        injector = YouTubeCredentialInjector(_resolver(secrets), lava.push)

        swapped = await injector.inject_for_guild("111", "youtube_music")

        assert swapped is True
        assert lava.pushes[0]["refreshToken"] == "1//ytm"
        assert lava.pushes[0]["poToken"] == "MnQ-ytm"
        assert lava.pushes[0]["visitorData"] == "Cgs-ytm"

    @pytest.mark.asyncio
    async def test_inject_reports_failure_when_push_fails(self):
        secret = _guild_youtube_secret("1//0g-A", "MnQ-A", "Cgs-A")
        secrets = FakeSecrets(
            {guild_source_secret_name(STAGE, "111", "youtube"): secret}
        )
        lava = FakeLavalink(ok=False)
        injector = YouTubeCredentialInjector(_resolver(secrets), lava.push)

        swapped = await injector.inject_for_guild("111", "youtube")

        # push was attempted but the node rejected it
        assert swapped is False
        assert len(lava.pushes) == 1


# ── 3.5 — no per-guild secret → NO swap → global push path preserved ────────


class TestNoSecretNoSwapPreservesGlobalPath:
    @pytest.mark.asyncio
    async def test_no_guild_secret_triggers_no_swap(self):
        # No per-guild youtube secret anywhere; only a global tidal/spotify leaf
        # (which is irrelevant to YouTube — youtube has no global leaf).
        secrets = FakeSecrets(
            {
                f"hellodj/{STAGE}/tidal-refresh": {"t": 1},
                f"hellodj/{STAGE}/spotify": {"t": 1},
            }
        )
        lava = FakeLavalink()
        injector = YouTubeCredentialInjector(_resolver(secrets), lava.push)

        swapped = await injector.inject_for_guild("111", "youtube")

        # No swap performed → caller falls through to the untouched global push.
        assert swapped is False
        assert lava.pushes == []
        assert lava.current is None
        # only the guild-scoped youtube secret was attempted (no global YT leaf)
        assert secrets.calls == [guild_source_secret_name(STAGE, "111", "youtube")]

    @pytest.mark.asyncio
    async def test_global_creds_on_node_remain_after_no_swap(self):
        # Simulate the global push having already loaded creds on the shared node.
        secrets = FakeSecrets({})
        lava = FakeLavalink()
        global_payload = {
            "skipInitialization": False,
            "refreshToken": "GLOBAL-REFRESH",
            "poToken": "GLOBAL-POT",
            "visitorData": "GLOBAL-VISITOR",
        }
        await lava.push(global_payload)  # global path pushed first
        injector = YouTubeCredentialInjector(_resolver(secrets), lava.push)

        swapped = await injector.inject_for_guild("111", "youtube")

        # A guild without its own secret must NOT clobber the global creds.
        assert swapped is False
        assert lava.current == global_payload
        assert len(lava.pushes) == 1  # only the global push happened


# ── Property test (hypothesis) — round-trip w/ no cross-guild leakage ───────

# Numeric guild ids as strings; distinct token material per guild.
_GID = st.integers(min_value=1, max_value=10**18).map(str)
_TOKEN = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=40,
)


@st.composite
def _guild_secret_map(draw):
    """Build a map of {gid: {refresh, pot, visitor}} for 1..6 distinct guilds."""
    gids = draw(st.lists(_GID, min_size=1, max_size=6, unique=True))
    out: dict[str, dict[str, str]] = {}
    for gid in gids:
        out[gid] = _guild_youtube_secret(
            draw(_TOKEN), draw(_TOKEN), draw(_TOKEN)
        )
    return out


class TestRoundTripNoCrossGuildLeakage:
    @settings(max_examples=200, deadline=None)
    @given(secret_map=_guild_secret_map())
    def test_each_guild_injects_exactly_its_own_creds(self, secret_map):
        """Random per-guild youtube secrets round-trip with no cross-guild leak.

        For every guild, resolving + injecting pushes EXACTLY that guild's stored
        ``{oauth_refresh_token, pot_token, pot_visitor_data}`` (mapped onto the
        plugin payload) to the node — never another guild's material.
        """
        import asyncio

        store = {
            guild_source_secret_name(STAGE, gid, "youtube"): secret
            for gid, secret in secret_map.items()
        }
        secrets = FakeSecrets(store)
        lava = FakeLavalink()
        injector = YouTubeCredentialInjector(_resolver(secrets), lava.push)

        async def run() -> None:
            for gid, secret in secret_map.items():
                lava.pushes.clear()
                # Serialize the swap on the per-node lock, exactly like the
                # play path does (held across push + resolve).
                async with injector.swap_lock():
                    swapped = await injector.inject_for_guild(gid, "youtube")

                assert swapped is True
                assert len(lava.pushes) == 1
                pushed = lava.pushes[0]
                # Exactly this guild's creds — the defining no-leakage assertion.
                assert pushed["refreshToken"] == secret["oauth_refresh_token"]
                assert pushed["poToken"] == secret["pot_token"]
                assert pushed["visitorData"] == secret["pot_visitor_data"]
                # And never any other guild's material.
                for other_gid, other in secret_map.items():
                    if other_gid == gid:
                        continue
                    if other["oauth_refresh_token"] != secret["oauth_refresh_token"]:
                        assert pushed["refreshToken"] != other["oauth_refresh_token"]

        asyncio.run(run())

    @settings(max_examples=100, deadline=None)
    @given(secret_map=_guild_secret_map(), absent=_GID)
    def test_absent_guild_never_swaps_even_when_others_present(
        self, secret_map, absent
    ):
        """A guild not in the store never swaps, regardless of other guilds (3.5)."""
        import asyncio

        # Ensure `absent` truly has no per-guild secret.
        if absent in secret_map:
            return
        store = {
            guild_source_secret_name(STAGE, gid, "youtube"): secret
            for gid, secret in secret_map.items()
        }
        secrets = FakeSecrets(store)
        lava = FakeLavalink()
        injector = YouTubeCredentialInjector(_resolver(secrets), lava.push)

        async def run() -> None:
            swapped = await injector.inject_for_guild(absent, "youtube")
            assert swapped is False
            assert lava.pushes == []

        asyncio.run(run())
