"""Tests for projectM preset category management.

Covers:
- Category listing (ProjectMEngine.get_available_categories)
- Invalid category handling (_resolve_preset_path fallback)
- Category autocomplete suggestions
- preset_category config validation
- Cog list-categories command and _get_projectm_categories helper

Requirements: Req 17 (AC 1-5)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure bot/ is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

os.environ.setdefault("HELLODJ_DB_KEY", "test-key-for-projectm-category-tests")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_interaction(guild_id: int = 123) -> MagicMock:
    """Create a mock discord.Interaction."""
    interaction = MagicMock()
    interaction.guild = MagicMock()
    interaction.guild.id = guild_id
    interaction.guild_id = guild_id
    interaction.user = MagicMock()
    interaction.user.voice = None
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.namespace = MagicMock()
    return interaction


def _make_bot() -> MagicMock:
    """Create a mock bot instance."""
    bot = MagicMock()
    bot.get_cog.return_value = None
    return bot


def _populate_preset_dir(base: Path) -> None:
    """Create a realistic preset directory structure for testing."""
    # Category with .milk presets
    abstract = base / "Abstract"
    abstract.mkdir()
    (abstract / "fractal1.milk").touch()
    (abstract / "fractal2.milk").touch()
    (abstract / "swirl.milk").touch()

    # Category with .prjm presets
    space = base / "Space"
    space.mkdir()
    (space / "nebula.prjm").touch()
    (space / "stars.prjm").touch()

    # Mixed category
    geometric = base / "Geometric"
    geometric.mkdir()
    (geometric / "triangle.milk").touch()
    (geometric / "circle.prjm").touch()
    (geometric / "readme.txt").touch()  # Non-preset file (should be excluded)

    # Empty directory (should be excluded from results)
    empty = base / "Empty"
    empty.mkdir()

    # Single preset category
    simple = base / "Simple"
    simple.mkdir()
    (simple / "basic.milk").touch()


# ---------------------------------------------------------------------------
# Tests — ProjectMEngine.get_available_categories (Req 17 AC 1, 4)
# ---------------------------------------------------------------------------


class TestGetAvailableCategories:
    """Validate that get_available_categories scans directories correctly."""

    def test_returns_categories_with_preset_counts(self, tmp_path):
        """Categories with presets are returned with accurate counts."""
        _populate_preset_dir(tmp_path)

        with patch("video.visualizer_engines.projectm.PRESET_DIR", str(tmp_path)):
            from video.visualizer_engines.projectm import ProjectMEngine

            categories = ProjectMEngine.get_available_categories()

        assert "Abstract" in categories
        assert categories["Abstract"] == 3  # 3 .milk files
        assert "Space" in categories
        assert categories["Space"] == 2  # 2 .prjm files
        assert "Geometric" in categories
        assert categories["Geometric"] == 2  # 1 .milk + 1 .prjm (excludes .txt)
        assert "Simple" in categories
        assert categories["Simple"] == 1

    def test_excludes_empty_directories(self, tmp_path):
        """Directories with no preset files are excluded."""
        _populate_preset_dir(tmp_path)

        with patch("video.visualizer_engines.projectm.PRESET_DIR", str(tmp_path)):
            from video.visualizer_engines.projectm import ProjectMEngine

            categories = ProjectMEngine.get_available_categories()

        assert "Empty" not in categories

    def test_nonexistent_base_directory_returns_empty(self):
        """When preset directory doesn't exist, returns empty dict."""
        with patch(
            "video.visualizer_engines.projectm.PRESET_DIR",
            "/nonexistent/path/that/should/not/exist",
        ):
            from video.visualizer_engines.projectm import ProjectMEngine

            categories = ProjectMEngine.get_available_categories()

        assert categories == {}

    def test_excludes_non_preset_files(self, tmp_path):
        """Only .milk and .prjm files are counted."""
        cat = tmp_path / "MixedFiles"
        cat.mkdir()
        (cat / "preset.milk").touch()
        (cat / "notes.txt").touch()
        (cat / "config.json").touch()
        (cat / "image.png").touch()

        with patch("video.visualizer_engines.projectm.PRESET_DIR", str(tmp_path)):
            from video.visualizer_engines.projectm import ProjectMEngine

            categories = ProjectMEngine.get_available_categories()

        assert categories == {"MixedFiles": 1}

    def test_case_insensitive_extensions(self, tmp_path):
        """File extensions are matched case-insensitively (.MILK, .Prjm)."""
        cat = tmp_path / "CaseTest"
        cat.mkdir()
        (cat / "upper.MILK").touch()
        (cat / "mixed.Prjm").touch()
        (cat / "lower.milk").touch()

        with patch("video.visualizer_engines.projectm.PRESET_DIR", str(tmp_path)):
            from video.visualizer_engines.projectm import ProjectMEngine

            categories = ProjectMEngine.get_available_categories()

        assert categories == {"CaseTest": 3}

    def test_results_are_sorted_by_name(self, tmp_path):
        """Categories are returned in sorted order."""
        for name in ["Zebra", "Alpha", "Middle"]:
            d = tmp_path / name
            d.mkdir()
            (d / "p.milk").touch()

        with patch("video.visualizer_engines.projectm.PRESET_DIR", str(tmp_path)):
            from video.visualizer_engines.projectm import ProjectMEngine

            categories = ProjectMEngine.get_available_categories()

        keys = list(categories.keys())
        assert keys == ["Alpha", "Middle", "Zebra"]


