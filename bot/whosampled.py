"""HelloDJ — WhoSampled sample-lookup integration.

Looks up a track on whosampled.com and extracts the samples it uses (the
"Sampled in" / "Samples" data). WhoSampled is Cloudflare-fronted, so this
module:

  * fetches with a browser-like User-Agent,
  * parses the HTML with the stdlib ``html.parser`` (no bs4/lxml dependency),
  * degrades gracefully when the site returns a 403 / challenge page, returning
    a structured result with ``blocked=True`` so the caller can explain it.

The public search endpoint is ``https://www.whosampled.com/search/?q=<query>``.
Track pages live at ``https://www.whosampled.com/<artist>/<album>/<track>/``.
Sample data on a track page appears in the "Samples" section as links of the
form ``/artist/album/track/`` under a heading, plus "Sampled in" (tracks that
sampled this one).
"""

import logging
import re
from html.parser import HTMLParser
from urllib.parse import quote_plus, urljoin

import aiohttp

log = logging.getLogger(__name__)

BASE = "https://www.whosampled.com"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# A WhoSampled track/artist/album link looks like /artist/album/track/
_LINK_RE = re.compile(r"^/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/?$")


class _Result:
    """Structured outcome of a WhoSampled lookup."""

    def __init__(self):
        self.query: str = ""
        self.track_url: str | None = None
        self.track_title: str | None = None
        self.artist: str | None = None
        self.album: str | None = None
        # Samples used BY this track: list of {title, artist, album, url}
        self.samples: list[dict] = []
        # Tracks that sampled this one ("Sampled in"): same shape.
        self.sampled_in: list[dict] = []
        self.blocked: bool = False
        self.error: str | None = None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


