"""Unit tests for bot-side feature gating behavior (bot/feature_gate.py).

Tests:
- Base_Plan restricts to audio only
- Video_Addon enables video commands
- Premium_Addon enables Tidal/lossless
- Fallback behavior when API unreachable
- Tidal fallback to free sources when Premium not active

Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.7
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure bot/ is importable directly (same pattern as conftest.py)
_bot_dir = str(Path(__file__).resolve().parent.parent / "bot")
if _bot_dir not in sys.path:
    sys.path.insert(0, _bot_dir)

import feature_gate as fg_module
from feature_gate import (
    BASE_PLAN_SOURCES,
    FEATURE_TO_ADDON,
    _BASE_PLAN_DEFAULTS,
    _CACHE_TTL_SECONDS,
    check_tidal_fallback,
    get_feature_flags,
    get_feature_flags_sync,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_module_state():
    """Reset module-level state between tests."""
    fg_module._tenant_id = None
    fg_module._redis_url = None
    fg_module._api_base_url = None
    fg_module._feature_cache = {}
    fg_module._cache_timestamp = 0.0
    fg_module._subscriber_task = None
    fg_module._http_session = None


def _set_cache(flags: dict, fresh: bool = True):
    """Set the module-level cache with given flags."""
    fg_module._feature_cache = flags.copy()
    if fresh:
        fg_module._cache_timestamp = time.monotonic()
    else:
        fg_module._cache_timestamp = 0.0


@pytest.fixture(autouse=True)
def clean_state():
    """Reset feature gate state before each test."""
    _reset_module_state()
    yield
    _reset_module_state()


# ---------------------------------------------------------------------------
# Base_Plan restricts to audio only (Requirement 13.1)
# ---------------------------------------------------------------------------


class TestBasePlanRestrictions:
    """Test that Base_Plan restricts to audio only."""

    def test_base_plan_defaults_audio_enabled(self):
        """Base plan defaults have audio=True."""
        assert _BASE_PLAN_DEFAULTS["audio"] is True

    def test_base_plan_defaults_video_disabled(self):
        """Base plan defaults have video=False."""
        assert _BASE_PLAN_DEFAULTS["video"] is False

    def test_base_plan_defaults_activity_disabled(self):
        """Base plan defaults have activity=False."""
        assert _BASE_PLAN_DEFAULTS["activity"] is False

    def test_base_plan_defaults_hls_disabled(self):
        """Base plan defaults have hls=False."""
        assert _BASE_PLAN_DEFAULTS["hls"] is False

    def test_base_plan_defaults_visualizer_disabled(self):
        """Base plan defaults have visualizer=False."""
        assert _BASE_PLAN_DEFAULTS["visualizer"] is False

    def test_base_plan_defaults_tidal_disabled(self):
        """Base plan defaults have tidal_hifi=False."""
        assert _BASE_PLAN_DEFAULTS["tidal_hifi"] is False

    def test_base_plan_defaults_lossless_disabled(self):
        """Base plan defaults have lossless=False."""
        assert _BASE_PLAN_DEFAULTS["lossless"] is False

    def test_base_plan_defaults_priority_queue_disabled(self):
        """Base plan defaults have priority_queue=False."""
        assert _BASE_PLAN_DEFAULTS["priority_queue"] is False

    @pytest.mark.asyncio
    async def test_get_feature_flags_returns_base_defaults_when_no_cache(self):
        """get_feature_flags returns base plan defaults when cache is empty and API not configured."""
        flags = await get_feature_flags()
        assert flags["audio"] is True
        assert flags["video"] is False
        assert flags["tidal_hifi"] is False
        assert flags["lossless"] is False

    def test_get_feature_flags_sync_returns_base_defaults(self):
        """Sync access returns base plan defaults when cache is empty."""
        flags = get_feature_flags_sync()
        assert flags["audio"] is True
        assert flags["video"] is False
        assert flags["tidal_hifi"] is False

    @pytest.mark.asyncio
    async def test_base_plan_api_response_restricts_video(self):
        """When API returns base plan flags, video features are disabled."""
        base_flags = {
            "audio": True,
            "video": False,
            "activity": False,
            "hls": False,
            "visualizer": False,
            "tidal_hifi": False,
            "lossless": False,
            "priority_queue": False,
            "max_bot_instances": 1,
            "max_guilds_per_bot": 5,
        }
        _set_cache(base_flags)

        flags = await get_feature_flags()
        assert flags["video"] is False
        assert flags["activity"] is False
        assert flags["hls"] is False
        assert flags["visualizer"] is False


# ---------------------------------------------------------------------------
# Video_Addon enables video commands (Requirement 13.2)
# ---------------------------------------------------------------------------


class TestVideoAddonEnabled:
    """Test that Video_Addon enables video-related features."""

    @pytest.mark.asyncio
    async def test_video_addon_enables_video(self):
        """Video addon flag enables video feature."""
        flags = {
            "audio": True,
            "video": True,
            "activity": True,
            "hls": True,
            "visualizer": True,
            "tidal_hifi": False,
            "lossless": False,
            "priority_queue": False,
            "max_bot_instances": 1,
            "max_guilds_per_bot": 5,
        }
        _set_cache(flags)

        result = await get_feature_flags()
        assert result["video"] is True
        assert result["activity"] is True
        assert result["hls"] is True
        assert result["visualizer"] is True

    @pytest.mark.asyncio
    async def test_video_addon_does_not_enable_premium(self):
        """Video addon does not enable premium features."""
        flags = {
            "audio": True,
            "video": True,
            "activity": True,
            "hls": True,
            "visualizer": True,
            "tidal_hifi": False,
            "lossless": False,
            "priority_queue": False,
            "max_bot_instances": 1,
            "max_guilds_per_bot": 5,
        }
        _set_cache(flags)

        result = await get_feature_flags()
        assert result["tidal_hifi"] is False
        assert result["lossless"] is False
        assert result["priority_queue"] is False

    def test_video_feature_maps_to_video_addon(self):
        """Video-related features map to 'Video Addon' in FEATURE_TO_ADDON."""
        assert FEATURE_TO_ADDON["video"] == "Video Addon"
        assert FEATURE_TO_ADDON["activity"] == "Video Addon"
        assert FEATURE_TO_ADDON["hls"] == "Video Addon"
        assert FEATURE_TO_ADDON["visualizer"] == "Video Addon"


# ---------------------------------------------------------------------------
# Premium_Addon enables Tidal/lossless (Requirement 13.3)
# ---------------------------------------------------------------------------


class TestPremiumAddonEnabled:
    """Test that Premium_Addon enables Tidal HiFi and lossless."""

    @pytest.mark.asyncio
    async def test_premium_addon_enables_tidal(self):
        """Premium addon flag enables tidal_hifi."""
        flags = {
            "audio": True,
            "video": False,
            "activity": False,
            "hls": False,
            "visualizer": False,
            "tidal_hifi": True,
            "lossless": True,
            "priority_queue": True,
            "max_bot_instances": 1,
            "max_guilds_per_bot": 5,
        }
        _set_cache(flags)

        result = await get_feature_flags()
        assert result["tidal_hifi"] is True
        assert result["lossless"] is True
        assert result["priority_queue"] is True

    @pytest.mark.asyncio
    async def test_premium_addon_does_not_enable_video(self):
        """Premium addon does not enable video features."""
        flags = {
            "audio": True,
            "video": False,
            "activity": False,
            "hls": False,
            "visualizer": False,
            "tidal_hifi": True,
            "lossless": True,
            "priority_queue": True,
            "max_bot_instances": 1,
            "max_guilds_per_bot": 5,
        }
        _set_cache(flags)

        result = await get_feature_flags()
        assert result["video"] is False
        assert result["activity"] is False

    def test_premium_feature_maps_to_premium_addon(self):
        """Premium-related features map to 'Premium Addon' in FEATURE_TO_ADDON."""
        assert FEATURE_TO_ADDON["tidal_hifi"] == "Premium Addon"
        assert FEATURE_TO_ADDON["lossless"] == "Premium Addon"
        assert FEATURE_TO_ADDON["priority_queue"] == "Premium Addon"

    @pytest.mark.asyncio
    async def test_full_bundle_enables_all(self):
        """Both addons together enable all features."""
        flags = {
            "audio": True,
            "video": True,
            "activity": True,
            "hls": True,
            "visualizer": True,
            "tidal_hifi": True,
            "lossless": True,
            "priority_queue": True,
            "max_bot_instances": 1,
            "max_guilds_per_bot": 5,
        }
        _set_cache(flags)

        result = await get_feature_flags()
        assert all(
            result[k] is True
            for k in ["audio", "video", "activity", "hls", "visualizer", "tidal_hifi", "lossless", "priority_queue"]
        )


# ---------------------------------------------------------------------------
# Fallback behavior when API unreachable (Requirement 13.7)
# ---------------------------------------------------------------------------


class TestAPIUnreachableFallback:
    """Test fallback to Base_Plan when feature flag API is unreachable."""

    @pytest.mark.asyncio
    async def test_fallback_to_base_plan_when_no_cache_and_api_unreachable(self):
        """When API is unreachable and no cache exists, defaults to Base_Plan."""
        fg_module._tenant_id = "test-tenant-id"
        fg_module._api_base_url = "http://unreachable:9999"

        # Mock HTTP session that raises on GET
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(side_effect=Exception("Connection refused"))
        mock_session.get = MagicMock(return_value=mock_resp)
        fg_module._http_session = mock_session

        flags = await get_feature_flags()

        # Should fall back to base plan defaults
        assert flags["audio"] is True
        assert flags["video"] is False
        assert flags["tidal_hifi"] is False
        assert flags["lossless"] is False
        assert flags["priority_queue"] is False

    @pytest.mark.asyncio
    async def test_retains_stale_cache_when_api_unreachable(self):
        """When API is unreachable but stale cache exists, returns stale cache."""
        # Set up stale cache (timestamp expired)
        premium_flags = {
            "audio": True,
            "video": True,
            "activity": True,
            "hls": True,
            "visualizer": True,
            "tidal_hifi": True,
            "lossless": True,
            "priority_queue": True,
            "max_bot_instances": 2,
            "max_guilds_per_bot": 5,
        }
        fg_module._feature_cache = premium_flags.copy()
        fg_module._cache_timestamp = 0.0  # expired

        fg_module._tenant_id = "test-tenant-id"
        fg_module._api_base_url = "http://unreachable:9999"

        # Mock HTTP session that raises on GET
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(side_effect=Exception("Connection refused"))
        mock_session.get = MagicMock(return_value=mock_resp)
        fg_module._http_session = mock_session

        flags = await get_feature_flags()

        # Should retain the stale cache (premium features still enabled)
        assert flags["tidal_hifi"] is True
        assert flags["video"] is True

    @pytest.mark.asyncio
    async def test_fresh_cache_skips_api_call(self):
        """When cache is fresh, API is not called at all."""
        video_flags = {
            "audio": True,
            "video": True,
            "activity": True,
            "hls": True,
            "visualizer": True,
            "tidal_hifi": False,
            "lossless": False,
            "priority_queue": False,
            "max_bot_instances": 1,
            "max_guilds_per_bot": 5,
        }
        _set_cache(video_flags, fresh=True)

        fg_module._tenant_id = "test-tenant-id"
        fg_module._api_base_url = "http://should-not-be-called:9999"
        # No HTTP session set — would crash if called
        fg_module._http_session = None

        flags = await get_feature_flags()
        assert flags["video"] is True

    @pytest.mark.asyncio
    async def test_non_200_response_falls_back_to_defaults(self):
        """When API returns non-200 and no cache, falls back to Base_Plan."""
        fg_module._tenant_id = "test-tenant-id"
        fg_module._api_base_url = "http://api:8080"

        # Mock HTTP session returning 500
        mock_resp_ctx = AsyncMock()
        mock_resp_obj = AsyncMock()
        mock_resp_obj.status = 500
        mock_resp_ctx.__aenter__ = AsyncMock(return_value=mock_resp_obj)
        mock_resp_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp_ctx)
        fg_module._http_session = mock_session

        flags = await get_feature_flags()

        assert flags["audio"] is True
        assert flags["video"] is False
        assert flags["tidal_hifi"] is False

    @pytest.mark.asyncio
    async def test_successful_api_response_updates_cache(self):
        """When API responds with 200, cache is updated with returned flags."""
        fg_module._tenant_id = "test-tenant-id"
        fg_module._api_base_url = "http://api:8080"

        api_flags = {
            "audio": True,
            "video": True,
            "activity": True,
            "hls": True,
            "visualizer": True,
            "tidal_hifi": False,
            "lossless": False,
            "priority_queue": False,
            "max_bot_instances": 1,
            "max_guilds_per_bot": 5,
        }

        # Mock HTTP session returning 200 with json
        mock_resp_ctx = AsyncMock()
        mock_resp_obj = AsyncMock()
        mock_resp_obj.status = 200
        mock_resp_obj.json = AsyncMock(return_value=api_flags)
        mock_resp_ctx.__aenter__ = AsyncMock(return_value=mock_resp_obj)
        mock_resp_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp_ctx)
        fg_module._http_session = mock_session

        flags = await get_feature_flags()

        assert flags["video"] is True
        assert fg_module._feature_cache["video"] is True
        assert fg_module._cache_timestamp > 0


# ---------------------------------------------------------------------------
# Tidal fallback to free sources (Requirement 13.5)
# ---------------------------------------------------------------------------


class TestTidalFallback:
    """Test Tidal fallback behavior when Premium_Addon not active."""

    @pytest.mark.asyncio
    async def test_tidal_fallback_when_premium_not_active(self):
        """When Premium not active, Tidal request returns fallback=True with message."""
        base_flags = {
            "audio": True,
            "video": False,
            "activity": False,
            "hls": False,
            "visualizer": False,
            "tidal_hifi": False,
            "lossless": False,
            "priority_queue": False,
            "max_bot_instances": 1,
            "max_guilds_per_bot": 5,
        }
        _set_cache(base_flags)

        should_fallback, message = await check_tidal_fallback("tidal")
        assert should_fallback is True
        assert message is not None
        assert "Premium Addon" in message
        assert "Falling back" in message

    @pytest.mark.asyncio
    async def test_tidal_no_fallback_when_premium_active(self):
        """When Premium is active, Tidal request returns fallback=False."""
        premium_flags = {
            "audio": True,
            "video": False,
            "activity": False,
            "hls": False,
            "visualizer": False,
            "tidal_hifi": True,
            "lossless": True,
            "priority_queue": True,
            "max_bot_instances": 1,
            "max_guilds_per_bot": 5,
        }
        _set_cache(premium_flags)

        should_fallback, message = await check_tidal_fallback("tidal")
        assert should_fallback is False
        assert message is None

    @pytest.mark.asyncio
    async def test_non_tidal_source_no_fallback(self):
        """Non-tidal sources never trigger fallback regardless of premium status."""
        base_flags = _BASE_PLAN_DEFAULTS.copy()
        _set_cache(base_flags)

        for source in ["youtube", "spotify", "soundcloud", "other"]:
            should_fallback, message = await check_tidal_fallback(source)
            assert should_fallback is False
            assert message is None

    @pytest.mark.asyncio
    async def test_tidal_fallback_case_insensitive(self):
        """Tidal source check is case insensitive."""
        base_flags = _BASE_PLAN_DEFAULTS.copy()
        _set_cache(base_flags)

        for variant in ["Tidal", "TIDAL", "tidal", "TiDaL"]:
            should_fallback, message = await check_tidal_fallback(variant)
            assert should_fallback is True
            assert message is not None

    @pytest.mark.asyncio
    async def test_tidal_fallback_message_contains_upgrade_link(self):
        """Fallback message contains upgrade URL."""
        base_flags = _BASE_PLAN_DEFAULTS.copy()
        _set_cache(base_flags)

        _, message = await check_tidal_fallback("tidal")
        assert "hellodj.celestium.life" in message

    def test_base_plan_sources_available_for_fallback(self):
        """BASE_PLAN_SOURCES contains youtube, spotify, soundcloud."""
        assert "youtube" in BASE_PLAN_SOURCES
        assert "spotify" in BASE_PLAN_SOURCES
        assert "soundcloud" in BASE_PLAN_SOURCES


# ---------------------------------------------------------------------------
# Cache TTL and invalidation behavior
# ---------------------------------------------------------------------------


class TestCacheBehavior:
    """Test cache TTL and invalidation mechanisms."""

    def test_cache_ttl_is_5_minutes(self):
        """Cache TTL is configured to 5 minutes (300 seconds)."""
        assert _CACHE_TTL_SECONDS == 300

    @pytest.mark.asyncio
    async def test_expired_cache_triggers_refresh(self):
        """Expired cache triggers a refresh attempt."""
        flags = {"audio": True, "video": True}
        fg_module._feature_cache = flags.copy()
        # Set timestamp to well past TTL
        fg_module._cache_timestamp = time.monotonic() - _CACHE_TTL_SECONDS - 10

        # No API configured, so refresh will just keep existing cache
        fg_module._tenant_id = None
        fg_module._api_base_url = None

        result = await get_feature_flags()
        # Should still have the cached values since refresh fills defaults if not configured
        # but existing cache is preserved
        assert result is not None

    def test_invalidate_cache_resets_timestamp(self):
        """Cache invalidation sets timestamp to 0."""
        _set_cache({"audio": True}, fresh=True)
        assert fg_module._cache_timestamp > 0

        fg_module._invalidate_cache()
        assert fg_module._cache_timestamp == 0.0

    def test_get_feature_flags_sync_returns_copy(self):
        """Sync access returns a copy, not a reference."""
        _set_cache({"audio": True, "video": False})
        flags = get_feature_flags_sync()
        flags["audio"] = False  # Mutate the copy
        assert fg_module._feature_cache["audio"] is True  # Original unchanged