# ---------------------------------------------------------------------------
# Tests — _resolve_preset_path (Req 17 AC 2, 3, 5)
# ---------------------------------------------------------------------------


class TestResolvePresetPath:
    """Validate category path resolution with fallback behavior."""

    def test_all_returns_base_directory(self, tmp_path):
        """preset_category='all' returns the base preset directory."""
        from video.visualizer_engines.projectm import ProjectMEngine

        with patch("video.visualizer_engines.projectm.PRESET_DIR", str(tmp_path)):
            engine = ProjectMEngine(preset_category="all")
            result = engine._resolve_preset_path()

        assert result == str(tmp_path)

    def test_valid_category_returns_subdirectory(self, tmp_path):
        """Existing category name resolves to its subdirectory."""
        from video.visualizer_engines.projectm import ProjectMEngine

        category = tmp_path / "Abstract"
        category.mkdir()

        with patch("video.visualizer_engines.projectm.PRESET_DIR", str(tmp_path)):
            engine = ProjectMEngine(preset_category="Abstract")
            result = engine._resolve_preset_path()

        assert result == str(category)

    def test_invalid_category_falls_back_to_base(self, tmp_path):
        """Non-existent category falls back to base directory."""
        from video.visualizer_engines.projectm import ProjectMEngine

        with patch("video.visualizer_engines.projectm.PRESET_DIR", str(tmp_path)):
            engine = ProjectMEngine(preset_category="NonExistent")
            result = engine._resolve_preset_path()

        assert result == str(tmp_path)

    def test_category_change_takes_effect_on_next_call(self, tmp_path):
        """Changing _preset_category is reflected in next _resolve_preset_path call."""
        from video.visualizer_engines.projectm import ProjectMEngine

        (tmp_path / "Geometric").mkdir()
        (tmp_path / "Space").mkdir()

        with patch("video.visualizer_engines.projectm.PRESET_DIR", str(tmp_path)):
            engine = ProjectMEngine(preset_category="Geometric")
            assert engine._resolve_preset_path() == str(tmp_path / "Geometric")

            # Simulate category change (config update)
            engine._preset_category = "Space"
            assert engine._resolve_preset_path() == str(tmp_path / "Space")

            # Change to all
            engine._preset_category = "all"
            assert engine._resolve_preset_path() == str(tmp_path)


# ---------------------------------------------------------------------------
# Tests — Cog _get_projectm_categories helper (Req 17 AC 4)
# ---------------------------------------------------------------------------


class TestCogGetProjectMCategories:
    """Validate the cog's module-level _get_projectm_categories function."""

    def test_returns_categories_from_directory(self, tmp_path):
        """Reads preset directory and returns name→count mapping."""
        cat = tmp_path / "Trippy"
        cat.mkdir()
        (cat / "a.milk").touch()
        (cat / "b.milk").touch()

        with patch("cogs.visualizer.PROJECTM_PRESET_DIR", tmp_path):
            from cogs.visualizer import _get_projectm_categories

            result = _get_projectm_categories()

        assert result == {"Trippy": 2}

    def test_missing_directory_returns_empty(self):
        """If preset directory doesn't exist, returns empty dict."""
        with patch("cogs.visualizer.PROJECTM_PRESET_DIR", Path("/no/such/path")):
            from cogs.visualizer import _get_projectm_categories

            result = _get_projectm_categories()

        assert result == {}

    def test_excludes_directories_with_no_milk_files(self, tmp_path):
        """Directories without .milk files are excluded."""
        cat = tmp_path / "TextOnly"
        cat.mkdir()
        (cat / "notes.txt").touch()

        with patch("cogs.visualizer.PROJECTM_PRESET_DIR", tmp_path):
            from cogs.visualizer import _get_projectm_categories

            result = _get_projectm_categories()

        assert result == {}


# ---------------------------------------------------------------------------
# Tests — Category autocomplete (Req 17 AC 5)
# ---------------------------------------------------------------------------


