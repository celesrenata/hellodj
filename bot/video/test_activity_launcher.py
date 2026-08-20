"""Tests for ActivityLauncher — verifies Discord API interactions, rate limiting, and error handling."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from video.activity_launcher import ActivityLauncher, ActivityLaunchError


# ── Helpers ───────────────────────────────────────────────────────────


def _mock_response(
    status: int = 200,
    json_data: dict | list | None = None,
    headers: dict | None = None,
) -> MagicMock:
    """Create a mock aiohttp response."""
    resp = MagicMock()
    resp.status = status
    resp.headers = headers or {}
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    return resp


@pytest.fixture
def launcher():
    """Create an ActivityLauncher with a mocked session."""
    session = MagicMock()
    session.request = AsyncMock()
    return ActivityLauncher(session=session, token="test-bot-token")


# ── launch() tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_launch_success(launcher):
    """launch() returns the API response dict on success."""
    expected = {"code": "abc123", "target_type": 2}
    launcher._session.request.return_value = _mock_response(200, expected)

    result = await launcher.launch(channel_id=123456, application_id=789012)

    assert result == expected
    launcher._session.request.assert_called_once()
    call_kwargs = launcher._session.request.call_args
    assert call_kwargs[0][0] == "POST"
    assert "123456" in call_kwargs[0][1]


@pytest.mark.asyncio
async def test_launch_includes_correct_payload(launcher):
    """launch() sends target_type=2 and the application_id."""
    launcher._session.request.return_value = _mock_response(200, {"code": "x"})

    await launcher.launch(channel_id=111, application_id=222)

    call_kwargs = launcher._session.request.call_args
    payload = call_kwargs[1]["json"]
    assert payload["target_type"] == 2
    assert payload["target_application_id"] == 222
    assert payload["max_age"] == 0


@pytest.mark.asyncio
async def test_launch_uses_bot_authorization(launcher):
    """launch() sends the Bot token in Authorization header."""
    launcher._session.request.return_value = _mock_response(200, {})

    await launcher.launch(channel_id=111, application_id=222)

    call_kwargs = launcher._session.request.call_args
    headers = call_kwargs[1]["headers"]
    assert headers["Authorization"] == "Bot test-bot-token"


@pytest.mark.asyncio
async def test_launch_raises_on_4xx(launcher):
    """launch() raises ActivityLaunchError on 4xx responses."""
    launcher._session.request.return_value = _mock_response(
        403, {"message": "Missing Permissions", "code": 50013}
    )

    with pytest.raises(ActivityLaunchError) as exc_info:
        await launcher.launch(channel_id=111, application_id=222)

    assert exc_info.value.status == 403
    assert "Missing Permissions" in exc_info.value.message


@pytest.mark.asyncio
async def test_launch_raises_on_5xx(launcher):
    """launch() raises ActivityLaunchError on 5xx responses."""
    launcher._session.request.return_value = _mock_response(
        500, {"message": "Internal Server Error"}
    )

    with pytest.raises(ActivityLaunchError) as exc_info:
        await launcher.launch(channel_id=111, application_id=222)

    assert exc_info.value.status == 500


# ── Rate limiting tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_launch_retries_on_429(launcher):
    """launch() retries once after a 429 and succeeds."""
    rate_limited = _mock_response(
        429,
        {"retry_after": 0.01, "message": "rate limited"},
        headers={"Retry-After": "0.01"},
    )
    success = _mock_response(200, {"code": "abc"})
    launcher._session.request.side_effect = [rate_limited, success]

    result = await launcher.launch(channel_id=111, application_id=222)

    assert result == {"code": "abc"}
    assert launcher._session.request.call_count == 2


@pytest.mark.asyncio
async def test_launch_raises_after_double_429(launcher):
    """launch() raises after being rate-limited twice."""
    rate_limited = _mock_response(
        429,
        {"retry_after": 0.01},
        headers={"Retry-After": "0.01"},
    )
    launcher._session.request.side_effect = [rate_limited, rate_limited]

    with pytest.raises(ActivityLaunchError) as exc_info:
        await launcher.launch(channel_id=111, application_id=222)

    assert exc_info.value.status == 429
    assert "after retry" in exc_info.value.message


@pytest.mark.asyncio
async def test_retry_after_from_header(launcher):
    """Rate limit retry reads Retry-After header when JSON lacks it."""
    rate_limited = _mock_response(429, {}, headers={"Retry-After": "0.01"})
    rate_limited.json = AsyncMock(side_effect=Exception("no json"))
    success = _mock_response(200, {"code": "ok"})
    launcher._session.request.side_effect = [rate_limited, success]

    result = await launcher.launch(channel_id=111, application_id=222)
    assert result == {"code": "ok"}


# ── close() tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_deletes_activity_invites(launcher):
    """close() fetches invites and deletes those with target_type=2."""
    invites = [
        {"code": "abc", "target_type": 2},
        {"code": "def", "target_type": 1},  # Not an Activity invite
    ]
    get_resp = _mock_response(200, invites)
    delete_resp = _mock_response(204)
    launcher._session.request.side_effect = [get_resp, delete_resp]

    await launcher.close(channel_id=111)

    # Should have made 2 calls: GET invites, DELETE the activity invite
    assert launcher._session.request.call_count == 2
    delete_call = launcher._session.request.call_args_list[1]
    assert delete_call[0][0] == "DELETE"
    assert "abc" in delete_call[0][1]


@pytest.mark.asyncio
async def test_close_handles_get_failure_gracefully(launcher):
    """close() does not raise if fetching invites fails."""
    launcher._session.request.return_value = _mock_response(
        403, {"message": "Forbidden"}
    )

    # Should not raise
    await launcher.close(channel_id=111)


@pytest.mark.asyncio
async def test_close_handles_delete_failure_gracefully(launcher):
    """close() does not raise if deleting an invite fails."""
    invites = [{"code": "abc", "target_type": 2}]
    get_resp = _mock_response(200, invites)
    delete_resp = _mock_response(404, {"message": "Unknown Invite"})
    launcher._session.request.side_effect = [get_resp, delete_resp]

    # Should not raise
    await launcher.close(channel_id=111)


# ── Error message extraction ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_error_message_includes_code(launcher):
    """Error messages include the Discord error code when present."""
    launcher._session.request.return_value = _mock_response(
        400, {"message": "Invalid Form Body", "code": 50035}
    )

    with pytest.raises(ActivityLaunchError) as exc_info:
        await launcher.launch(channel_id=111, application_id=222)

    assert "50035" in exc_info.value.message
    assert "Invalid Form Body" in exc_info.value.message


@pytest.mark.asyncio
async def test_error_message_fallback_when_no_json(launcher):
    """Error messages fall back gracefully when response has no JSON."""
    resp = _mock_response(502)
    resp.json = AsyncMock(side_effect=Exception("not json"))
    launcher._session.request.return_value = resp

    with pytest.raises(ActivityLaunchError) as exc_info:
        await launcher.launch(channel_id=111, application_id=222)

    assert exc_info.value.status == 502
    assert "502" in exc_info.value.message
