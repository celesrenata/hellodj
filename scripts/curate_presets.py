#!/usr/bin/env python3
"""Curate high-quality community Milkdrop presets from presets-cream-of-the-crop.

This is a build-time tool — it is NOT shipped in the Docker image.

Usage:
    python scripts/curate_presets.py --source /path/to/cream-of-the-crop --output bot/data/presets/projectm/
    python scripts/curate_presets.py --output bot/data/presets/projectm/  # auto-clones repo

Options:
    --source        Path to local clone of presets-cream-of-the-crop (clones if omitted)
    --output        Output directory for curated presets (required)
    --max-presets   Maximum number of presets to select (default: 350)
    --min-presets   Minimum number of presets required (default: 200)
    --max-size-mb   Maximum total size in MB (default: 50)
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REPO_URL = "https://github.com/projectM-visualizer/presets-cream-of-the-crop.git"

AUDIO_REACTIVE_VARS = {"bass", "mid", "treb", "bass_att", "mid_att", "treb_att"}

RECOGNIZED_AUTHORS = {
    "geiss", "flexi", "rovastar", "zylot", "eo.s.", "martin", "cope",
    "idiot24-7", "shifter", "phat", "krash", "unchained",
}

# Category mapping: source folder pattern → target category
CATEGORY_PATTERNS: list[tuple[list[str], str]] = [
    (["abstract", "organic", "blob"], "Abstract"),
    (["classic", "geiss", "milkdrop"], "Classic"),
    (["energy", "beat", "intense"], "Energy"),
    (["fluid", "water", "flow"], "Fluid Motion"),
    (["geometric", "fractal", "math"], "Geometric"),
    (["space", "cosmic", "star"], "Space"),
    (["trippy", "psychedelic", "kaleid"], "Trippy"),
]

ALL_CATEGORIES = ["Abstract", "Classic", "Energy", "Fluid Motion", "Geometric", "Space", "Trippy", "Simple"]

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class PresetInfo:
    """Metadata extracted from a single .milk preset file."""

    path: Path
    content: str = ""
    line_count: int = 0
    file_size: int = 0
    author: str = ""
    has_per_pixel: bool = False
    has_warp_shader: bool = False
    has_comp_shader: bool = False
    has_audio_reactive: bool = False
    is_stub: bool = False
    score: float = 0.0
    category: str = "Simple"
    source_folder: str = ""
    per_frame_eq_count: int = 0
    has_polar_refs: bool = False
    has_motion_trails: bool = False
    audio_vars_found: set = field(default_factory=set)


# ---------------------------------------------------------------------------
# Git clone
# ---------------------------------------------------------------------------


def clone_repo(dest: Path) -> Path:
    """Clone presets-cream-of-the-crop into dest. Returns the clone path."""
    clone_path = dest / "presets-cream-of-the-crop"
    if clone_path.exists():
        print(f"  Using existing clone at {clone_path}")
        return clone_path

    print(f"  Cloning {REPO_URL} ...")
    subprocess.run(
        ["git", "clone", "--depth=1", REPO_URL, str(clone_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    print(f"  Cloned to {clone_path}")
    return clone_path


# ---------------------------------------------------------------------------
# Preset discovery
# ---------------------------------------------------------------------------


def discover_presets(source_dir: Path) -> list[Path]:
    """Recursively find all .milk files in source_dir."""
    presets = sorted(source_dir.rglob("*.milk"))
    return presets


# ---------------------------------------------------------------------------
# Author extraction
# ---------------------------------------------------------------------------


def extract_author(filepath: Path) -> str:
    """Extract author from filename convention 'Author - Preset Name.milk'."""
    name = filepath.stem
    if " - " in name:
        author = name.split(" - ", 1)[0].strip()
        return author
    return ""


def is_recognized_author(author: str) -> bool:
    """Check if author is in the recognized set (case-insensitive)."""
    return author.lower() in RECOGNIZED_AUTHORS


# ---------------------------------------------------------------------------
# Category mapping
# ---------------------------------------------------------------------------


def determine_category(preset_path: Path, source_root: Path) -> str:
    """Map preset to category based on its source folder path."""
    try:
        rel = preset_path.relative_to(source_root)
    except ValueError:
        return "Simple"

    # Get all folder names in the relative path (lowercased)
    folder_parts = [p.lower() for p in rel.parts[:-1]]  # exclude filename
    folder_str = " ".join(folder_parts)

    for patterns, category in CATEGORY_PATTERNS:
        for pattern in patterns:
            if pattern in folder_str:
                return category

    # Also check filename for category hints
    name_lower = preset_path.stem.lower()
    for patterns, category in CATEGORY_PATTERNS:
        for pattern in patterns:
            if pattern in name_lower:
                return category

    return "Simple"


# ---------------------------------------------------------------------------
# Quality analysis
# ---------------------------------------------------------------------------


def analyze_preset(preset_path: Path, source_root: Path) -> PresetInfo:
    """Analyze a preset file and extract quality metadata."""
    info = PresetInfo(path=preset_path)
    info.file_size = preset_path.stat().st_size

    try:
        content = preset_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        info.is_stub = True
        return info

    info.content = content
    info.line_count = content.count("\n") + 1

    content_lower = content.lower()

    # Audio-reactive variable check
    for var in AUDIO_REACTIVE_VARS:
        # Match as whole words or as part of equations (e.g., bass_att, mid=)
        if re.search(rf"\b{re.escape(var)}\b", content_lower):
            info.audio_vars_found.add(var)
    info.has_audio_reactive = len(info.audio_vars_found) > 0

    # Structural checks
    info.has_per_pixel = "per_pixel" in content_lower
    info.has_warp_shader = bool(re.search(r"warp_\d", content_lower))
    info.has_comp_shader = bool(re.search(r"comp_\d", content_lower))

    # Stub rejection: <80 lines AND no per_pixel AND no warp/comp
    if (
        info.line_count < 80
        and not info.has_per_pixel
        and not info.has_warp_shader
        and not info.has_comp_shader
    ):
        info.is_stub = True

    # Per-frame equation count
    info.per_frame_eq_count = len(re.findall(r"per_frame_\d+\s*=", content_lower))

    # Polar coordinate references in per_pixel
    if info.has_per_pixel:
        # Look for rad/ang references within the per_pixel section
        info.has_polar_refs = bool(re.search(r"\b(rad|ang)\b", content_lower))

    # Motion trails: fDecay < 0.99
    decay_match = re.search(r"fdecay\s*=\s*([\d.]+)", content_lower)
    if decay_match:
        try:
            decay_val = float(decay_match.group(1))
            info.has_motion_trails = decay_val < 0.99
        except ValueError:
            pass

    # Author extraction
    info.author = extract_author(preset_path)

    # Category
    info.category = determine_category(preset_path, source_root)
    info.source_folder = str(preset_path.parent.relative_to(source_root)) if source_root in preset_path.parents else ""

    return info


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_preset(info: PresetInfo) -> float:
    """Score a preset based on quality criteria. Higher = better."""
    score = 0.0

    # per_pixel section (complex per-pixel transforms)
    if info.has_per_pixel:
        score += 3.0

    # Warp shader
    if info.has_warp_shader:
        score += 3.0

    # Composite shader
    if info.has_comp_shader:
        score += 3.0

    # Motion trails (fDecay < 0.99)
    if info.has_motion_trails:
        score += 1.5

    # More than 5 per_frame equations
    if info.per_frame_eq_count > 5:
        score += 2.0

    # Polar coordinate refs (rad/ang) in per_pixel
    if info.has_polar_refs:
        score += 1.5

    # Audio-reactive variable diversity
    score += len(info.audio_vars_found) * 0.5

    # Recognized author bonus
    if is_recognized_author(info.author):
        score += 1.0

    # Length bonus (longer presets tend to be more complex)
    if info.line_count > 200:
        score += 1.0
    elif info.line_count > 100:
        score += 0.5

    return score


# ---------------------------------------------------------------------------
# Selection logic
# ---------------------------------------------------------------------------


def select_presets(
    presets: list[PresetInfo],
    max_presets: int,
    min_presets: int,
    max_size_bytes: int,
) -> list[PresetInfo]:
    """Select the best presets subject to constraints."""

    # Filter: must be audio-reactive and not a stub
    candidates = [p for p in presets if p.has_audio_reactive and not p.is_stub]

    if len(candidates) < min_presets:
        print(f"  WARNING: Only {len(candidates)} candidates pass quality filter (need {min_presets})")
        print("  Relaxing audio-reactive filter to meet minimum...")
        # Relax: include non-stub presets even without explicit audio vars
        candidates = [p for p in presets if not p.is_stub]

    # Score all candidates
    for p in candidates:
        p.score = score_preset(p)

    # Sort by score descending
    candidates.sort(key=lambda p: p.score, reverse=True)

    # Select top presets, respecting size limit and category balance
    selected: list[PresetInfo] = []
    total_size = 0
    category_counts: dict[str, int] = {cat: 0 for cat in ALL_CATEGORIES}

    # First pass: ensure minimum per category (at least 10 per category)
    min_per_category = 10
    for category in ALL_CATEGORIES:
        cat_presets = [p for p in candidates if p.category == category]
        for p in cat_presets[:min_per_category]:
            if p not in selected and total_size + p.file_size <= max_size_bytes:
                selected.append(p)
                total_size += p.file_size
                category_counts[category] += 1

    # Second pass: fill remaining slots by score
    for p in candidates:
        if len(selected) >= max_presets:
            break
        if total_size + p.file_size > max_size_bytes:
            continue
        if p in selected:
            continue
        selected.append(p)
        total_size += p.file_size
        category_counts[p.category] += 1

    # Verify author diversity
    authors_found = {p.author.lower() for p in selected if p.author}
    recognized_found = authors_found & RECOGNIZED_AUTHORS
    if len(recognized_found) < 5:
        print(f"  WARNING: Only {len(recognized_found)} recognized authors found: {recognized_found}")
        print("  Attempting to boost author diversity...")
        # Try to add presets from underrepresented recognized authors
        needed_authors = RECOGNIZED_AUTHORS - recognized_found
        for author in needed_authors:
            if len(recognized_found) >= 5:
                break
            author_presets = [
                p for p in candidates
                if p.author.lower() == author and p not in selected
            ]
            for p in author_presets[:3]:  # up to 3 per missing author
                if len(selected) < max_presets and total_size + p.file_size <= max_size_bytes:
                    selected.append(p)
                    total_size += p.file_size
                    category_counts[p.category] += 1
                    recognized_found.add(author)
                    break

    return selected


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def copy_presets(selected: list[PresetInfo], output_dir: Path) -> None:
    """Copy selected presets UNMODIFIED to output directory organized by category."""
    # Ensure category directories exist
    for category in ALL_CATEGORIES:
        (output_dir / category).mkdir(parents=True, exist_ok=True)

    for preset in selected:
        dest_dir = output_dir / preset.category
        dest_file = dest_dir / preset.path.name

        # Handle name collisions by appending a number
        if dest_file.exists():
            stem = preset.path.stem
            suffix = preset.path.suffix
            counter = 1
            while dest_file.exists():
                dest_file = dest_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        shutil.copy2(preset.path, dest_file)


def print_summary(selected: list[PresetInfo], output_dir: Path) -> None:
    """Print summary report."""
    total_size = sum(p.file_size for p in selected)

    # Category breakdown
    category_counts: dict[str, int] = {cat: 0 for cat in ALL_CATEGORIES}
    for p in selected:
        category_counts[p.category] += 1

    # Author breakdown
    author_counts: dict[str, int] = {}
    for p in selected:
        author = p.author if p.author else "(unknown)"
        author_counts[author] = author_counts.get(author, 0) + 1

    # Recognized authors
    recognized = {
        p.author for p in selected
        if p.author and is_recognized_author(p.author)
    }

    print("\n" + "=" * 60)
    print("PRESET CURATION SUMMARY")
    print("=" * 60)
    print(f"\nTotal presets selected: {len(selected)}")
    print(f"Total size: {total_size / (1024 * 1024):.2f} MB")
    print(f"Output directory: {output_dir}")

    print(f"\nCategory breakdown:")
    for category in ALL_CATEGORIES:
        count = category_counts[category]
        print(f"  {category:<15} {count:>4} presets")

    print(f"\nRecognized authors ({len(recognized)}):")
    for author in sorted(recognized):
        count = author_counts.get(author, 0)
        print(f"  {author:<15} {count:>4} presets")

    # Top authors overall
    top_authors = sorted(author_counts.items(), key=lambda x: x[1], reverse=True)[:15]
    print(f"\nTop 15 authors:")
    for author, count in top_authors:
        marker = " *" if is_recognized_author(author) else ""
        print(f"  {author:<25} {count:>4} presets{marker}")

    # Quality stats
    with_per_pixel = sum(1 for p in selected if p.has_per_pixel)
    with_shaders = sum(1 for p in selected if p.has_warp_shader or p.has_comp_shader)
    with_advanced = sum(1 for p in selected if p.has_per_pixel or p.has_warp_shader or p.has_comp_shader)
    avg_score = sum(p.score for p in selected) / len(selected) if selected else 0

    print(f"\nQuality metrics:")
    print(f"  With per_pixel:     {with_per_pixel:>4} ({100 * with_per_pixel / len(selected):.1f}%)")
    print(f"  With warp/comp:     {with_shaders:>4} ({100 * with_shaders / len(selected):.1f}%)")
    print(f"  Advanced (any):     {with_advanced:>4} ({100 * with_advanced / len(selected):.1f}%)")
    print(f"  Average score:      {avg_score:.2f}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_output(selected: list[PresetInfo], min_presets: int, max_size_bytes: int) -> bool:
    """Validate selection meets requirements. Returns True if valid."""
    total_size = sum(p.file_size for p in selected)
    errors = []

    if len(selected) < min_presets:
        errors.append(f"Only {len(selected)} presets selected (need ≥{min_presets})")

    if total_size > max_size_bytes:
        errors.append(f"Total size {total_size / (1024*1024):.1f}MB exceeds {max_size_bytes / (1024*1024):.0f}MB limit")

    # Check category diversity: at least 5 categories with ≥10 presets
    category_counts: dict[str, int] = {}
    for p in selected:
        category_counts[p.category] = category_counts.get(p.category, 0) + 1
    cats_with_10 = sum(1 for c in category_counts.values() if c >= 10)
    if cats_with_10 < 5:
        errors.append(f"Only {cats_with_10} categories have ≥10 presets (need ≥5)")

    # Check author diversity
    recognized = {
        p.author.lower() for p in selected
        if p.author and is_recognized_author(p.author)
    }
    if len(recognized) < 5:
        errors.append(f"Only {len(recognized)} recognized authors (need ≥5): {recognized}")

    if errors:
        print("\nVALIDATION ERRORS:")
        for err in errors:
            print(f"  ✗ {err}")
        return False

    print("\n✓ All validation checks passed")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Curate high-quality community Milkdrop presets from presets-cream-of-the-crop.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Path to local clone of presets-cream-of-the-crop (auto-clones if omitted)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output directory for curated presets",
    )
    parser.add_argument(
        "--max-presets",
        type=int,
        default=350,
        help="Maximum number of presets to select (default: 350)",
    )
    parser.add_argument(
        "--min-presets",
        type=int,
        default=200,
        help="Minimum number of presets required (default: 200)",
    )
    parser.add_argument(
        "--max-size-mb",
        type=float,
        default=50.0,
        help="Maximum total size in MB (default: 50)",
    )

    args = parser.parse_args()
    max_size_bytes = int(args.max_size_mb * 1024 * 1024)

    print("=" * 60)
    print("projectM Preset Curation Tool")
    print("=" * 60)

    # Resolve source directory
    tmp_dir = None
    if args.source:
        source_dir = args.source
        if not source_dir.is_dir():
            print(f"ERROR: Source directory not found: {source_dir}")
            return 1
    else:
        tmp_dir = Path(tempfile.mkdtemp(prefix="preset_curation_"))
        print(f"\nNo --source provided, cloning repo...")
        try:
            source_dir = clone_repo(tmp_dir)
        except subprocess.CalledProcessError as e:
            print(f"ERROR: Failed to clone repository: {e}")
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            return 1

    try:
        # Discover presets
        print(f"\nDiscovering .milk files in {source_dir}...")
        all_presets = discover_presets(source_dir)
        print(f"  Found {len(all_presets)} .milk files")

        if not all_presets:
            print("ERROR: No .milk files found in source directory")
            return 1

        # Analyze presets
        print(f"\nAnalyzing presets...")
        analyzed: list[PresetInfo] = []
        for i, preset_path in enumerate(all_presets):
            if (i + 1) % 1000 == 0:
                print(f"  Analyzed {i + 1}/{len(all_presets)}...")
            info = analyze_preset(preset_path, source_dir)
            analyzed.append(info)

        # Stats on raw analysis
        stubs = sum(1 for p in analyzed if p.is_stub)
        audio_reactive = sum(1 for p in analyzed if p.has_audio_reactive)
        print(f"  Stubs (rejected): {stubs}")
        print(f"  Audio-reactive: {audio_reactive}")
        print(f"  Candidates: {len(analyzed) - stubs}")

        # Select presets
        print(f"\nSelecting top presets (max={args.max_presets}, min={args.min_presets})...")
        selected = select_presets(analyzed, args.max_presets, args.min_presets, max_size_bytes)
        print(f"  Selected {len(selected)} presets")

        # Validate
        valid = validate_output(selected, args.min_presets, max_size_bytes)

        # Copy to output
        print(f"\nCopying presets to {args.output}...")
        args.output.mkdir(parents=True, exist_ok=True)
        copy_presets(selected, args.output)
        print(f"  Done!")

        # Summary
        print_summary(selected, args.output)

        return 0 if valid else 1

    finally:
        if tmp_dir and tmp_dir.exists():
            print(f"\nCleaning up temporary directory {tmp_dir}...")
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
