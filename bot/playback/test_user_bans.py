"""Tests for UserBans module.

Validates Requirements 13.1–13.5:
- ban_user: prevents user from playback commands (13.1)
- is_banned: checks ban status for enforcement (13.2)
- unban_user: restores user access (13.3)
- list_bans: shows all banned users (13.4)
- Persistence across restarts (13.5)
"""

from __future__ import annotations

import os

import pytest

from playback.user_bans import UserBans


@pytest.fixture
def tmp_data_path(tmp_path):
    """Provide a temporary path for the bans JSON file."""
    return str(tmp_path / "user_bans.json")


@pytest.fixture
def user_bans(tmp_data_path):
    """Create a UserBans instance backed by a temp file."""
    return UserBans(data_path=tmp_data_path)


class TestBanUser:
    @pytest.mark.asyncio
    async def test_ban_new_user(self, user_bans):
        result = await user_bans.ban_user(guild_id=123, user_id=456, banned_by=789)
        assert result is True

    @pytest.mark.asyncio
    async def test_ban_already_banned_user(self, user_bans):
        await user_bans.ban_user(123, 456, 789)
        result = await user_bans.ban_user(123, 456, 789)
        assert result is False

    @pytest.mark.asyncio
    async def test_ban_stores_metadata(self, user_bans):
        await user_bans.ban_user(123, 456, 789)
        bans = user_bans.list_bans(123)
        assert len(bans) == 1
        assert bans[0]["user_id"] == 456
        assert bans[0]["banned_by"] == 789
        assert "banned_at" in bans[0]

    @pytest.mark.asyncio
    async def test_ban_multiple_users_same_guild(self, user_bans):
        await user_bans.ban_user(123, 456, 789)
        await user_bans.ban_user(123, 111, 789)
        bans = user_bans.list_bans(123)
        assert len(bans) == 2

    @pytest.mark.asyncio
    async def test_ban_same_user_different_guilds(self, user_bans):
        await user_bans.ban_user(111, 456, 789)
        await user_bans.ban_user(222, 456, 789)
        assert len(user_bans.list_bans(111)) == 1
        assert len(user_bans.list_bans(222)) == 1


class TestUnbanUser:
    @pytest.mark.asyncio
    async def test_unban_existing_user(self, user_bans):
        await user_bans.ban_user(123, 456, 789)
        result = await user_bans.unban_user(123, 456)
        assert result is True
        assert user_bans.list_bans(123) == []

    @pytest.mark.asyncio
    async def test_unban_nonexistent_user(self, user_bans):
        result = await user_bans.unban_user(123, 456)
        assert result is False

    @pytest.mark.asyncio
    async def test_unban_from_nonexistent_guild(self, user_bans):
        result = await user_bans.unban_user(999, 456)
        assert result is False

    @pytest.mark.asyncio
    async def test_unban_only_target_user(self, user_bans):
        await user_bans.ban_user(123, 456, 789)
        await user_bans.ban_user(123, 111, 789)
        await user_bans.unban_user(123, 456)
        bans = user_bans.list_bans(123)
        assert len(bans) == 1
        assert bans[0]["user_id"] == 111


class TestIsBanned:
    @pytest.mark.asyncio
    async def test_banned_user_returns_true(self, user_bans):
        await user_bans.ban_user(123, 456, 789)
        assert user_bans.is_banned(123, 456) is True

    def test_not_banned_user_returns_false(self, user_bans):
        assert user_bans.is_banned(123, 456) is False

    @pytest.mark.asyncio
    async def test_banned_in_different_guild_returns_false(self, user_bans):
        await user_bans.ban_user(111, 456, 789)
        assert user_bans.is_banned(222, 456) is False

    @pytest.mark.asyncio
    async def test_unbanned_user_returns_false(self, user_bans):
        await user_bans.ban_user(123, 456, 789)
        await user_bans.unban_user(123, 456)
        assert user_bans.is_banned(123, 456) is False

    def test_empty_guild_returns_false(self, user_bans):
        assert user_bans.is_banned(999, 123) is False


class TestListBans:
    def test_list_empty_guild(self, user_bans):
        assert user_bans.list_bans(999) == []

    @pytest.mark.asyncio
    async def test_list_returns_all_bans(self, user_bans):
        await user_bans.ban_user(123, 456, 789)
        await user_bans.ban_user(123, 111, 789)
        bans = user_bans.list_bans(123)
        assert len(bans) == 2
        user_ids = {b["user_id"] for b in bans}
        assert user_ids == {456, 111}


class TestPersistence:
    @pytest.mark.asyncio
    async def test_data_persists_to_file(self, tmp_data_path):
        ub = UserBans(data_path=tmp_data_path)
        await ub.ban_user(123, 456, 789)

        # Create new instance from same file
        ub2 = UserBans(data_path=tmp_data_path)
        assert ub2.is_banned(123, 456) is True

    @pytest.mark.asyncio
    async def test_unban_persists(self, tmp_data_path):
        ub = UserBans(data_path=tmp_data_path)
        await ub.ban_user(123, 456, 789)
        await ub.unban_user(123, 456)

        ub2 = UserBans(data_path=tmp_data_path)
        assert ub2.is_banned(123, 456) is False

    def test_handles_corrupt_file(self, tmp_data_path):
        os.makedirs(os.path.dirname(tmp_data_path), exist_ok=True)
        with open(tmp_data_path, "w") as f:
            f.write("not valid json {{{")

        ub = UserBans(data_path=tmp_data_path)
        assert ub.list_bans(123) == []

    def test_handles_missing_file(self, tmp_data_path):
        ub = UserBans(data_path=tmp_data_path)
        assert ub.list_bans(123) == []


class TestBanMetadata:
    @pytest.mark.asyncio
    async def test_ban_has_timestamp(self, user_bans):
        await user_bans.ban_user(123, 456, 789)
        bans = user_bans.list_bans(123)
        assert "banned_at" in bans[0]
        # Should be a valid ISO timestamp
        from datetime import datetime
        dt = datetime.fromisoformat(bans[0]["banned_at"])
        assert dt.tzinfo is not None  # Should be timezone-aware
