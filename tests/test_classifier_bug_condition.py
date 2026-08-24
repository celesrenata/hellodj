"""Bug condition exploration test for classifier.py.

Property 1: Bug Condition — YouTube Hostname Fallthrough & Rule 10 VIDEO Default

This test encodes the EXPECTED behavior after the fix:
- YouTube-domain URLs (including youtube-nocookie.com and arbitrary subdomains)
  should classify as AUDIO (not VIDEO)
- Unrecognized URLs without video extensions should classify as AUDIO (not VIDEO)

On UNFIXED code, these tests MUST FAIL — failure confirms the bug exists.
After the fix is applied, these tests should PASS.

**Validates: Requirements 1.3, 1.4, 1.5, 2.3, 2.5**
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from playback.classifier import ContentType, ClassificationResult, classify


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Generate arbitrary lowercase subdomains for youtube.com
_youtube_subdomains = st.from_regex(r"[a-z]{2,12}", fullmatch=True)

# Safe path segments that won't accidentally produce video extensions
_safe_path_segments = st.from_regex(r"[a-z0-9_\-]{1,15}", fullmatch=True)
_safe_paths = st.lists(_safe_path_segments, min_size=1, max_size=3).map(
    lambda parts: "/" + "/".join(parts)
)

# Domains that are NOT recognized audio platforms or YouTube
_unrecognized_domains = st.from_regex(
    r"[a-z]{3,10}\.(org|net|io|dev|xyz|co|fm)", fullmatch=True
).filter(
    lambda d: d not in (
        "youtube.com", "youtu.be", "soundcloud.com", "tidal.com", "spotify.com",
    )
    and not d.startswith("music.")
    and not d.startswith("open.")
    and not d.startswith("www.")
    and not d.startswith("m.")
    and not d.startswith("listen.")
)


# ---------------------------------------------------------------------------
# Concrete failing cases (bug demonstrations)
# ---------------------------------------------------------------------------


class TestBugConditionConcreteCases:
    """Concrete URLs that demonstrate the classifier bug.

    Each of these currently returns VIDEO but should return AUDIO.
    """

    def test_youtube_nocookie_embed(self) -> None:
        """youtube-nocookie.com not in Rule 9 set → falls to Rule 10 VIDEO."""
        result = classify("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ")
        assert result.content_type == ContentType.AUDIO, (
            f"Expected AUDIO, got {result}"
        )

    def test_youtube_nocookie_bare(self) -> None:
        """youtube-nocookie.com (no www) not in Rule 9 set."""
        result = classify("https://youtube-nocookie.com/watch?v=abc123")
        assert result.content_type == ContentType.AUDIO, (
            f"Expected AUDIO, got {result}"
        )

    def test_gaming_youtube_subdomain(self) -> None:
        """gaming.youtube.com subdomain falls through Rule 9."""
        result = classify("https://gaming.youtube.com/watch?v=abc123")
        assert result.content_type == ContentType.AUDIO, (
            f"Expected AUDIO, got {result}"
        )

    def test_consent_youtube_subdomain(self) -> None:
        """consent.youtube.com subdomain falls through Rule 9."""
        result = classify("https://consent.youtube.com/redirect?q=something")
        assert result.content_type == ContentType.AUDIO, (
            f"Expected AUDIO, got {result}"
        )

    def test_unrecognized_url_podcast(self) -> None:
        """Unrecognized URL (no video ext) defaults to VIDEO instead of AUDIO."""
        result = classify("https://example.com/podcast/episode-5")
        assert result.content_type == ContentType.AUDIO, (
            f"Expected AUDIO, got {result}"
        )

    def test_unrecognized_url_podcast_fm(self) -> None:
        """Unrecognized .fm URL defaults to VIDEO instead of AUDIO."""
        result = classify("https://somepodcast.fm/episode/123")
        assert result.content_type == ContentType.AUDIO, (
            f"Expected AUDIO, got {result}"
        )


# ---------------------------------------------------------------------------
# Property-based: YouTube subdomains fall through Rule 9
# ---------------------------------------------------------------------------


class TestBugConditionYouTubeSubdomains:
    """For any YouTube-domain URL with an arbitrary subdomain (not in the
    current Rule 9 hostname set), classify() should return AUDIO.

    On unfixed code, these fall through to Rule 10 and return VIDEO.

    **Validates: Requirements 1.3, 2.3**
    """

    @settings(max_examples=100)
    @given(
        subdomain=_youtube_subdomains,
        path=_safe_paths,
    )
    def test_arbitrary_subdomain_youtube_com(self, subdomain: str, path: str) -> None:
        """<subdomain>.youtube.com URLs should classify as AUDIO."""
        # Skip subdomains that ARE in the current Rule 9 set (www, m)
        # and music.youtube.com which is handled by Rule 3
        if subdomain in ("www", "m", "music"):
            return

        url = f"https://{subdomain}.youtube.com{path}"
        result = classify(url)
        assert result.content_type == ContentType.AUDIO, (
            f"classify({url!r}) returned {result}, expected AUDIO"
        )


# ---------------------------------------------------------------------------
# Property-based: Unrecognized URLs default to VIDEO (should be AUDIO)
# ---------------------------------------------------------------------------


class TestBugConditionUnrecognizedUrlDefault:
    """For any URL with an unrecognized domain and no video file extension,
    classify() should return AUDIO with confidence "default".

    On unfixed code, Rule 10 returns VIDEO for these.

    **Validates: Requirements 1.5, 2.5**
    """

    @settings(max_examples=100)
    @given(
        domain=_unrecognized_domains,
        path=_safe_paths,
    )
    def test_unrecognized_url_no_video_ext_returns_audio(
        self, domain: str, path: str
    ) -> None:
        """Unrecognized domain + no video extension → should be AUDIO (default)."""
        url = f"https://{domain}{path}"

        # Ensure path doesn't accidentally end with a video extension
        lower_path = path.lower()
        dot_idx = lower_path.rfind(".")
        if dot_idx != -1:
            ext = lower_path[dot_idx + 1:]
            if ext in {"mp4", "webm", "mkv", "avi", "mov", "m4v"}:
                return  # Skip — video extension URLs should be VIDEO

        result = classify(url)
        assert result.content_type == ContentType.AUDIO, (
            f"classify({url!r}) returned {result}, expected AUDIO (default)"
        )
        assert result.confidence == "default", (
            f"classify({url!r}) confidence={result.confidence}, expected 'default'"
        )
