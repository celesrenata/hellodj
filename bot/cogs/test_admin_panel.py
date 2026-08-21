"""Tests for AdminPanel cog.

Validates Requirements 11.1–11.6 (command registration and structure)
and integration with ContentFilter (12.x) and UserBans (13.x).
"""

from __future__ import annotations

import asyncio
import sys
import os

# Ensure bot directory is on the path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import discord
from discord.ext import commands
from unittest.mock import AsyncMock, MagicMock, patch

from playback.content_filter import ContentFilter
from playback.user_bans import UserBans


@pytest.fixture
def bot():
    """Create a minimal bot instance for testing."""
    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix="!", intents=intents)
    bot._user = MagicMock()
    bot._user.display_name = "HelloDJ"
    return bot


@pytest.fixture
def content_filter(tmp_path):
    """Create a ContentFilter instance backed by a temp file."""
    return ContentFilter(data_path=str(tmp_path / "content_filters.json"))


@pytest.fixture
def user_bans(tmp_path):
    """Create a UserBans instance backed by a temp file."""
    return UserBans(data_path=str(tmp_path / "user_bans.json"))


@pytest.fixture
def cog(bot, content_filter, user_bans):
    """Create an AdminPanel cog instance."""
    from cogs.admin_panel import AdminPanel
    return AdminPanel(bot, content_filter=content_filter, user_bans=user_bans)


class TestCogStructure:
    """Validates Requirement 11.1: /hellodj command group exists with all subcommands."""

    def test_hellodj_group_exists(self, cog):
        assert cog.hellodj is not None
        assert cog.hellodj.name == "hellodj"

    def test_block_subgroup_exists(self, cog):
        assert cog.block is not None
        assert cog.block.name == "block"

    def test_command_tree_has_all_commands(self, cog):
        """Verify all required commands are registered."""
        command_names = {cmd.name for cmd in cog.hellodj.walk_commands()}
        expected = {
            "ping", "status", "settings",
            "block", "artist", "track", "domain", "keyword", "list",
            "unblock", "ban", "unban", "banlist", "instances",
        }
        assert expected.issubset(command_names), (
            f"Missing commands: {expected - command_names}"
        )

    def test_block_subcommands_exist(self, cog):
        """Verify block subgroup has all subcommands."""
        block_cmds = {cmd.name for cmd in cog.block.walk_commands()}
        expected = {"artist", "track", "domain", "keyword", "list"}
        assert expected.issubset(block_cmds)


class TestCogInit:
    """Validates cog initialization with optional dependencies."""

    def test_init_with_all_dependencies(self, bot, content_filter, user_bans):
        from cogs.admin_panel import AdminPanel
        cog = AdminPanel(bot, content_filter=content_filter, user_bans=user_bans)
        assert cog.content_filter is content_filter
        assert cog.user_bans is user_bans

    def test_init_with_no_dependencies(self, bot):
        from cogs.admin_panel import AdminPanel
        cog = AdminPanel(bot)
        assert cog.content_filter is None
        assert cog.user_bans is None


class TestCogLoad:
    """Validates cog can be loaded via setup()."""

    @pytest.mark.asyncio
    async def test_setup_adds_cog(self, bot):
        from cogs.admin_panel import setup
        await setup(bot)
        cog = bot.get_cog("AdminPanel")
        assert cog is not None

    @pytest.mark.asyncio
    async def test_setup_initializes_dependencies(self, bot):
        from cogs.admin_panel import setup
        await setup(bot)
        cog = bot.get_cog("AdminPanel")
        # ContentFilter and UserBans should be initialized
        assert cog.content_filter is not None
        assert cog.user_bans is not None
