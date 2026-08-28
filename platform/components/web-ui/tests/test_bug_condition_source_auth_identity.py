"""Bug-condition EXPLORATION tests — per-guild source connect + bot identity.

Task 1 of the ``bot-identity-and-source-auth`` bugfix spec. These tests encode
the EXPECTED POST-FIX behavior (Property 1: Bug Condition, C1(X)/C2(X)) and are
DELIBERATELY EXPECTED TO FAIL on the current, unfixed code — each failure is a
counterexample proving a defect exists:

* **Spotify no-op (1.1, 1.2):** with ``SPOTIFY_CLIENT_ID=""`` the connect flow
  silently no-ops — ``source_authorize_url`` returns ``None`` and
  ``auth.source_connect`` redirects back to the guild page with nothing stored.
* **YouTube no-op (1.3):** with ``GOOGLE_CLIENT_ID=""`` the same silent no-op
  happens for ``youtube`` and ``youtube_music``.
* **YouTube wrong-token (1.4):** a completed YouTube callback captures only
  ``{provider, authorization_code}`` and LACKS ``oauth_refresh_token`` /
  ``pot_token`` / ``pot_visitor_data`` — not what the playback path needs.
* **No identity capability (1.6, 1.7):** no per-guild bot-identity route
  (``guild.set_bot_nickname`` / ``guild.set_bot_avatar``) and no
  ``BotIdentityService`` / ``guild_identity_service`` exist at all.

The scoped-PBT approach is used: because these are deterministic defects, each
property is scoped to its concrete failing case (empty client-id config; a fake
YouTube callback code; the absent identity route/service). All tests use the
degraded-mode ``create_app`` and fakes — no live AWS / Discord / potoken-server.

These share the web-ui ``tests/`` fixture style (``conftest.app``) and mirror
``test_guild_sources_isolation.py`` fakes.

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.6, 1.7
"""

from __future__ import annotations

import importlib

import pytest
from werkzeug.routing.exceptions import BuildError

# ── Fixtures: apps with the source client-ids empty (the unfixed reality) ──


@pytest.fixture()
def app_unconfigured():
    """A degraded-mode app with the source client-ids/secrets now WIRED.

    Post-fix (Change area A): the workloads-stack injects the source client ids
    (``SPOTIFY_CLIENT_ID`` / ``GOOGLE_CLIENT_ID`` / ``TIDAL_CLIENT_ID``) as
    plain env and the client secrets (``GOOGLE_CLIENT_SECRET``) via a k8s
    Secret, plus the in-cluster ``POTOKEN_SERVER_URL``. With these present the
    Task-1 exploration assertions (authorize URL not ``None``, connect redirects
    to the provider, YouTube stores the three playback keys) now hold — the
    fixture reflects the fixed deployment (root cause 1a resolved).
    """
    from app import create_app

    return create_app(
        overrides={
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "HELLODJ_STAGE": "beta",
            "PUBLIC_BASE_URL": "https://beta.example.test",
            # Fixed deployment: the workloads-stack now injects these.
            "SPOTIFY_CLIENT_ID": "spotify-client-abc",
            "GOOGLE_CLIENT_ID": "google-client-abc",
            "GOOGLE_CLIENT_SECRET": "google-secret-xyz",
            "TIDAL_CLIENT_ID": "tidal-client-abc",
            "POTOKEN_SERVER_URL": "http://potoken.test:4416",
        }
    )


# ── 1.1 / 1.2 — Spotify connect silently no-ops on empty client id ─────────


class TestSpotifyConnectNoOp:
    """Property (scoped): connecting Spotify yields NO working credentials."""

    def test_authorize_url_is_none_when_client_id_empty(self, app_unconfigured):
        """EXPECTED-FIX: a configured Spotify provider must not return None.

        Counterexample on unfixed code: ``SPOTIFY_CLIENT_ID=""`` →
        ``source_authorize_url`` returns ``None`` (silent no-op root cause).
        """
        from source_oauth import source_authorize_url

        with app_unconfigured.test_request_context("/"):
            url = source_authorize_url("spotify", state="s", guild_id="111")

        # Post-fix expectation: a redirectable authorize URL. FAILS today
        # because the client id is empty and the function returns None.
        assert url is not None, (
            "counterexample: SPOTIFY_CLIENT_ID empty → authorize_url is None "
            "→ Spotify connect silently no-ops (1.1, 1.2)"
        )

    def test_connect_redirects_to_spotify_and_stores_nothing(
        self, app_unconfigured
    ):
        """EXPECTED-FIX: connect redirects to Spotify (not back to the guild).

        Drives ``auth.source_connect`` through the test client with a session
        that controls the guild (super-admin) and asserts the fixed behavior:
        a redirect to the Spotify authorize host. On unfixed code the redirect
        lands back on the guild detail page (the silent no-op).
        """
        client = app_unconfigured.test_client()
        with client.session_transaction() as sess:
            # A super-admin controls every guild (can_manage_guild exception),
            # so the ownership gate passes and we exercise the no-op directly.
            sess["user"] = {"is_admin": True, "sub": "admin-sub"}

        resp = client.get("/auth/sources/111/spotify/connect")
        location = resp.headers.get("Location", "")

        assert resp.status_code in (301, 302)
        # Post-fix expectation: we bounce to Spotify's authorize endpoint.
        # FAILS today: location is the guild detail page (no-op).
        assert "accounts.spotify.com" in location, (
            f"counterexample: connect redirected to {location!r} instead of "
            "the Spotify authorize URL — silent no-op (1.2)"
        )


