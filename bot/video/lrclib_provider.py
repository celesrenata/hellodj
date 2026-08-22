"""LRCLIB.net API client and LRC format parser.

Fetches time-synced or plain lyrics from LRCLIB.net for the synced lyrics
overlay system. Implements LRC line-level timestamp parsing (word-level
parsing added in Phase 2).

Requirements: 1.1, 1.2, 1.3, 8.1, 8.2, 8.3, 8.4, 8.5
"""

from __future__ import annotations

import logging
import re

import aiohttp

from video.lyrics_models import TimedLine, TimedLyrics, TimedWord

log = logging.getLogger(__name__)

_USER_AGENT = "HelloDJ/1.0 (https://hellodj.celestium.life)"
_LRCLIB_BASE = "https://lrclib.net/api/get"
_TIMEOUT_S = 5

# Line-level LRC timestamp: [mm:ss.xx] or [mm:ss.xxx]
_LRC_LINE_RE = re.compile(r"\[(\d{2}):(\d{2})\.(\d{2,3})\](.*)")

# Word-level LRC timestamp: <mm:ss.xx>word (inline within a line)
_LRC_WORD_RE = re.compile(r"<(\d{2}):(\d{2})\.(\d{2,3})>(\S+)")


def parse_lrc(lrc_text: str) -> list[TimedLine]:
    """Parse an LRC-formatted string into a list of TimedLine objects.

    Handles both 2-digit centisecond (xx) and 3-digit millisecond (xxx)
    timestamp formats:
      - [01:23.45] → 1*60000 + 23*1000 + 45*10 = 83450 ms
      - [01:23.456] → 1*60000 + 23*1000 + 456 = 83456 ms

    Also detects word-level timestamps within a line:
      - [00:12.34]<00:12.34>Hello <00:12.80>world
      - Creates TimedWord objects for each word
      - Cleans display text by removing <mm:ss.xx> tags

    Lines that don't match the LRC pattern are silently skipped.
    """
    lines: list[TimedLine] = []
    for raw_line in lrc_text.strip().split("\n"):
        match = _LRC_LINE_RE.match(raw_line.strip())
        if not match:
            continue

        mm, ss, frac_str = match.group(1), match.group(2), match.group(3)
        frac = int(frac_str)
        # 2-digit → centiseconds (multiply by 10), 3-digit → milliseconds
        ms = (int(mm) * 60 + int(ss)) * 1000 + (frac * 10 if len(frac_str) == 2 else frac)
        text = match.group(4).strip()

        # Check for word-level timestamps: <mm:ss.xx>word
        words = None
        word_matches = _LRC_WORD_RE.findall(text)
        if word_matches:
            words = []
            for w_mm, w_ss, w_frac_str, word_text in word_matches:
                w_frac = int(w_frac_str)
                w_ms = (int(w_mm) * 60 + int(w_ss)) * 1000 + (
                    w_frac * 10 if len(w_frac_str) == 2 else w_frac
                )
                words.append(TimedWord(time_ms=w_ms, text=word_text))
            # Clean display text by removing word-timestamp tags
            text = re.sub(r"<\d{2}:\d{2}\.\d{2,3}>", "", text).strip()

        lines.append(TimedLine(time_ms=ms, text=text, words=words))

    return lines


class LRCLIBProvider:
    """LRCLIB.net API client for fetching time-synced lyrics.

    Usage:
        provider = LRCLIBProvider()
        result = await provider.fetch("Artist", "Title", 225.0)

    Returns:
        - TimedLyrics when syncedLyrics is present (parsed LRC)
        - str when only plainLyrics is present (caller handles timing)
        - None for 404, instrumental, or errors
    """

    async def fetch(
        self, artist: str, title: str, duration_s: float
    ) -> TimedLyrics | str | None:
        """Fetch lyrics from LRCLIB.net for a given track.

        Args:
            artist: The track artist name.
            title: The track title.
            duration_s: Track duration in seconds.

        Returns:
            TimedLyrics if synced lyrics are available,
            str (plain text) if only plain lyrics are available,
            None if not found, instrumental, or on error.
        """
        params = {
            "artist_name": artist,
            "track_name": title,
            "duration": str(int(duration_s)),
        }
        headers = {"User-Agent": _USER_AGENT}
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT_S)

        try:
            async with aiohttp.ClientSession(
                timeout=timeout, headers=headers
            ) as session:
                async with session.get(_LRCLIB_BASE, params=params) as resp:
                    if resp.status == 404:
                        log.debug(
                            "LRCLIB: no match for %r by %r", title, artist
                        )
                        return None

                    if resp.status != 200:
                        log.debug(
                            "LRCLIB: unexpected status %d for %r by %r",
                            resp.status,
                            title,
                            artist,
                        )
                        return None

                    data = await resp.json()

        except TimeoutError:
            log.debug("LRCLIB: timeout fetching %r by %r", title, artist)
            return None
        except aiohttp.ClientError as exc:
            log.debug("LRCLIB: client error for %r by %r: %s", title, artist, exc)
            return None
        except Exception as exc:
            log.debug("LRCLIB: unexpected error for %r by %r: %s", title, artist, exc)
            return None

        # Instrumental tracks — no lyrics to show
        if data.get("instrumental"):
            log.debug("LRCLIB: instrumental track %r by %r", title, artist)
            return None

        # Prefer synced lyrics (LRC format with timestamps)
        synced_text = data.get("syncedLyrics")
        if synced_text:
            lines = parse_lrc(synced_text)
            if lines:
                track_id = f"{artist.lower().strip()}:{title.lower().strip()}"
                # Detect word-level data: if ANY line has words, use lrc_word
                has_words = any(line.words is not None for line in lines)
                sync_type = "lrc_word" if has_words else "lrc_synced"
                return TimedLyrics(
                    track_id=track_id,
                    sync_type=sync_type,
                    duration_s=duration_s,
                    lines=lines,
                )

        # Fall back to plain lyrics (caller will handle timing)
        plain_text = data.get("plainLyrics")
        if plain_text:
            log.debug(
                "LRCLIB: plain lyrics only for %r by %r, delegating timing",
                title,
                artist,
            )
            return plain_text

        log.debug("LRCLIB: no usable lyrics in response for %r by %r", title, artist)
        return None
