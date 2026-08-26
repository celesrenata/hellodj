"""WCAG AA color-contrast checks for the web-ui OKLCH palette (R14.4).

The web-ui applies the dark-glassmorphism palette defined in
``static/css/app.css`` as a Tailwind v4 ``@theme`` block of OKLCH custom
properties. These tests parse that palette straight from the CSS, convert each
color OKLCH → sRGB → WCAG relative luminance, and assert the text- and
UI-color combinations clear the WCAG AA contrast ratios:

* normal text on its surface       >= 4.5:1  (WCAG 1.4.3)
* large / non-essential text        >= 3.0:1  (WCAG 1.4.3 large)
* UI / non-text graphical elements  >= 3.0:1  (WCAG 1.4.11)

NOTE: This automates only the *measurable contrast-ratio* portion of WCAG AA.
Full WCAG AA conformance additionally requires manual assistive-technology
review (screen readers, keyboard-only navigation, focus order, text resize,
and human judgement about which text is "large" or "non-essential"). Treat a
passing run here as necessary-but-not-sufficient for AA.

Requirements: 14.4
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from oklch_contrast import (
    AA_LARGE_TEXT,
    AA_NORMAL_TEXT,
    AA_UI,
    contrast_over,
    parse_oklch,
)

# static/css/app.css lives one directory up from tests/.
_CSS_PATH = Path(__file__).resolve().parents[1] / "static" / "css" / "app.css"

# Matches lines like:  --color-text-primary: oklch(0.96 0.01 280);
_THEME_VAR_RE = re.compile(
    r"--color-([a-z0-9-]+)\s*:\s*(oklch\([^;]*\))\s*;",
    re.IGNORECASE,
)


def _load_palette() -> dict[str, str]:
    """Parse ``--color-*: oklch(...)`` declarations from app.css."""
    css = _CSS_PATH.read_text(encoding="utf-8")
    palette: dict[str, str] = {}
    for name, value in _THEME_VAR_RE.findall(css):
        palette[name] = value
    return palette


PALETTE = _load_palette()


def _color(name: str):
    """Return the parsed OKLCH color for a palette key, failing clearly."""
    if name not in PALETTE:
        pytest.fail(f"palette is missing --color-{name} in {_CSS_PATH.name}")
    return parse_oklch(PALETTE[name])


def test_palette_parsed_from_css() -> None:
    """Sanity: the OKLCH palette was found and parsed from app.css."""
    assert _CSS_PATH.is_file()
    # Core keys the design system relies on must be present.
    for key in (
        "surface-0",
        "text-primary",
        "text-secondary",
        "text-muted",
        "brand",
        "danger",
        "info",
    ):
        assert key in PALETTE, f"missing --color-{key}"


# --------------------------------------------------------------------------- #
# Text contrast on the base surface (WCAG 1.4.3)
# --------------------------------------------------------------------------- #


def test_text_primary_on_surface0_meets_aa_normal() -> None:
    ratio = contrast_over(_color("text-primary"), _color("surface-0"))
    assert ratio >= AA_NORMAL_TEXT, f"text-primary/surface-0 only {ratio:.2f}:1"


def test_text_secondary_on_surface0_meets_aa_normal() -> None:
    ratio = contrast_over(_color("text-secondary"), _color("surface-0"))
    assert ratio >= AA_NORMAL_TEXT, (
        f"text-secondary/surface-0 only {ratio:.2f}:1"
    )


def test_text_muted_on_surface0_meets_aa_large() -> None:
    # text-muted is reserved for large / non-essential labels (per app.css),
    # so it is held to the 3:1 large-text threshold, not 4.5:1.
    ratio = contrast_over(_color("text-muted"), _color("surface-0"))
    assert ratio >= AA_LARGE_TEXT, f"text-muted/surface-0 only {ratio:.2f}:1"


# Body text is placed on elevated surfaces too (cards, inputs). Verify the
# primary/secondary tones still clear AA on the deepest elevated surface.
@pytest.mark.parametrize("surface", ["surface-0", "surface-1", "surface-2", "surface-3"])
def test_primary_text_on_all_surfaces_meets_aa_normal(surface: str) -> None:
    ratio = contrast_over(_color("text-primary"), _color(surface))
    assert ratio >= AA_NORMAL_TEXT, (
        f"text-primary/{surface} only {ratio:.2f}:1"
    )


# --------------------------------------------------------------------------- #
# UI / non-text contrast (WCAG 1.4.11) — brand and semantic accents
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ui_color", ["brand", "danger", "info", "success", "warning"])
def test_ui_accents_meet_aa_non_text(ui_color: str) -> None:
    ratio = contrast_over(_color(ui_color), _color("surface-0"))
    assert ratio >= AA_UI, f"{ui_color}/surface-0 only {ratio:.2f}:1 (< 3:1)"


def test_primary_button_text_meets_aa_normal() -> None:
    # .btn-primary uses near-black text (oklch(0.14 0.02 280)) on the brand fill.
    button_text = parse_oklch("oklch(0.14 0.02 280)")
    ratio = contrast_over(button_text, _color("brand"))
    assert ratio >= AA_NORMAL_TEXT, f"btn-primary text only {ratio:.2f}:1"
