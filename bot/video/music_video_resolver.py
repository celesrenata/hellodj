"""Music Video Query Classifier and Spotify Metadata Extractor.

Classifies user input into exactly one MusicVideoSourceType based on URL domain
and path structure, or falls back to text_search for non-URL inputs.

Also provides SpotifyMetadataExtractor for fetching track metadata via the
Spotify Web API (client_credentials OAuth2 flow).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs, urlparse

import aiohttp

from config import cfg

log = logging.getLogger(__name__)


class MusicVideoSourceType(Enum):
    """Source types recognized by the music video resolver."""

    YOUTUBE_DIRECT = "youtube_direct"
    YOUTUBE_MUSIC = "youtube_music"
    TIDAL_VIDEO = "tidal_video"
    TIDAL_TRACK = "tidal_track"
    SPOTIFY_TRACK = "spotify_track"
    TEXT_SEARCH = "text_search"


@dataclass(frozen=True)
class MusicVideoClassification:
    """Result of classifying a user query into a source type.

    Attributes:
        source_type: The detected provider/source category.
        original_query: The raw user input, preserved for downstream use.
        extracted_id: Video or track ID extracted from the URL when applicable.
    """

    source_type: MusicVideoSourceType
    original_query: str
    extracted_id: str | None = None


# --- URL pattern regexes (compiled once at module load) ---

_YOUTUBE_MUSIC_RE = re.compile(
    r"https?://music\.youtube\.com/watch", re.IGNORECASE
)
_YOUTUBE_RE = re.compile(
    r"https?://(?:www\.)?youtube\.com/", re.IGNORECASE
)
_YOUTU_BE_RE = re.compile(
    r"https?://youtu\.be/", re.IGNORECASE
)
_TIDAL_VIDEO_RE = re.compile(
    r"https?://(?:www\.)?(?:listen\.)?tidal\.com/(?:browse/)?video/(\d+)", re.IGNORECASE
)
_TIDAL_TRACK_RE = re.compile(
    r"https?://(?:www\.)?(?:listen\.)?tidal\.com/(?:browse/)?track/(\d+)", re.IGNORECASE
)
_SPOTIFY_TRACK_RE = re.compile(
    r"https?://open\.spotify\.com/track/([A-Za-z0-9]+)", re.IGNORECASE
)


def _extract_youtube_music_video_id(url: str) -> str | None:
    """Extract the `v` query parameter from a YouTube Music URL."""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        v_values = params.get("v")
        if v_values:
            return v_values[0]
    except Exception:
        pass
    return None


def classify_music_video_query(query: str) -> MusicVideoClassification:
    """Classify a user query into exactly one MusicVideoSourceType.

    Classification priority (checked in this order):
    1. music.youtube.com → youtube_music (extract video ID from `v` param)
    2. youtube.com or youtu.be → youtube_direct (no ID extraction)
    3. tidal.com with /video/ or /browse/video/ → tidal_video
    4. tidal.com with /track/ or /browse/track/ → tidal_track (extract track ID)
    5. open.spotify.com/track/ → spotify_track (extract track ID)
    6. No URL scheme (`://`) detected → text_search

    Note: music.youtube.com is checked BEFORE youtube.com because
    "music.youtube.com" contains "youtube.com" as a substring.

    This function is pure — no I/O, no exceptions raised. It always returns
    a valid classification.
    """
    stripped = query.strip()

    # No URL scheme → text search
    if "://" not in stripped:
        return MusicVideoClassification(
            source_type=MusicVideoSourceType.TEXT_SEARCH,
            original_query=query,
        )

    # 1. YouTube Music (must check before generic youtube.com)
    if _YOUTUBE_MUSIC_RE.search(stripped):
        video_id = _extract_youtube_music_video_id(stripped)
        return MusicVideoClassification(
            source_type=MusicVideoSourceType.YOUTUBE_MUSIC,
            original_query=query,
            extracted_id=video_id,
        )

    # 2. YouTube direct (youtube.com or youtu.be)
    if _YOUTUBE_RE.search(stripped) or _YOUTU_BE_RE.search(stripped):
        return MusicVideoClassification(
            source_type=MusicVideoSourceType.YOUTUBE_DIRECT,
            original_query=query,
        )

    # 3. Tidal video
    tidal_video_match = _TIDAL_VIDEO_RE.search(stripped)
    if tidal_video_match:
        return MusicVideoClassification(
            source_type=MusicVideoSourceType.TIDAL_VIDEO,
            original_query=query,
        )

    # 4. Tidal track
    tidal_track_match = _TIDAL_TRACK_RE.search(stripped)
    if tidal_track_match:
        return MusicVideoClassification(
            source_type=MusicVideoSourceType.TIDAL_TRACK,
            original_query=query,
            extracted_id=tidal_track_match.group(1),
        )

    # 5. Spotify track
    spotify_match = _SPOTIFY_TRACK_RE.search(stripped)
    if spotify_match:
        return MusicVideoClassification(
            source_type=MusicVideoSourceType.SPOTIFY_TRACK,
            original_query=query,
            extracted_id=spotify_match.group(1),
        )

    # 6. Fallback: has a scheme but didn't match any known provider
    return MusicVideoClassification(
        source_type=MusicVideoSourceType.TEXT_SEARCH,
        original_query=query,
    )


# ---------------------------------------------------------------------------
# Spotify Metadata Extraction
# ---------------------------------------------------------------------------

_SPOTIFY_AUTH_URL = "https://accounts.spotify.com/api/token"
_SPOTIFY_API_BASE = "https://api.spotify.com/v1"
_SPOTIFY_TIMEOUT = aiohttp.ClientTimeout(total=10)


@dataclass(frozen=True)
class TrackMetadata:
    """Metadata extracted from a Spotify track.

    Attributes:
        artist: The primary artist name.
        title: The track title.
        isrc: International Standard Recording Code, if available.
    """

    artist: str
    title: str
    isrc: str | None = None


class SpotifyMetadataError(Exception):
    """Raised when Spotify metadata extraction fails.

    Covers auth failures, track-not-found, and network errors.
    """


class SpotifyMetadataExtractor:
    """Extract artist/title from Spotify track URLs via Spotify Web API.

    Uses the client_credentials OAuth2 flow — no user login required.
    Access tokens are cached based on the ``expires_in`` response field.
    """

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_ttl_buffer: int = 60,
    ) -> None:
        self._client_id = client_id or cfg("spotify.client_id") or ""
        self._client_secret = client_secret or cfg("spotify.client_secret") or ""
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    # ── OAuth token management ────────────────────────────────────────────

    async def _ensure_token(self) -> str:
        """Return a valid access token, fetching/refreshing as needed."""
        if self._access_token and time.monotonic() < self._expires_at:
            return self._access_token

        async with self._token_lock:
            # Double-check after acquiring lock.
            if self._access_token and time.monotonic() < self._expires_at:
                return self._access_token
            token, expires_in = await self._fetch_token()
            self._access_token = token
            self._expires_at = time.monotonic() + max(expires_in - 60, 0)
            log.info("spotify: acquired access token (expires_in=%ss)", expires_in)
            return token

    async def _fetch_token(self) -> tuple[str, int]:
        """POST client_credentials grant to Spotify token endpoint."""
        if not self._client_id or not self._client_secret:
            raise SpotifyMetadataError(
                "Spotify is not configured. Set spotify.client_id and "
                "spotify.client_secret in the credential store."
            )

        data = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }

        try:
            async with aiohttp.ClientSession(timeout=_SPOTIFY_TIMEOUT) as session:
                async with session.post(
                    _SPOTIFY_AUTH_URL,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.error(
                            "spotify: token request failed status=%s body=%r",
                            resp.status,
                            body,
                        )
                        raise SpotifyMetadataError(
                            f"Spotify auth failed (HTTP {resp.status})"
                        )
                    payload = await resp.json()
                    access_token = payload.get("access_token")
                    expires_in = payload.get("expires_in", 3600)
                    if not access_token:
                        raise SpotifyMetadataError(
                            "Spotify auth response missing access_token"
                        )
                    return access_token, int(expires_in)
        except SpotifyMetadataError:
            raise
        except asyncio.TimeoutError as exc:
            raise SpotifyMetadataError("Spotify auth request timed out") from exc
        except aiohttp.ClientError as exc:
            raise SpotifyMetadataError(
                f"Spotify auth network error: {exc}"
            ) from exc

    # ── Public API ────────────────────────────────────────────────────────

    async def extract(self, track_id: str) -> TrackMetadata:
        """Fetch track metadata from the Spotify Web API.

        Args:
            track_id: The Spotify track ID (alphanumeric string from the URL).

        Returns:
            TrackMetadata with artist, title, and optional ISRC.

        Raises:
            SpotifyMetadataError: On auth failure, track not found, or network error.
        """
        token = await self._ensure_token()
        url = f"{_SPOTIFY_API_BASE}/tracks/{track_id}"
        headers = {"Authorization": f"Bearer {token}"}

        try:
            async with aiohttp.ClientSession(timeout=_SPOTIFY_TIMEOUT) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 401:
                        # Token may have expired between check and use; invalidate.
                        self._access_token = None
                        self._expires_at = 0.0
                        raise SpotifyMetadataError(
                            "Spotify API returned 401 — token may be expired"
                        )
                    if resp.status == 404:
                        raise SpotifyMetadataError(
                            f"Spotify track not found: {track_id}"
                        )
                    if resp.status != 200:
                        body = await resp.text()
                        raise SpotifyMetadataError(
                            f"Spotify API error (HTTP {resp.status}): {body[:200]}"
                        )
                    payload = await resp.json()
        except SpotifyMetadataError:
            raise
        except asyncio.TimeoutError as exc:
            raise SpotifyMetadataError(
                "Spotify API request timed out"
            ) from exc
        except aiohttp.ClientError as exc:
            raise SpotifyMetadataError(
                f"Spotify API network error: {exc}"
            ) from exc

        # Extract artist name (first artist) and title.
        artists = payload.get("artists", [])
        artist_name = artists[0]["name"] if artists else "Unknown Artist"
        title = payload.get("name", "Unknown Title")

        # Extract ISRC from external_ids if present.
        external_ids = payload.get("external_ids", {})
        isrc = external_ids.get("isrc")

        return TrackMetadata(artist=artist_name, title=title, isrc=isrc)


# ---------------------------------------------------------------------------
# MusicVideoResolver — Orchestrator
# ---------------------------------------------------------------------------


class MusicVideoResolverError(Exception):
    """Raised when music video resolution fails.

    Attributes:
        user_message: A short, user-facing description of the failure suitable
            for display in a Discord followup message.
    """

    def __init__(self, message: str, user_message: str | None = None) -> None:
        super().__init__(message)
        self.user_message = user_message or message


class MusicVideoResolver:
    """Resolve music video queries to VideoSource objects.

    Orchestrates classification → metadata extraction → sub-resolver dispatch
    with fallback logic per source type.

    The resolver composes:
    - YouTubeResolver for YouTube direct, YouTube Music, and fallback searches
    - TidalResolver for Tidal video and Tidal track-to-video lookup
    - SpotifyMetadataExtractor for Spotify track → YouTube search fallback
    """

    def __init__(
        self,
        *,
        youtube_resolver: "YouTubeResolver | None" = None,
        tidal_resolver: "TidalResolver | None" = None,
        spotify_extractor: SpotifyMetadataExtractor | None = None,
    ) -> None:
        # Lazy imports to avoid circular dependencies at module level
        from video.sources import YouTubeResolver
        from video.tidal_resolver import TidalResolver

        self._youtube = youtube_resolver or YouTubeResolver()
        self._tidal = tidal_resolver or TidalResolver()
        self._spotify = spotify_extractor or SpotifyMetadataExtractor()

    async def resolve(self, query: str) -> "VideoSource":
        """Classify, resolve, and return a VideoSource for the given query.

        Resolution flow:
        1. Classify the query via classify_music_video_query
        2. Dispatch to the appropriate private resolver method
        3. Apply fallback logic on failure where applicable
        4. Return a VideoSource ready for ActivityStreamer

        Args:
            query: A URL or plain-text search string from the user.

        Returns:
            A VideoSource object ready for the Video Activity pipeline.

        Raises:
            MusicVideoResolverError: When all resolution paths fail.
        """
        from video import VideoSource  # noqa: F811

        classification = classify_music_video_query(query)
        log.info(
            "MusicVideoResolver: classified %r as %s (id=%s)",
            query,
            classification.source_type.value,
            classification.extracted_id,
        )

        dispatch = {
            MusicVideoSourceType.YOUTUBE_DIRECT: self._resolve_youtube_direct,
            MusicVideoSourceType.YOUTUBE_MUSIC: self._resolve_youtube_music,
            MusicVideoSourceType.TIDAL_VIDEO: self._resolve_tidal_video,
            MusicVideoSourceType.TIDAL_TRACK: self._resolve_tidal_track,
            MusicVideoSourceType.SPOTIFY_TRACK: self._resolve_spotify_track,
            MusicVideoSourceType.TEXT_SEARCH: self._resolve_text_search,
        }

        handler = dispatch.get(classification.source_type)
        if handler is None:
            raise MusicVideoResolverError(
                f"Unhandled source type: {classification.source_type}",
                user_message="An unexpected error occurred.",
            )

        return await handler(classification)

    # ------------------------------------------------------------------
    # Private dispatch methods (stubs — implemented in tasks 4.2–4.6)
    # ------------------------------------------------------------------

    async def _resolve_youtube_direct(
        self, classification: MusicVideoClassification
    ) -> "VideoSource":
        """Resolve a youtube.com or youtu.be URL via YouTubeResolver.

        Passes the full original URL directly to YouTubeResolver.resolve().
        """
        from video.sources import YouTubeResolverError

        try:
            return await self._youtube.resolve(classification.original_query)
        except YouTubeResolverError as exc:
            raise MusicVideoResolverError(
                f"YouTube resolution failed: {exc}",
                user_message="YouTube video is unavailable or could not be downloaded.",
            ) from exc

    async def _resolve_youtube_music(
        self, classification: MusicVideoClassification
    ) -> "VideoSource":
        """Resolve a music.youtube.com URL by extracting the video ID.

        Constructs a standard YouTube URL from the extracted video ID
        and delegates to YouTubeResolver.
        """
        from video.sources import YouTubeResolverError

        if classification.extracted_id is None:
            raise MusicVideoResolverError(
                "YouTube Music URL missing video ID",
                user_message="Could not extract video ID from YouTube Music URL.",
            )

        url = f"https://youtube.com/watch?v={classification.extracted_id}"
        try:
            return await self._youtube.resolve(url)
        except YouTubeResolverError as exc:
            raise MusicVideoResolverError(
                f"YouTube Music resolution failed: {exc}",
                user_message="YouTube video is unavailable or could not be downloaded.",
            ) from exc

    async def _resolve_tidal_video(
        self, classification: MusicVideoClassification
    ) -> "VideoSource":
        """Resolve a Tidal video URL via TidalResolver.resolve_url().

        On recoverable TidalResolverError, falls back to YouTube search.
        On non-recoverable TidalResolverError, raises MusicVideoResolverError.
        """
        from video.sources import YouTubeResolverError
        from video.tidal_resolver import TidalResolverError

        try:
            return await self._tidal.resolve_url(classification.original_query)
        except TidalResolverError as exc:
            if not exc.recoverable:
                raise MusicVideoResolverError(
                    f"Tidal video resolution failed (non-recoverable): {exc}",
                    user_message="Tidal video is unavailable.",
                ) from exc

            # Recoverable error — fall back to YouTube search.
            # Try to extract useful context from the error message for the search query.
            error_msg = str(exc)
            fallback_query = error_msg if error_msg and len(error_msg) > 3 else None

            if not fallback_query:
                # Strip the domain from the original URL to use as a search hint.
                url = classification.original_query
                # Remove protocol and domain, use remaining path segments
                stripped = re.sub(
                    r"https?://(?:www\.)?(?:listen\.)?tidal\.com/(?:browse/)?video/",
                    "",
                    url,
                    flags=re.IGNORECASE,
                )
                fallback_query = stripped if stripped else "tidal music video"

            log.info(
                "MusicVideoResolver: Tidal recoverable error, falling back to YouTube search: %r",
                fallback_query,
            )

            try:
                return await self._youtube.resolve(
                    f"{fallback_query} official music video"
                )
            except YouTubeResolverError as yt_exc:
                raise MusicVideoResolverError(
                    f"YouTube fallback search also failed: {yt_exc}",
                    user_message="No music video found for that query.",
                ) from yt_exc

    async def _resolve_tidal_track(
        self, classification: MusicVideoClassification
    ) -> "VideoSource":
        """Resolve a Tidal track URL to a music video.

        Flow:
        1. Fetch track metadata from Tidal API (GET /v1/tracks/{track_id})
        2. Search Tidal videos for "{artist} - {title}" via TidalResolver.search
        3. If video found: return the VideoSource from TidalResolver
        4. If no video (TidalResolverError): YouTube fallback "{artist} - {title} official music video"
        5. If Tidal track API fails: YouTube fallback with available metadata
        """
        from video.tidal_resolver import TidalResolverError

        track_id = classification.extracted_id
        if not track_id:
            raise MusicVideoResolverError(
                "No track ID extracted from Tidal track URL",
                user_message="Could not extract a track ID from that Tidal URL.",
            )

        artist: str | None = None
        title: str | None = None

        # Step 1: Fetch track metadata from Tidal API
        try:
            token = await self._tidal._ensure_token()
            api_url = f"{self._tidal._API_BASE}/tracks/{track_id}?countryCode=US"
            headers = {"Authorization": f"Bearer {token}"}
            timeout = aiohttp.ClientTimeout(total=15)

            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(api_url, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        artist = data.get("artist", {}).get("name")
                        title = data.get("title")
                    else:
                        log.warning(
                            "Tidal track API returned %s for track %s",
                            resp.status,
                            track_id,
                        )
        except Exception as exc:
            log.warning(
                "Tidal track API failed for track %s: %s", track_id, exc
            )

        # Step 2: If we have metadata, try Tidal video search first (source priority)
        if artist and title:
            search_query = f"{artist} - {title}"
            try:
                # Requirement 9.1: check Tidal for native video before YouTube
                return await self._tidal.search(search_query)
            except TidalResolverError:
                log.info(
                    "No Tidal video found for %r, falling back to YouTube",
                    search_query,
                )

            # Step 3: YouTube fallback with "{artist} - {title} official music video"
            try:
                yt_query = f"ytsearch:{artist} - {title} official music video"
                return await self._youtube.resolve(yt_query)
            except Exception as exc:
                raise MusicVideoResolverError(
                    f"YouTube fallback failed for Tidal track {track_id}: {exc}",
                    user_message="No music video found for that track.",
                ) from exc

        # Step 4: Tidal API failed — fallback YouTube search with track ID only
        try:
            yt_query = f"ytsearch:tidal track {track_id} official music video"
            return await self._youtube.resolve(yt_query)
        except Exception as exc:
            raise MusicVideoResolverError(
                f"All resolution paths failed for Tidal track {track_id}: {exc}",
                user_message="No music video found for that track.",
            ) from exc

    async def _resolve_spotify_track(
        self, classification: MusicVideoClassification
    ) -> "VideoSource":
        """Resolve a Spotify track URL to a music video via metadata + YouTube search.

        Extracts artist/title from Spotify, then searches YouTube for
        "{artist} - {title} official music video".
        """
        from video.sources import YouTubeResolverError

        track_id = classification.extracted_id
        if not track_id:
            raise MusicVideoResolverError(
                "No track ID in Spotify classification",
                user_message="Could not extract track ID from Spotify URL.",
            )

        # Step 1: Get metadata from Spotify API
        try:
            metadata = await self._spotify.extract(track_id)
        except SpotifyMetadataError as exc:
            raise MusicVideoResolverError(
                f"Spotify metadata extraction failed: {exc}",
                user_message="Could not retrieve track info from Spotify.",
            ) from exc

        # Step 2: Search YouTube for the music video
        search_query = (
            f"ytsearch:{metadata.artist} - {metadata.title} official music video"
        )
        log.info(
            "MusicVideoResolver: spotify_track → YouTube search %r", search_query
        )

        try:
            return await self._youtube.resolve(search_query)
        except YouTubeResolverError as exc:
            raise MusicVideoResolverError(
                f"YouTube search failed for Spotify track: {exc}",
                user_message="No music video found for that query.",
            ) from exc

    async def _resolve_text_search(
        self, classification: MusicVideoClassification
    ) -> "VideoSource":
        """Resolve a plain text query via YouTube search.

        Searches YouTube for "{query} official music video".
        """
        from video.sources import YouTubeResolverError

        search_text = classification.original_query.strip()
        if not search_text:
            raise MusicVideoResolverError(
                "Empty text search query",
                user_message="Please provide a URL or search query.",
            )

        search_query = f"ytsearch:{search_text} official music video"
        try:
            return await self._youtube.resolve(search_query)
        except YouTubeResolverError as exc:
            raise MusicVideoResolverError(
                f"YouTube search failed for {search_text!r}: {exc}",
                user_message="No music video found for that query.",
            ) from exc
