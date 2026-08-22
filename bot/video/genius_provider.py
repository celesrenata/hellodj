"""Genius API client for plain text lyrics fetch.

Extracted from bot/cogs/lyrics.py into a shared module for use by both
the /lyrics cog (chat embed) and LyricsService (overlay fallback).

The GeniusProvider searches Genius and scrapes the lyrics page for plain
text. It does NOT provide timing data — the caller handles beat-estimated
timing computation.

Requirements: 1.1, 1.4, 9.3
"""

from __future__ import annotations

import logging
import re

import aiohttp

log = logging.getLogger(__name__)

_USER_AGENT = "HelloDJ/1.0 (https://hellodj.celestium.life)"
_GENIUS_API_URL = "https://api.genius.com"
_TIMEOUT_S = 5


class GeniusProvider:
    """Genius API client for fetching plain text lyrics.

    Usage:
        provider = GeniusProvider(access_token="...")
        text = await provider.fetch("Song Title", "Artist Name")

    Returns plain text lyrics or None if not found / on error.
    All errors are logged at debug level and never raised to the caller.
    """

    def __init__(self, access_token: str) -> None:
        self._token = access_token

    async def fetch(self, title: str, artist: str) -> str | None:
        """Search Genius for a song and return its plain text lyrics.

        Args:
            title: The track title.
            artist: The track artist name.

        Returns:
            Plain text lyrics string, or None if not found or on error.
        """
        if not self._token:
            log.debug("Genius: no access token configured, skipping")
            return None

        headers = {
            "Authorization": f"Bearer {self._token}",
            "User-Agent": _USER_AGENT,
        }
        params = {"q": f"{title} {artist}"}
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)

        try:
            async with aiohttp.ClientSession(
                timeout=timeout, headers=headers
            ) as session:
                # Step 1: Search for the song
                async with session.get(
                    f"{_GENIUS_API_URL}/search", params=params
                ) as resp:
                    if resp.status != 200:
                        log.debug(
                            "Genius: search returned status %d for %r by %r",
                            resp.status,
                            title,
                            artist,
                        )
                        return None
                    data = await resp.json()

                hits = data.get("response", {}).get("hits", [])
                if not hits:
                    log.debug("Genius: no hits for %r by %r", title, artist)
                    return None

                # Take the first hit
                song = hits[0].get("result", {})
                song_url = song.get("url")
                if not song_url:
                    log.debug("Genius: no URL in first hit for %r by %r", title, artist)
                    return None

                # Step 2: Fetch the lyrics page and extract text
                async with session.get(song_url) as page_resp:
                    if page_resp.status != 200:
                        log.debug(
                            "Genius: lyrics page returned status %d for %r by %r",
                            page_resp.status,
                            title,
                            artist,
                        )
                        return None
                    html = await page_resp.text()

        except TimeoutError:
            log.debug("Genius: timeout fetching %r by %r", title, artist)
            return None
        except aiohttp.ClientError as exc:
            log.debug("Genius: client error for %r by %r: %s", title, artist, exc)
            return None
        except Exception as exc:
            log.debug("Genius: unexpected error for %r by %r: %s", title, artist, exc)
            return None

        # Extract lyrics from the HTML page
        return _extract_lyrics_from_html(html)


def _extract_lyrics_from_html(html: str) -> str | None:
    """Extract plain text lyrics from a Genius lyrics page.

    Tries multiple extraction strategies in order:
    1. Modern Genius layout: <div class="lyrics">...</div>
    2. Data attribute: data-lyrics="..."
    3. Lyrics container divs with data-lyrics-container attribute

    Returns cleaned plain text or None.
    """
    # Strategy 1: Classic <div class="lyrics"> container
    match = re.search(
        r'<div class="lyrics">(.*?)</div>',
        html,
        re.DOTALL,
    )
    if match:
        return _clean_html_to_text(match.group(1))

    # Strategy 2: data-lyrics attribute
    match = re.search(
        r'data-lyrics="(.*?)"',
        html,
        re.DOTALL,
    )
    if match:
        text = match.group(1)
        text = text.replace("\\n", "\n").replace("<br>", "\n").strip()
        return text if text else None

    # Strategy 3: Modern Genius uses data-lyrics-container divs
    containers = re.findall(
        r'<div[^>]*data-lyrics-container="true"[^>]*>(.*?)</div>',
        html,
        re.DOTALL,
    )
    if containers:
        combined = "\n".join(containers)
        return _clean_html_to_text(combined)

    log.debug("Genius: could not extract lyrics from HTML")
    return None


def _clean_html_to_text(html_fragment: str) -> str | None:
    """Strip HTML tags and decode entities from a lyrics HTML fragment."""
    # Replace <br> and <br/> with newlines before stripping tags
    text = re.sub(r"<br\s*/?>", "\n", html_fragment)
    # Strip all remaining HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode common HTML entities
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#x27;", "'")
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    # Normalize whitespace: collapse multiple blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    return text if text else None
