"""Tests for TidalResolver OAuth token management (_ensure_token / _refresh_token)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Set up environment so credentials.py can initialize (uses tmp for DB)
os.environ.setdefault("HELLODJ_DB_KEY", "test-key-for-unit-tests-only")
os.environ.setdefault("DATA_DIR", "/tmp/hellodj_test_data")

from video.tidal_resolver import TidalResolver, TidalResolverError, _FALLBACK_CLIENT_ID


@pytest.fixture
def resolver(tmp_path):
    """Create a TidalResolver with a temp download dir."""
    return TidalResolver(download_dir=tmp_path)


class TestEnsureToken:
    """Tests for _ensure_token() — check expiry with 5-minute buffer, refresh if needed."""

    @pytest.mark.asyncio
    async def test_no_refresh_token_raises(self, resolver):
        """When no refresh token is stored, raise a non-recoverable error."""
        with patch("video.tidal_resolver.creds") as mock_creds:
            mock_creds.get.return_value = None
            with pytest.raises(TidalResolverError, match="not connected"):
                await resolver._ensure_token()

    @pytest.mark.asyncio
    async def test_valid_token_returned_without_refresh(self, resolver):
        """When token is valid (more than 5 min to expiry), return it directly."""
        future_expiry = str(time.time() + 600)  # 10 minutes from now

        with patch("video.tidal_resolver.creds") as mock_creds:
            mock_creds.get.side_effect = lambda key, *a, **kw: {
                "tidal.refresh_token": "rt_valid",
                "tidal.access_token": "at_valid",
                "tidal.expiry": future_expiry,
            }.get(key)

            result = await resolver._ensure_token()
            assert result == "at_valid"

    @pytest.mark.asyncio
    async def test_expired_token_triggers_refresh(self, resolver):
        """When token expiry is within 5-minute buffer, refresh is called."""
        # Expiry 2 minutes from now (within 5-min buffer)
        near_expiry = str(time.time() + 120)

        with patch("video.tidal_resolver.creds") as mock_creds:
            mock_creds.get.side_effect = lambda key, *a, **kw: {
                "tidal.refresh_token": "rt_valid",
                "tidal.access_token": "at_old",
                "tidal.expiry": near_expiry,
            }.get(key)

            with patch.object(resolver, "_refresh_token", new_callable=AsyncMock) as mock_refresh:
                mock_refresh.return_value = "at_new"
                result = await resolver._ensure_token()
                assert result == "at_new"
                mock_refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_expiry_triggers_refresh(self, resolver):
        """When expiry value is missing/None, refresh is triggered."""
        with patch("video.tidal_resolver.creds") as mock_creds:
            mock_creds.get.side_effect = lambda key, *a, **kw: {
                "tidal.refresh_token": "rt_valid",
                "tidal.access_token": "at_valid",
                "tidal.expiry": None,
            }.get(key)

            with patch.object(resolver, "_refresh_token", new_callable=AsyncMock) as mock_refresh:
                mock_refresh.return_value = "at_refreshed"
                result = await resolver._ensure_token()
                assert result == "at_refreshed"

    @pytest.mark.asyncio
    async def test_invalid_expiry_triggers_refresh(self, resolver):
        """When expiry is non-numeric, refresh is triggered."""
        with patch("video.tidal_resolver.creds") as mock_creds:
            mock_creds.get.side_effect = lambda key, *a, **kw: {
                "tidal.refresh_token": "rt_valid",
                "tidal.access_token": "at_valid",
                "tidal.expiry": "not_a_number",
            }.get(key)

            with patch.object(resolver, "_refresh_token", new_callable=AsyncMock) as mock_refresh:
                mock_refresh.return_value = "at_refreshed"
                result = await resolver._ensure_token()
                assert result == "at_refreshed"

    @pytest.mark.asyncio
    async def test_exactly_at_buffer_boundary_triggers_refresh(self, resolver):
        """When T == E - 300 (exactly at boundary), refresh is triggered."""
        # Expiry exactly 300 seconds from now: time.time() == expiry - 300
        boundary_expiry = str(time.time() + 300)

        with patch("video.tidal_resolver.creds") as mock_creds:
            mock_creds.get.side_effect = lambda key, *a, **kw: {
                "tidal.refresh_token": "rt_valid",
                "tidal.access_token": "at_valid",
                "tidal.expiry": boundary_expiry,
            }.get(key)

            with patch.object(resolver, "_refresh_token", new_callable=AsyncMock) as mock_refresh:
                mock_refresh.return_value = "at_refreshed"
                result = await resolver._ensure_token()
                # At exactly the boundary (T >= E-300), we should refresh
                # time.time() < (expiry - 300) is False when T == E - 300
                # so refresh should be triggered
                assert result == "at_refreshed"


class TestRefreshToken:
    """Tests for _refresh_token() — POST to auth endpoint with correct params."""

    @pytest.mark.asyncio
    async def test_successful_refresh_updates_store(self, resolver):
        """On successful refresh, access_token + expiry are updated in cred store."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "access_token": "new_at",
            "expires_in": 3600,
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.tidal_resolver.creds") as mock_creds:
            mock_creds.get.side_effect = lambda key, *a, **kw: {
                "tidal.refresh_token": "rt_stored",
                "tidal.issuing_client_id": "custom_client",
            }.get(key)

            with patch("video.tidal_resolver.aiohttp.ClientSession", return_value=mock_session):
                result = await resolver._refresh_token()

            assert result == "new_at"
            # Verify creds.set was called with correct values
            set_calls = {call[0][0]: call[0][1] for call in mock_creds.set.call_args_list}
            assert set_calls["tidal.access_token"] == "new_at"
            assert "tidal.expiry" in set_calls
            # refresh_token should NOT be updated since response didn't include one
            assert "tidal.refresh_token" not in set_calls

    @pytest.mark.asyncio
    async def test_refresh_updates_refresh_token_when_provided(self, resolver):
        """When response includes a new refresh_token, it's stored."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "access_token": "new_at",
            "expires_in": 3600,
            "refresh_token": "new_rt",
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.tidal_resolver.creds") as mock_creds:
            mock_creds.get.side_effect = lambda key, *a, **kw: {
                "tidal.refresh_token": "rt_stored",
                "tidal.issuing_client_id": None,
            }.get(key)

            with patch("video.tidal_resolver.aiohttp.ClientSession", return_value=mock_session):
                result = await resolver._refresh_token()

            assert result == "new_at"
            set_calls = {call[0][0]: call[0][1] for call in mock_creds.set.call_args_list}
            assert set_calls["tidal.refresh_token"] == "new_rt"

    @pytest.mark.asyncio
    async def test_uses_issuing_client_id_when_present(self, resolver):
        """Uses tidal.issuing_client_id for the refresh request when available."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "access_token": "new_at",
            "expires_in": 3600,
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.tidal_resolver.creds") as mock_creds:
            mock_creds.get.side_effect = lambda key, *a, **kw: {
                "tidal.refresh_token": "rt_stored",
                "tidal.issuing_client_id": "custom_id_123",
            }.get(key)

            with patch("video.tidal_resolver.aiohttp.ClientSession", return_value=mock_session):
                await resolver._refresh_token()

            # Check the POST data contains the custom client ID
            post_call = mock_session.post.call_args
            post_data = post_call[1]["data"] if "data" in post_call[1] else post_call[0][1] if len(post_call[0]) > 1 else None
            # aiohttp session.post is called with positional url and keyword data
            assert post_data["client_id"] == "custom_id_123"

    @pytest.mark.asyncio
    async def test_falls_back_to_default_client_id(self, resolver):
        """Falls back to _FALLBACK_CLIENT_ID when no issuing_client_id stored."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "access_token": "new_at",
            "expires_in": 3600,
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.tidal_resolver.creds") as mock_creds:
            mock_creds.get.side_effect = lambda key, *a, **kw: {
                "tidal.refresh_token": "rt_stored",
                "tidal.issuing_client_id": None,  # Not set
            }.get(key)

            with patch("video.tidal_resolver.aiohttp.ClientSession", return_value=mock_session):
                await resolver._refresh_token()

            post_call = mock_session.post.call_args
            post_data = post_call[1]["data"] if "data" in post_call[1] else None
            assert post_data["client_id"] == _FALLBACK_CLIENT_ID

    @pytest.mark.asyncio
    async def test_http_400_raises_non_recoverable(self, resolver):
        """HTTP 400 from OAuth endpoint → non-recoverable error + WARNING log."""
        mock_response = AsyncMock()
        mock_response.status = 400
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.tidal_resolver.creds") as mock_creds:
            mock_creds.get.side_effect = lambda key, *a, **kw: {
                "tidal.refresh_token": "rt_stored",
                "tidal.issuing_client_id": None,
            }.get(key)

            with patch("video.tidal_resolver.aiohttp.ClientSession", return_value=mock_session):
                with pytest.raises(TidalResolverError, match="re-login required") as exc_info:
                    await resolver._refresh_token()
                assert exc_info.value.recoverable is False

    @pytest.mark.asyncio
    async def test_http_401_raises_non_recoverable(self, resolver):
        """HTTP 401 from OAuth endpoint → non-recoverable error."""
        mock_response = AsyncMock()
        mock_response.status = 401
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.tidal_resolver.creds") as mock_creds:
            mock_creds.get.side_effect = lambda key, *a, **kw: {
                "tidal.refresh_token": "rt_stored",
                "tidal.issuing_client_id": None,
            }.get(key)

            with patch("video.tidal_resolver.aiohttp.ClientSession", return_value=mock_session):
                with pytest.raises(TidalResolverError, match="re-login required") as exc_info:
                    await resolver._refresh_token()
                assert exc_info.value.recoverable is False

    @pytest.mark.asyncio
    async def test_network_error_raises_recoverable(self, resolver):
        """Network error → recoverable TidalResolverError."""
        import aiohttp

        mock_session = AsyncMock()
        mock_session.post = MagicMock(side_effect=aiohttp.ClientError("connection reset"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.tidal_resolver.creds") as mock_creds:
            mock_creds.get.side_effect = lambda key, *a, **kw: {
                "tidal.refresh_token": "rt_stored",
                "tidal.issuing_client_id": None,
            }.get(key)

            with patch("video.tidal_resolver.aiohttp.ClientSession", return_value=mock_session):
                with pytest.raises(TidalResolverError, match="try again later") as exc_info:
                    await resolver._refresh_token()
                assert exc_info.value.recoverable is True

    @pytest.mark.asyncio
    async def test_no_refresh_token_stored_raises(self, resolver):
        """When no refresh_token in store, raise non-recoverable error."""
        with patch("video.tidal_resolver.creds") as mock_creds:
            mock_creds.get.return_value = None

            with pytest.raises(TidalResolverError, match="not connected"):
                await resolver._refresh_token()

    @pytest.mark.asyncio
    async def test_empty_access_token_in_response_raises(self, resolver):
        """When response has empty access_token, raise recoverable error."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "access_token": "",
            "expires_in": 3600,
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.tidal_resolver.creds") as mock_creds:
            mock_creds.get.side_effect = lambda key, *a, **kw: {
                "tidal.refresh_token": "rt_stored",
                "tidal.issuing_client_id": None,
            }.get(key)

            with patch("video.tidal_resolver.aiohttp.ClientSession", return_value=mock_session):
                with pytest.raises(TidalResolverError, match="empty access token") as exc_info:
                    await resolver._refresh_token()
                assert exc_info.value.recoverable is True

    @pytest.mark.asyncio
    async def test_refresh_posts_correct_grant_type(self, resolver):
        """The refresh request uses grant_type=refresh_token."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "access_token": "new_at",
            "expires_in": 3600,
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("video.tidal_resolver.creds") as mock_creds:
            mock_creds.get.side_effect = lambda key, *a, **kw: {
                "tidal.refresh_token": "rt_stored",
                "tidal.issuing_client_id": None,
            }.get(key)

            with patch("video.tidal_resolver.aiohttp.ClientSession", return_value=mock_session):
                await resolver._refresh_token()

            # Verify POST was to the correct URL with correct data
            post_call = mock_session.post.call_args
            url_arg = post_call[0][0]
            assert url_arg == "https://auth.tidal.com/v1/oauth2/token"
            post_data = post_call[1]["data"]
            assert post_data["grant_type"] == "refresh_token"
            assert post_data["refresh_token"] == "rt_stored"
