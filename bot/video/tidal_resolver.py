"""HelloDJ — Tidal music video resolution: URL parsing, metadata fetch, and download.

Resolves Tidal music video URLs to downloadable video files via the Tidal API,
producing VideoSource objects compatible with the existing Activity streaming pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from credentials import creds
from video import VideoSource

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "hellodj_video"

# Tidal URL patterns
_TIDAL_DOMAINS: frozenset[str] = frozenset({"tidal.com", "www.tidal.com", "listen.tidal.com"})
_TIDAL_VIDEO_PATH_RE = re.compile(r"/(?:browse/)?video/(\d+)")

# Timeouts
_API_REQUEST_TIMEOUT: float = 15.0  # 15 seconds for API requests
_DOWNLOAD_TIMEOUT: float = 600.0  # 10 minutes for video download

# Default tidalapi internal client ID (fallback when no issuing_client_id stored)
_FALLBACK_CLIENT_ID = "zU4XHVVkc2tDPo4t"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TidalResolverError(Exception):
    """Raised when Tidal resolution fails."""

    def __init__(self, message: str, recoverable: bool = False) -> None:
        super().__init__(message)
        self.recoverable = recoverable


# ---------------------------------------------------------------------------
# TidalResolver
# ---------------------------------------------------------------------------


class TidalResolver:
    """Resolve Tidal music video URLs and search queries to VideoSource."""

    _API_BASE = "https://api.tidal.com/v1"
    _AUTH_URL = "https://auth.tidal.com/v1/oauth2/token"
    _TOKEN_EXPIRY_BUFFER = 300  # 5 minutes safety buffer

    def __init__(self, download_dir: Path | None = None) -> None:
        self.download_dir = download_dir or _DOWNLOAD_DIR
        self.download_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def resolve_url(self, url: str) -> VideoSource:
        """Resolve a Tidal music video URL to a downloadable VideoSource.

        Extracts the video ID from the URL, fetches metadata and stream URL
        from the Tidal API, downloads the video file, and returns a VideoSource.

        Args:
            url: A Tidal URL matching tidal.com/*/video/* or tidal.com/video/*

        Returns:
            VideoSource with source_type="tidal"

        Raises:
            TidalResolverError: On auth failure, not found, no video stream, etc.
        """
        video_id = self.extract_video_id(url)
        if video_id is None:
            raise TidalResolverError("Invalid Tidal video URL")

        # Get a valid access token
        access_token = await self._ensure_token()

        # Fetch metadata
        metadata = await self._fetch_video_metadata(video_id, access_token)

        # Fetch stream URL (HLS manifest from Tidal)
        stream_url = await self._fetch_stream_url(video_id, access_token)

        # Build artist string from metadata
        title = metadata.get("title", f"Tidal Video {video_id}")
        artist = metadata.get("artist", "")
        track_title = metadata.get("title", "")

        # Compose display title
        display_title = f"{artist} — {track_title}" if artist else track_title

        return VideoSource(
            source_type="tidal",
            file_path="",  # No local file — streaming directly from URL
            title=display_title,
            duration_seconds=float(metadata.get("duration", 0)),
            metadata={
                "artist": artist,
                "track_title": track_title,
                "video_id": video_id,
                "tidal_url": url,
            },
            audio_url=None,
            cleanup_on_finish=False,
            stream_url=stream_url,
        )

    async def search(self, query: str) -> VideoSource:
        """Search Tidal for music videos and resolve the top result.

        Args:
            query: Search text (after stripping the 'tidal:' prefix).
                   Must be 1-200 characters, non-whitespace-only.

        Returns:
            VideoSource with source_type="tidal"

        Raises:
            TidalResolverError: On no results, auth failure, unavailable, etc.
        """
        # Validate query
        if not query or not query.strip():
            raise TidalResolverError("A search query is required")

        # Truncate to 200 characters (don't error)
        search_text = query[:200]

        # Get a valid access token
        access_token = await self._ensure_token()

        # Search Tidal API for music videos
        video_id = await self._search_videos(search_text, access_token)

        # Resolve the first result through the same download flow as URL resolution
        try:
            metadata = await self._fetch_video_metadata(video_id, access_token)
        except TidalResolverError:
            raise

        try:
            stream_url = await self._fetch_stream_url(video_id, access_token)
        except TidalResolverError as exc:
            # If stream fetch fails for the selected result, it's unavailable
            if "no music video" in str(exc).lower() or "unavailable" in str(exc).lower():
                raise TidalResolverError("This video is unavailable")
            raise

        # Build artist string from metadata
        title = metadata.get("title", f"Tidal Video {video_id}")
        artist = metadata.get("artist", "")
        track_title = metadata.get("title", "")

        # Compose display title
        display_title = f"{artist} — {track_title}" if artist else track_title

        return VideoSource(
            source_type="tidal",
            file_path="",  # No local file — streaming directly from URL
            title=display_title,
            duration_seconds=float(metadata.get("duration", 0)),
            metadata={
                "artist": artist,
                "track_title": track_title,
                "video_id": video_id,
                "tidal_url": f"https://tidal.com/browse/video/{video_id}",
            },
            audio_url=None,
            cleanup_on_finish=False,
            stream_url=stream_url,
        )

    def extract_video_id(self, url: str) -> int | None:
        """Extract numeric video ID from a Tidal URL path.

        Matches patterns:
            - tidal.com/browse/video/12345
            - tidal.com/video/12345
            - listen.tidal.com/video/12345
            - www.tidal.com/browse/video/12345

        Returns None if the URL doesn't match a Tidal video pattern.
        """
        try:
            parsed = urlparse(url)
        except (ValueError, AttributeError):
            return None

        # Check domain
        hostname = (parsed.hostname or "").lower()
        if hostname not in _TIDAL_DOMAINS:
            return None

        # Match path pattern
        match = _TIDAL_VIDEO_PATH_RE.search(parsed.path)
        if match is None:
            return None

        try:
            return int(match.group(1))
        except (ValueError, IndexError):
            return None

    # ------------------------------------------------------------------
    # Token management (stubs — full implementation in task 2.2)
    # ------------------------------------------------------------------

    async def _ensure_token(self) -> str:
        """Ensure a valid Tidal access token is available.

        Checks expiry (with 5-minute buffer). Refreshes if needed.
        Returns the access token string.

        Raises:
            TidalResolverError: If no credentials stored or refresh fails.
        """
        # Check if we have credentials at all
        refresh_token = creds.get("tidal.refresh_token")
        if not refresh_token:
            raise TidalResolverError(
                "Tidal is not connected — use the web UI to authenticate",
                recoverable=False,
            )

        access_token = creds.get("tidal.access_token")
        expiry_str = creds.get("tidal.expiry")

        # Check if token is still valid (with 5-minute buffer)
        if access_token and expiry_str:
            try:
                expiry = float(expiry_str)
                if time.time() < (expiry - self._TOKEN_EXPIRY_BUFFER):
                    return access_token
            except (ValueError, TypeError):
                pass  # Invalid expiry — need refresh

        # Token expired or missing — refresh
        return await self._refresh_token()

    async def _refresh_token(self) -> str:
        """Refresh the Tidal OAuth access token using the stored refresh token.

        Updates the credential store with the new access token, expiry,
        and refresh token (if a new one is provided in the response).

        Returns the new access token.

        Raises:
            TidalResolverError: If the refresh token is invalid/expired.
        """
        refresh_token = creds.get("tidal.refresh_token")
        if not refresh_token:
            raise TidalResolverError(
                "Tidal is not connected — use the web UI to authenticate",
                recoverable=False,
            )

        # Determine client ID
        client_id = creds.get("tidal.issuing_client_id")
        if not client_id:
            client_id = _FALLBACK_CLIENT_ID

        # POST to token endpoint
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }

        timeout = aiohttp.ClientTimeout(total=_API_REQUEST_TIMEOUT)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self._AUTH_URL, data=data) as resp:
                    if resp.status in (400, 401):
                        log.warning("Tidal token refresh failed (HTTP %d) — re-login required", resp.status)
                        raise TidalResolverError(
                            "Tidal authentication expired — re-login required",
                            recoverable=False,
                        )

                    if resp.status >= 400:
                        raise TidalResolverError(
                            "Tidal API request failed — try again later",
                            recoverable=True,
                        )

                    result = await resp.json()

        except aiohttp.ClientError as exc:
            raise TidalResolverError(
                "Tidal API request failed — try again later",
                recoverable=True,
            ) from exc
        except asyncio.TimeoutError:
            raise TidalResolverError(
                "Tidal API request failed — try again later",
                recoverable=True,
            )

        # Update credential store
        new_access_token = result.get("access_token", "")
        if not new_access_token:
            raise TidalResolverError(
                "Tidal token refresh returned empty access token",
                recoverable=True,
            )

        expires_in = int(result.get("expires_in", 3600))
        new_expiry = time.time() + expires_in

        creds.set("tidal.access_token", new_access_token)
        creds.set("tidal.expiry", str(new_expiry))

        # Update refresh token if a new one was provided
        new_refresh_token = result.get("refresh_token")
        if new_refresh_token:
            creds.set("tidal.refresh_token", new_refresh_token)

        log.info("Tidal access token refreshed (expires in %ds)", expires_in)
        return new_access_token

    # ------------------------------------------------------------------
    # Private API methods
    # ------------------------------------------------------------------

    async def _search_videos(self, query: str, access_token: str) -> int:
        """Search Tidal API for music videos and return the first result's video ID.

        GET /search/videos with params: query, limit=1, countryCode=US

        Returns the video ID of the first result.
        Raises TidalResolverError on no results, auth errors, or network issues.
        """
        url = f"{self._API_BASE}/search/videos"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        params = {
            "query": query,
            "limit": "1",
            "countryCode": "US",
        }

        timeout = aiohttp.ClientTimeout(total=_API_REQUEST_TIMEOUT)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, params=params) as resp:
                    if resp.status == 401:
                        # Token may have expired — attempt one refresh and retry
                        new_token = await self._refresh_token()
                        headers["Authorization"] = f"Bearer {new_token}"
                        async with session.get(url, headers=headers, params=params) as retry_resp:
                            if retry_resp.status == 401:
                                raise TidalResolverError(
                                    "Tidal authentication expired — re-login required",
                                    recoverable=False,
                                )
                            if retry_resp.status >= 400:
                                raise TidalResolverError(
                                    "Tidal search failed — try again later",
                                    recoverable=True,
                                )
                            data = await retry_resp.json()
                    elif resp.status >= 400:
                        raise TidalResolverError(
                            "Tidal search failed — try again later",
                            recoverable=True,
                        )
                    else:
                        data = await resp.json()

        except aiohttp.ClientError as exc:
            raise TidalResolverError(
                "Tidal API request failed — try again later",
                recoverable=True,
            ) from exc
        except asyncio.TimeoutError:
            raise TidalResolverError(
                "Tidal API request failed — try again later",
                recoverable=True,
            )

        # Extract first result
        items = data.get("items", [])
        if not items:
            raise TidalResolverError("No Tidal music videos matched your search")

        video_id = items[0].get("id")
        if video_id is None:
            raise TidalResolverError("No Tidal music videos matched your search")

        return int(video_id)

    async def _fetch_video_metadata(self, video_id: int, access_token: str) -> dict:
        """Fetch video metadata from Tidal API.

        GET /videos/{video_id}

        Returns dict with: title, duration, artist(s)
        Raises TidalResolverError on 404, auth errors, or network issues.
        """
        url = f"{self._API_BASE}/videos/{video_id}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        params = {"countryCode": "US"}

        timeout = aiohttp.ClientTimeout(total=_API_REQUEST_TIMEOUT)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, params=params) as resp:
                    if resp.status == 401:
                        # Token may have expired mid-request — attempt one refresh
                        new_token = await self._refresh_token()
                        headers["Authorization"] = f"Bearer {new_token}"
                        async with session.get(url, headers=headers, params=params) as retry_resp:
                            if retry_resp.status == 401:
                                raise TidalResolverError(
                                    "Tidal authentication expired — re-login required",
                                    recoverable=False,
                                )
                            if retry_resp.status == 404:
                                raise TidalResolverError("Tidal video not found")
                            if retry_resp.status >= 400:
                                raise TidalResolverError(
                                    "Tidal API request failed — try again later",
                                    recoverable=True,
                                )
                            data = await retry_resp.json()
                    elif resp.status == 404:
                        raise TidalResolverError("Tidal video not found")
                    elif resp.status == 403:
                        raise TidalResolverError(
                            "This video is unavailable in the current region"
                        )
                    elif resp.status >= 400:
                        raise TidalResolverError(
                            "Tidal API request failed — try again later",
                            recoverable=True,
                        )
                    else:
                        data = await resp.json()

        except aiohttp.ClientError as exc:
            raise TidalResolverError(
                "Tidal API request failed — try again later",
                recoverable=True,
            ) from exc
        except asyncio.TimeoutError:
            raise TidalResolverError(
                "Tidal API request failed — try again later",
                recoverable=True,
            )

        # Extract metadata
        title = data.get("title", "")
        duration = data.get("duration", 0)

        # Extract artist name(s)
        artists = data.get("artists", []) or data.get("artist", {})
        if isinstance(artists, list) and artists:
            artist = ", ".join(a.get("name", "") for a in artists if a.get("name"))
        elif isinstance(artists, dict):
            artist = artists.get("name", "")
        else:
            artist = ""

        return {
            "title": title,
            "duration": duration,
            "artist": artist,
            "video_id": video_id,
        }

    async def _fetch_stream_url(self, video_id: int, access_token: str) -> str:
        """Fetch the highest-quality video stream URL from Tidal.

        GET /videos/{video_id}/streamurl with quality=HIGH

        Returns the direct stream URL for download.
        Raises TidalResolverError if no video stream is available.
        """
        url = f"{self._API_BASE}/videos/{video_id}/streamurl"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        params = {
            "videoQuality": "HIGH",
            "countryCode": "US",
        }

        timeout = aiohttp.ClientTimeout(total=_API_REQUEST_TIMEOUT)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, params=params) as resp:
                    if resp.status == 401:
                        # Attempt token refresh and retry
                        new_token = await self._refresh_token()
                        headers["Authorization"] = f"Bearer {new_token}"
                        async with session.get(url, headers=headers, params=params) as retry_resp:
                            if retry_resp.status == 401:
                                raise TidalResolverError(
                                    "Tidal authentication expired — re-login required",
                                    recoverable=False,
                                )
                            if retry_resp.status == 404:
                                raise TidalResolverError(
                                    "This track has no music video available"
                                )
                            if retry_resp.status == 403:
                                raise TidalResolverError(
                                    "This video is unavailable in the current region"
                                )
                            if retry_resp.status >= 400:
                                raise TidalResolverError(
                                    "Tidal API request failed — try again later",
                                    recoverable=True,
                                )
                            data = await retry_resp.json()
                    elif resp.status == 404:
                        raise TidalResolverError(
                            "This track has no music video available"
                        )
                    elif resp.status == 403:
                        raise TidalResolverError(
                            "This video is unavailable in the current region"
                        )
                    elif resp.status >= 400:
                        raise TidalResolverError(
                            "Tidal API request failed — try again later",
                            recoverable=True,
                        )
                    else:
                        data = await resp.json()

        except aiohttp.ClientError as exc:
            raise TidalResolverError(
                "Tidal API request failed — try again later",
                recoverable=True,
            ) from exc
        except asyncio.TimeoutError:
            raise TidalResolverError(
                "Tidal API request failed — try again later",
                recoverable=True,
            )

        # Extract stream URL — Tidal returns url field in streamurl response
        stream_url = data.get("url", "")
        if not stream_url:
            raise TidalResolverError("This track has no music video available")

        return stream_url

    async def _download_video(self, stream_url: str, title: str) -> str:
        """Download the video from the stream URL to a temporary file.

        Tidal returns HLS manifest URLs (m3u8). We use ffmpeg to download and
        remux the HLS stream into an MP4 container.

        Args:
            stream_url: HLS manifest URL (or direct URL) from Tidal
            title: Video title (used for filename sanitization)

        Returns:
            Path to the downloaded MP4 file.

        Raises:
            TidalResolverError: On download timeout (10 min) or ffmpeg error.
        """
        # Sanitize title for filename
        safe_title = re.sub(r'[^\w\s\-.]', '', title)[:80].strip() or "tidal_video"
        unique_name = f"{uuid.uuid4().hex[:8]}_{safe_title}.mp4"
        output_path = self.download_dir / unique_name

        # Use ffmpeg to download and remux HLS → MP4
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-y",  # overwrite
            "-i", stream_url,
            "-c", "copy",  # just remux, no re-encode
            str(output_path),
        ]

        try:
            proc = await asyncio.wait_for(
                asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                ),
                timeout=30,  # timeout for starting the process
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=_DOWNLOAD_TIMEOUT,
            )
        except asyncio.TimeoutError:
            output_path.unlink(missing_ok=True)
            raise TidalResolverError("Video download timed out", recoverable=True)
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            raise TidalResolverError(
                f"Video download failed: {exc}", recoverable=True
            ) from exc

        if proc.returncode != 0:
            err_msg = stderr.decode(errors="replace")[:500] if stderr else "unknown error"
            output_path.unlink(missing_ok=True)
            log.warning("TidalResolver: ffmpeg download failed (rc=%d): %s", proc.returncode, err_msg)
            raise TidalResolverError(
                "Video download failed — stream may be unavailable",
                recoverable=True,
            )

        if not output_path.exists() or output_path.stat().st_size == 0:
            output_path.unlink(missing_ok=True)
            raise TidalResolverError(
                "Video download produced empty file",
                recoverable=True,
            )

        log.info("TidalResolver: downloaded %s (%d bytes)", output_path.name, output_path.stat().st_size)
        return str(output_path)