class TestCategoryAutocomplete:
    """Validate category autocomplete returns correct suggestions."""

    @pytest.mark.asyncio
    async def test_includes_all_option(self):
        """Autocomplete always includes 'all' as the first choice."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer._get_projectm_categories") as mock_cats:
            mock_cats.return_value = {"Abstract": 10, "Space": 5}
            result = await cog._category_autocomplete(interaction, "")

        values = [c.value for c in result]
        assert values[0] == "all"

    @pytest.mark.asyncio
    async def test_includes_available_categories(self):
        """Autocomplete lists all available category names."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer._get_projectm_categories") as mock_cats:
            mock_cats.return_value = {"Abstract": 42, "Geometric": 18, "Space": 7}
            result = await cog._category_autocomplete(interaction, "")

        values = [c.value for c in result]
        assert "Abstract" in values
        assert "Geometric" in values
        assert "Space" in values

    @pytest.mark.asyncio
    async def test_filters_by_partial_input(self):
        """Autocomplete filters categories by partial text match."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer._get_projectm_categories") as mock_cats:
            mock_cats.return_value = {
                "Abstract": 42,
                "Geometric": 18,
                "Space": 7,
                "Simple": 12,
            }
            result = await cog._category_autocomplete(interaction, "sp")

        values = [c.value for c in result]
        assert "Space" in values
        # "all" is always present (it doesn't filter "all" by partial match in the impl)
        # But "Abstract", "Geometric", "Simple" should not be in the filtered results
        # since "sp" is only in "Space"
        assert "Geometric" not in values
        assert "Abstract" not in values

    @pytest.mark.asyncio
    async def test_shows_preset_counts_in_name(self):
        """Autocomplete shows preset count in the display name."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer._get_projectm_categories") as mock_cats:
            mock_cats.return_value = {"Abstract": 42}
            result = await cog._category_autocomplete(interaction, "abs")

        # Find the Abstract choice
        abstract_choice = next(c for c in result if c.value == "Abstract")
        assert "42 presets" in abstract_choice.name


# ---------------------------------------------------------------------------
# Tests — Invalid category in config command (Req 17 AC 5)
# ---------------------------------------------------------------------------


class TestInvalidCategoryConfig:
    """Validate that invalid category in config is handled properly."""

    @pytest.mark.asyncio
    async def test_invalid_category_still_stores(self):
        """preset_category is a free-form string; validation is at engine level.

        The config schema accepts any string for preset_category. The engine's
        _resolve_preset_path handles non-existent categories by falling back
        to the base directory. This is by design — categories may be added later.
        """
        from video.visualizer_engines.config_schema import validate_config_value

        # Any string should be accepted for preset_category
        result = validate_config_value("projectm", "preset_category", "NonExistent")
        assert result == "NonExistent"

    @pytest.mark.asyncio
    async def test_all_is_valid_category_value(self):
        """'all' is always a valid preset_category value."""
        from video.visualizer_engines.config_schema import validate_config_value

        result = validate_config_value("projectm", "preset_category", "all")
        assert result == "all"


# ---------------------------------------------------------------------------
# Tests — list-categories command (Req 17 AC 4)
# ---------------------------------------------------------------------------


class TestListCategoriesCommand:
    """Validate /visualizer projectm list-categories command output."""

    @pytest.mark.asyncio
    async def test_displays_embed_with_categories(self):
        """Categories found → embed with fields per category and total footer."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer._get_projectm_categories") as mock_cats:
            mock_cats.return_value = {
                "Abstract": 50,
                "Geometric": 20,
                "Simple": 10,
            }
            await cog.projectm_list_categories.callback(cog, interaction)

        interaction.response.send_message.assert_awaited_once()
        call_kwargs = interaction.response.send_message.await_args[1]
        embed = call_kwargs["embed"]

        # Title contains "Categories"
        assert "Categories" in embed.title

        # Fields for each category
        assert len(embed.fields) == 3
        field_names = [f.name for f in embed.fields]
        assert "Abstract" in field_names
        assert "Geometric" in field_names
        assert "Simple" in field_names

        # Footer shows total count
        assert "80 presets" in embed.footer.text
        assert "3 categories" in embed.footer.text

    @pytest.mark.asyncio
    async def test_no_categories_shows_error(self):
        """No categories available → error message, not an embed."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer._get_projectm_categories") as mock_cats:
            mock_cats.return_value = {}
            await cog.projectm_list_categories.callback(cog, interaction)

        interaction.response.send_message.assert_awaited_once()
        msg = interaction.response.send_message.await_args[0][0]
        assert "No projectM preset categories found" in msg

    @pytest.mark.asyncio
    async def test_embed_is_ephemeral(self):
        """list-categories response is ephemeral."""
        from cogs.visualizer import VisualizerCog

        cog = VisualizerCog.__new__(VisualizerCog)
        cog.bot = _make_bot()

        interaction = _make_interaction()

        with patch("cogs.visualizer._get_projectm_categories") as mock_cats:
            mock_cats.return_value = {"Abstract": 10}
            await cog.projectm_list_categories.callback(cog, interaction)

        call_kwargs = interaction.response.send_message.await_args[1]
        assert call_kwargs["ephemeral"] is True
