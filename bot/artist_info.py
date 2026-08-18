"""HelloDJ — Artist info lookup for /whosat (and /whosthis alias).

Fetches a quick artist biography / trivia using free, no-key APIs:
  * MusicBrainz (https://musicbrainz.org/ws/2/) for the artist MBID + a
    short bio/trivia blurb (the ``life-span``/``area``/``begin`` fields),
  * Wikipedia (https://en.wikipedia.org/api/rest_v1/) for a longer intro.

Both are async (aiohttp) and degrade gracefully — when an artist cannot be
found or the APIs are unreachable, the module returns a structured result with
``error`` set so the cog can surface a friendly message instead of crashing.

MusicBrainz rate-limits aggressively (1 req/s). We send exactly one lookup and
never hammer it; the Wikipedia fallback is a single extra request.
"""

import logging
from urllib.parse import quote_plus

import aiohttp

log = logging.getLogger(__name__)

MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2/artist"
WIKIPEDIA_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKIPEDIA_SEARCH_URL = "https://en.wikipedia.org/w/api.php"
_UA = "HelloDJBot/1.0 (https://github.com/celesrenata/hellodj; music lookup)"


class ArtistResult:
    """Structured outcome of an artist lookup."""

    def __init__(self):
        self.query: str = ""
        self.name: str | None = None
        self.mbid: str | None = None
        self.bio: str | None = None
        self.wikipedia_url: str | None = None
        self.error: str | None = None


def _clean(text: str, limit: int = 1500) -> str:
    text = (text or "").strip()
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


async def _musicbrainz_artist(query: str, session: aiohttp.ClientSession) -> dict | None:
    """Query MusicBrainz for an artist by name. Returns a raw artist dict or None."""
    params = {
        "query": f'"{query}"',
        "fmt": "json",
        "limit": "1",
    }
    headers = {"User-Agent": _UA}
    async with session.get(
        MUSICBRAINZ_URL,
        params=params,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
    artists = data.get("artists") or []
    return artists[0] if artists else None


async def _wikipedia_summary(title: str, session: aiohttp.ClientSession) -> dict | None:
    """Fetch the Wikipedia REST summary for a page title. Returns dict or None."""
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    url = WIKIPEDIA_SUMMARY_URL.format(title=quote_plus(title.replace(" ", "_")))
    async with session.get(
        url,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        if resp.status != 200:
            return None
        return await resp.json()


async def _wikipedia_search(query: str, session: aiohttp.ClientSession) -> str | None:
    """Find the best Wikipedia page title for an artist name (via the search API)."""
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srnamespace": "0",
        "format": "json",
        "srlimit": "1",
    }
    headers = {"User-Agent": _UA}
    async with session.get(
        WIKIPEDIA_SEARCH_URL,
        params=params,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
    hits = data.get("query", {}).get("search") or []
    return hits[0].get("title") if hits else None


async def lookup(query: str) -> ArtistResult:
    """Look up an artist and return bio/trivia. Never raises."""
    result = ArtistResult()
    result.query = query

    try:
        async with aiohttp.ClientSession() as session:
            mb = await _musicbrainz_artist(query, session)
            if mb:
                result.name = mb.get("name")
                result.mbid = mb.get("id")
                begin = mb.get("life-span", {}).get("begin") if isinstance(mb.get("life-span"), dict) else None
                area = mb.get("area", {}).get("name") if isinstance(mb.get("area"), dict) else None
                bio_parts = []
                if begin:
                    bio_parts.append(f"Formed / active since **{begin}**")
                if area:
                    bio_parts.append(f"Origin: **{area}**")
                if bio_parts:
                    result.bio = " • ".join(bio_parts)

            # Wikipedia for a longer intro + a clickable link.
            title = result.name or query
            summary = await _wikipedia_summary(title, session)
            if summary is None:
                found_title = await _wikipedia_search(title, session)
                if found_title:
                    summary = await _wikipedia_summary(found_title, session)
            if summary:
                extract = summary.get("extract") or summary.get("description")
                if extract:
                    result.bio = _clean(extract)
                content_url = summary.get("content_urls", {}).get("desktop", {})
                result.wikipedia_url = content_url.get("url")
                if not result.name:
                    result.name = summary.get("title")
    except Exception as exc:
        log.warning("artist lookup failed for %r: %s", query, exc)
        result.error = f"Artist lookup failed: {exc}"
        return result

    if not result.name and not result.bio and not result.wikipedia_url:
        result.error = f"No artist information could be found for “{query}”."
    return result