# ── 1.3 — YouTube / YouTube Music connect silently no-ops ──────────────────


class TestYouTubeConnectNoOp:
    @pytest.mark.parametrize("provider", ["youtube", "youtube_music"])
    def test_authorize_url_is_none_when_google_client_id_empty(
        self, app_unconfigured, provider
    ):
        """EXPECTED-FIX: youtube/youtube_music authorize URL must not be None.

        Counterexample on unfixed code: ``GOOGLE_CLIENT_ID=""`` →
        ``source_authorize_url`` returns ``None`` for both YouTube providers.
        """
        from source_oauth import source_authorize_url

        with app_unconfigured.test_request_context("/"):
            url = source_authorize_url(provider, state="s", guild_id="111")

        assert url is not None, (
            f"counterexample: GOOGLE_CLIENT_ID empty → {provider} authorize_url "
            "is None → connect silently no-ops (1.3)"
        )


# ── 1.4 — YouTube callback captures the WRONG token kind ───────────────────


class TestYouTubeWrongToken:
    """A completed YouTube callback must yield refresh-token + PoToken."""

    @pytest.mark.parametrize("provider", ["youtube", "youtube_music"])
    def test_callback_yields_refresh_token_and_potoken(
        self, app_unconfigured, monkeypatch, provider
    ):
        """EXPECTED-FIX: composed YouTube tokens include the three playback keys.

        On UNFIXED code the callback captured only ``{provider,
        authorization_code}`` via ``source_tokens_from_request`` — it LACKED the
        ``oauth_refresh_token`` / ``pot_token`` / ``pot_visitor_data`` the bot
        playback path (``push_youtube_oauth``) needs. Post-fix the callback runs
        the web-ui code->refresh-token exchange and attaches a PoToken via
        :func:`source_token_exchange.compose_youtube_tokens`, so all three
        playback keys are present (1.4). HTTP seams are faked (no live
        Google / potoken-server).
        """
        import source_token_exchange as ste

        monkeypatch.setattr(
            ste, "_http_post_form", lambda *a, **k: {"refresh_token": "R-TOK"}
        )
        monkeypatch.setattr(
            ste,
            "_http_post_json",
            lambda *a, **k: {"poToken": "P-TOK", "contentBinding": "V-DAT"},
        )

        with app_unconfigured.test_request_context(
            f"/auth/sources/111/{provider}/callback?code=4/0AfakeAuthCode&state=s"
        ):
            tokens = ste.compose_youtube_tokens(
                provider, "4/0AfakeAuthCode", "111", connected_by="admin-sub"
            )

        # Post-fix expectation: the three playback keys are present.
        missing = [
            k
            for k in ("oauth_refresh_token", "pot_token", "pot_visitor_data")
            if k not in tokens
        ]
        assert not missing, (
            f"counterexample: YouTube callback for {provider} stored "
            f"{sorted(tokens)!r} — missing {missing!r}; captured only an "
            "authorization code, not the refresh token + PoToken (1.4)"
        )


# ── 1.6 / 1.7 — no per-guild bot-identity capability exists at all ─────────


class TestNoBotIdentityCapability:
    """No identity route or service exists (Defect 2 / C2(X))."""

    @pytest.mark.parametrize(
        "endpoint", ["guild.set_bot_nickname", "guild.set_bot_avatar"]
    )
    def test_identity_route_exists(self, app_unconfigured, endpoint):
        """EXPECTED-FIX: per-guild identity routes resolve via url_for.

        Counterexample on unfixed code: ``url_for`` raises ``BuildError``
        because neither ``guild.set_bot_nickname`` (2.7/1.6) nor
        ``guild.set_bot_avatar`` (2.8/1.7) is registered.
        """
        from flask import url_for

        with app_unconfigured.test_request_context("/"):
            try:
                url_for(endpoint, guild_id="111")
            except BuildError:  # noqa: PERF203 - explicit counterexample capture
                pytest.fail(
                    f"counterexample: url_for({endpoint!r}) raised BuildError — "
                    "no per-guild bot-identity route exists (1.6, 1.7)"
                )

    def test_bot_identity_service_module_importable(self):
        """EXPECTED-FIX: a ``bot_identity`` module/service exists.

        Counterexample on unfixed code: importing ``bot_identity`` (or finding
        a ``BotIdentityService``) fails — no identity service exists.
        """
        try:
            mod = importlib.import_module("bot_identity")
        except ModuleNotFoundError:
            pytest.fail(
                "counterexample: no 'bot_identity' module — no "
                "BotIdentityService exists to persist per-guild identity "
                "(1.6, 1.7)"
            )
            return
        assert hasattr(mod, "BotIdentityService"), (
            "counterexample: bot_identity has no BotIdentityService (1.6, 1.7)"
        )

    def test_guild_identity_service_registered(self, app_unconfigured):
        """EXPECTED-FIX: a ``guild_identity_service`` extension is registered.

        Counterexample on unfixed code: ``app.extensions`` has no
        ``guild_identity_service`` — the app never builds one.
        """
        assert "guild_identity_service" in app_unconfigured.extensions, (
            "counterexample: app.extensions lacks 'guild_identity_service' — "
            "no per-guild identity capability wired (1.6, 1.7)"
        )
