"""Discord Activity launcher for Video Activity sessions.

Handles launching and closing Discord Activities via the Discord REST API.
Includes rate-limit handling (retry once on HTTP 429) and descriptive error
messages for API failures.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"


class ActivityLaunchError(Exception):
    """Raised when the Discord API rejects an Activity launch or close request."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"Discord API error {status}: {message}")


class ActivityLauncher:
    """Launch and manage Discord Activities via the Discord REST API.

    Uses an aiohttp session for HTTP requests and the bot token for
    authorization. Handles rate limiting by respecting Retry-After and
    retrying once.
    """

    def __init__(self, session: aiohttp.ClientSession, token: str) -> None:
        self._session = session
        self._token = token

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bot {self._token}",
            "Content-Type": "application/json",
        }

    async def launch(self, channel_id: int, application_id: int) -> dict:
        """Launch a Discord Activity in a voice channel.

        Creates an invite with target_type=2 (embedded application) pointing
        to the specified application, which opens the Activity in the voice
        channel UI for all participants.

        Args:
            channel_id: The voice channel to launch the Activity in.
            application_id: The Discord application ID registered as an Activity.

        Returns:
            The API response dict containing the created invite data.

        Raises:
            ActivityLaunchError: If the API rejects the request after retries.
        """
        url = f"{DISCORD_API_BASE}/channels/{channel_id}/invites"
        payload = {
            "max_age": 0,
            "max_uses": 0,
            "target_type": 2,  # EMBEDDED_APPLICATION
            "target_application_id": application_id,
        }

        logger.info(
            "Launching Activity app=%d in channel=%d", application_id, channel_id
        )

        return await self._request_with_retry("POST", url, json=payload)

    async def close(self, channel_id: int) -> None:
        """Close an Activity session in a voice channel.

        Discord Activities close automatically when all participants leave.
        This method is a best-effort cleanup that deletes the voice channel's
        active embedded application session. If the API returns an error,
        it is logged but not raised — cleanup is best-effort.

        Args:
            channel_id: The voice channel whose Activity should be closed.
        """
        # Discord doesn't expose a direct "close activity" endpoint.
        # Activities terminate when no participants remain or the invite is
        # revoked. We attempt to delete active invites for the channel as
        # the closest approximation.
        url = f"{DISCORD_API_BASE}/channels/{channel_id}/invites"

        logger.info("Closing Activity in channel=%d", channel_id)

        try:
            invites = await self._request_with_retry("GET", url)
        except ActivityLaunchError as exc:
            logger.warning(
                "Failed to fetch invites for channel %d during close: %s",
                channel_id,
                exc.message,
            )
            return

        # Find and delete Activity invites (target_type == 2)
        for invite in invites:
            if invite.get("target_type") == 2:
                code = invite.get("code")
                if code:
                    delete_url = f"{DISCORD_API_BASE}/invites/{code}"
                    try:
                        await self._request_with_retry("DELETE", delete_url)
                        logger.info(
                            "Deleted Activity invite %s for channel %d",
                            code,
                            channel_id,
                        )
                    except ActivityLaunchError as exc:
                        logger.warning(
                            "Failed to delete invite %s: %s", code, exc.message
                        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        json: dict | None = None,
    ) -> dict | list:
        """Make an API request with a single retry on rate limit (429).

        Args:
            method: HTTP method (GET, POST, DELETE, etc.).
            url: Full API URL.
            json: Optional JSON body for the request.

        Returns:
            Parsed JSON response (dict or list).

        Raises:
            ActivityLaunchError: On 4xx/5xx errors (after retry for 429).
        """
        response = await self._do_request(method, url, json=json)

        if response.status == 429:
            retry_after = await self._get_retry_after(response)
            logger.warning(
                "Rate limited on %s %s — retrying after %.2fs",
                method,
                url,
                retry_after,
            )
            await asyncio.sleep(retry_after)
            response = await self._do_request(method, url, json=json)

            if response.status == 429:
                raise ActivityLaunchError(
                    429,
                    "Rate limited by Discord API after retry. "
                    "Please try again later.",
                )

        if response.status >= 400:
            message = await self._extract_error_message(response)
            logger.error(
                "Discord API error %d on %s %s: %s",
                response.status, method, url, message,
            )
            raise ActivityLaunchError(response.status, message)

        # 204 No Content (e.g. successful DELETE)
        if response.status == 204:
            return {}

        return await response.json()

    async def _do_request(
        self,
        method: str,
        url: str,
        *,
        json: dict | None = None,
    ) -> aiohttp.ClientResponse:
        """Perform a single HTTP request."""
        return await self._session.request(
            method, url, headers=self._headers, json=json
        )

    async def _get_retry_after(self, response: aiohttp.ClientResponse) -> float:
        """Extract the retry-after delay from a 429 response.

        Checks the JSON body first (more precise), then the header.
        Defaults to 1.0s if neither is available.
        """
        try:
            body = await response.json()
            if "retry_after" in body:
                return float(body["retry_after"])
        except Exception:
            pass

        header = response.headers.get("Retry-After")
        if header:
            try:
                return float(header)
            except ValueError:
                pass

        return 1.0

    async def _extract_error_message(
        self, response: aiohttp.ClientResponse
    ) -> str:
        """Extract a human-readable error message from an API error response."""
        try:
            body = await response.json()
            parts = []
            # Discord error responses have a "message" field
            if "message" in body:
                msg = body["message"]
                code = body.get("code")
                if code:
                    parts.append(f"{msg} (code: {code})")
                else:
                    parts.append(msg)
            # Include field-level errors if present
            if "errors" in body:
                parts.append(f"errors: {body['errors']}")
            if parts:
                return " | ".join(parts)
        except Exception:
            pass

        return f"HTTP {response.status} — no error details available"