class _WSParser(HTMLParser):
    """Extract track links and section headings from a WhoSampled page.

    WhoSampled marks sample sections with headings (``<h2>``/``<h3>``) such as
    "Samples" and "Sampled in". We track the current heading and collect the
    track links that follow it until the next heading.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._heading: str | None = None
        self._in_heading = False
        self._heading_buf: list[str] = []
        self._in_link = False
        self._link_href: str | None = None
        self._link_text: list[str] = []
        self.sections: dict[str, list[dict]] = {}
        self.title: str | None = None
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag in ("h2", "h3"):
            self._in_heading = True
            self._heading_buf = []
        elif tag == "a":
            href = attrs.get("href")
            if href and _LINK_RE.match(href):
                self._in_link = True
                self._link_href = href
                self._link_text = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in ("h2", "h3"):
            self._in_heading = False
            heading = _clean("".join(self._heading_buf)).lower()
            self._heading = heading or None
        elif tag == "a" and self._in_link:
            self._in_link = False
            href = self._link_href
            text = _clean("".join(self._link_text))
            self._link_href = None
            if href and text:
                m = _LINK_RE.match(href)
                if m:
                    artist, album, track = m.group(1), m.group(2), m.group(3)
                    entry = {
                        "title": text,
                        "artist": artist.replace("-", " ").title(),
                        "album": album.replace("-", " ").title(),
                        "url": urljoin(BASE, href),
                    }
                    key = self._heading or "general"
                    self.sections.setdefault(key, []).append(entry)

    def handle_data(self, data):
        if self._in_title and self.title is None:
            self.title = _clean(data)
        if self._in_heading:
            self._heading_buf.append(data)
        if self._in_link:
            self._link_text.append(data)


def _dedupe(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        key = it.get("url") or (it.get("title", "") + it.get("artist", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _pick_section(parser: _WSParser, *keywords: str) -> list[dict]:
    """Return the first section whose heading contains any keyword."""
    for heading, items in parser.sections.items():
        if any(k in heading for k in keywords):
            return _dedupe(items)
    return []


async def _get(session: aiohttp.ClientSession, url: str) -> tuple[int, str]:
    async with session.get(url, headers=_HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as resp:
        text = await resp.text(errors="replace")
        return resp.status, text


def _looks_blocked(status: int, html: str) -> bool:
    if status in (403, 503):
        return True
    low = html[:4000].lower()
    return ("just a moment" in low) or ("cf-challenge" in low) or ("challenge-platform" in low)


async def search(query: str) -> _Result:
    """Search WhoSampled for ``query`` and return the best track's sample data.

    Returns a :class:`_Result`. When the site is unreachable or Cloudflare
    blocks the request, ``result.blocked`` is True and ``result.error`` explains
    why — the caller should surface a friendly message rather than crash.
    """
    result = _Result()
    result.query = query
    url = f"{BASE}/search/?q={quote_plus(query)}"

    try:
        async with aiohttp.ClientSession() as session:
            status, html = await _get(session, url)
    except Exception as exc:
        result.error = f"Could not reach WhoSampled: {exc}"
        result.blocked = True
        return result

    if _looks_blocked(status, html):
        result.blocked = True
        result.error = (
            "WhoSampled is behind a Cloudflare challenge and blocked the request "
            f"(HTTP {status}). Try again later or look it up manually at "
            f"https://www.whosampled.com/search/?q={quote_plus(query)}"
        )
        return result

    # Find the first track link on the search results page.
    track_href: str | None = None
    for m in re.finditer(r'href="(/[^"]+)"', html):
        href = m.group(1)
        if _LINK_RE.match(href):
            track_href = href
            break

    if not track_href:
        result.error = f"No track found on WhoSampled for “{query}”."
        return result

    track_url = urljoin(BASE, track_href)
    result.track_url = track_url
    m = _LINK_RE.match(track_href)
    if m:
        result.artist = m.group(1).replace("-", " ").title()
        result.album = m.group(2).replace("-", " ").title()

    # Fetch the track page for the sample data.
    try:
        async with aiohttp.ClientSession() as session:
            tstatus, thtml = await _get(session, track_url)
    except Exception as exc:
        result.error = f"Found the track but could not load its page: {exc}"
        return result

    if _looks_blocked(tstatus, thtml):
        result.blocked = True
        result.error = (
            "WhoSampled blocked the track-page request (Cloudflare challenge). "
            f"Track page: {track_url}"
        )
        return result

    parser = _WSParser()
    try:
        parser.feed(thtml)
        parser.close()
    except Exception as exc:
        log.warning("whosampled: HTML parse error: %s", exc)

    result.track_title = parser.title or (m.group(3).replace("-", " ").title() if m else None)
    result.samples = _pick_section(parser, "sampled in", "samples used", "uses")
    # "Samples" (what this track samples) vs "Sampled in" (what samples it).
    # WhoSampled headings: "Samples" = tracks this one samples; "Sampled in" =
    # tracks that sample this one. Map both, preferring exact headings.
    samples_used = _pick_section(parser, "samples")
    if samples_used:
        result.samples = samples_used
    result.sampled_in = _pick_section(parser, "sampled in")

    return result


def to_embed_fields(result: _Result) -> tuple[str, list[tuple[str, str]]]:
    """Convert a result into (description, fields) for a Discord embed.

    Returns a description string and a list of (name, value) field tuples.
    """
    fields: list[tuple[str, str]] = []
    if result.artist:
        fields.append(("Artist", result.artist))
    if result.album:
        fields.append(("Album", result.album))

    def _fmt(items: list[dict], limit: int = 8) -> str:
        if not items:
            return "None listed."
        lines = []
        for it in items[:limit]:
            artist = it.get("artist") or ""
            lines.append(f"• **{it.get('title', 'Unknown')}**" + (f" — {artist}" if artist else ""))
        if len(items) > limit:
            lines.append(f"…and {len(items) - limit} more")
        return "\n".join(lines)

    if result.samples:
        fields.append(("Samples used in this track", _fmt(result.samples)))
    if result.sampled_in:
        fields.append(("Sampled in (tracks that use this)", _fmt(result.sampled_in)))

    if not result.samples and not result.sampled_in:
        fields.append(("Samples", "No sample data could be extracted for this track."))

    description = ""
    if result.track_title:
        description = f"**{result.track_title}**"
    return description, fields
