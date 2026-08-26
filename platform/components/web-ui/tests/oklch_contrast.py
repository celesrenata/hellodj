"""OKLCH → sRGB → WCAG relative-luminance / contrast helpers.

This is a small, dependency-free implementation used to verify that the
web-ui OKLCH palette (defined in ``static/css/app.css``) meets WCAG AA
color-contrast criteria for text and UI elements (Requirement 14.4).

The full WCAG AA success criteria (1.4.3 text contrast, 1.4.11 non-text
contrast) also require manual review with assistive technologies and human
judgement about which text is "large", which elements are essential, etc.
This module only automates the measurable *contrast-ratio* portion — it is a
gate, not a substitute for that manual assistive-technology review.

Conversion pipeline (per the CSS Color 4 / Oklab specification):

    OKLCH  -> OKLab  -> linear sRGB  -> gamma-encoded sRGB
    linear sRGB channels -> WCAG relative luminance -> contrast ratio

References (paraphrased; content rephrased for licensing compliance):
* CSS Color Module Level 4 — Oklab/OKLCH color space and conversion matrices.
* WCAG 2.1 — relative luminance and contrast-ratio definitions (1.4.3 / 1.4.11).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

__all__ = [
    "Oklch",
    "parse_oklch",
    "oklch_to_linear_srgb",
    "relative_luminance",
    "contrast_ratio",
    "contrast_over",
    "AA_NORMAL_TEXT",
    "AA_LARGE_TEXT",
    "AA_UI",
]

# WCAG AA thresholds.
AA_NORMAL_TEXT = 4.5  # 1.4.3 normal-size text
AA_LARGE_TEXT = 3.0  # 1.4.3 large text (>=18.66px bold / >=24px)
AA_UI = 3.0  # 1.4.11 non-text (UI components / graphical objects)


@dataclass(frozen=True)
class Oklch:
    """A color in the OKLCH space with an optional alpha (0..1)."""

    lightness: float  # L, 0..1
    chroma: float  # C, >= 0
    hue: float  # H, degrees
    alpha: float = 1.0


_OKLCH_RE = re.compile(
    r"oklch\(\s*"
    r"([0-9]*\.?[0-9]+%?)\s+"  # L (fraction or percent)
    r"([0-9]*\.?[0-9]+)\s+"  # C
    r"([0-9]*\.?[0-9]+)"  # H
    r"(?:\s*/\s*([0-9]*\.?[0-9]+%?))?"  # optional / alpha
    r"\s*\)",
    re.IGNORECASE,
)


def _num(token: str) -> float:
    """Parse a numeric token that may be a percentage."""
    token = token.strip()
    if token.endswith("%"):
        return float(token[:-1]) / 100.0
    return float(token)


def parse_oklch(value: str) -> Oklch:
    """Parse a CSS ``oklch(L C H[ / A])`` string into an :class:`Oklch`.

    ``L`` may be given as a 0..1 fraction (as in the palette) or a percentage.
    Raises ``ValueError`` if the string is not a valid oklch() color.
    """
    match = _OKLCH_RE.search(value)
    if not match:
        raise ValueError(f"not an oklch() color: {value!r}")
    lightness = _num(match.group(1))
    chroma = float(match.group(2))
    hue = float(match.group(3))
    alpha = _num(match.group(4)) if match.group(4) is not None else 1.0
    return Oklch(lightness=lightness, chroma=chroma, hue=hue, alpha=alpha)


def oklch_to_linear_srgb(color: Oklch) -> tuple[float, float, float]:
    """Convert an OKLCH color to linear-light sRGB (channels clamped to 0..1)."""
    # OKLCH -> OKLab
    hue_rad = math.radians(color.hue)
    a = color.chroma * math.cos(hue_rad)
    b = color.chroma * math.sin(hue_rad)
    lightness = color.lightness

    # OKLab -> LMS (cube of the intermediate), per CSS Color 4.
    l_ = lightness + 0.3963377774 * a + 0.2158037573 * b
    m_ = lightness - 0.1055613458 * a - 0.0638541728 * b
    s_ = lightness - 0.0894841775 * a - 1.2914855480 * b

    lms_l = l_ ** 3
    lms_m = m_ ** 3
    lms_s = s_ ** 3

    # LMS -> linear sRGB.
    red = 4.0767416621 * lms_l - 3.3077115913 * lms_m + 0.2309699292 * lms_s
    green = -1.2684380046 * lms_l + 2.6097574011 * lms_m - 0.3413193965 * lms_s
    blue = -0.0041960863 * lms_l - 0.7034186147 * lms_m + 1.7076147010 * lms_s

    return (_clamp01(red), _clamp01(green), _clamp01(blue))


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def relative_luminance(linear_rgb: tuple[float, float, float]) -> float:
    """WCAG relative luminance from linear-light sRGB channels."""
    red, green, blue = linear_rgb
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _composite_over(
    fg: Oklch, bg: Oklch
) -> tuple[float, float, float]:
    """Alpha-composite ``fg`` over opaque ``bg`` in linear sRGB."""
    fr, fg_, fb = oklch_to_linear_srgb(fg)
    br, bg_, bb = oklch_to_linear_srgb(bg)
    alpha = _clamp01(fg.alpha)
    return (
        fr * alpha + br * (1.0 - alpha),
        fg_ * alpha + bg_ * (1.0 - alpha),
        fb * alpha + bb * (1.0 - alpha),
    )


def contrast_ratio(lum_a: float, lum_b: float) -> float:
    """WCAG contrast ratio between two relative luminances."""
    lighter = max(lum_a, lum_b)
    darker = min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


def contrast_over(foreground: Oklch, background: Oklch) -> float:
    """Contrast ratio of ``foreground`` (possibly translucent) over ``background``.

    The background is treated as opaque (its alpha is ignored) since the palette
    surfaces are opaque base layers.
    """
    fg_linear = _composite_over(foreground, background)
    bg_linear = oklch_to_linear_srgb(
        Oklch(background.lightness, background.chroma, background.hue, 1.0)
    )
    return contrast_ratio(
        relative_luminance(fg_linear), relative_luminance(bg_linear)
    )
