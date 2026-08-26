"""Unit tests for the per-guild user ban list."""

from __future__ import annotations

from playback_orchestrator.user_bans import UserBans

GUILD = 222
USER = 555


def test_ban_and_check() -> None:
    bans = UserBans()
    assert bans.ban_user(GUILD, USER, banned_by=1) is True
    assert bans.is_banned(GUILD, USER) is True


def test_ban_is_idempotent() -> None:
    bans = UserBans()
    assert bans.ban_user(GUILD, USER, banned_by=1) is True
    assert bans.ban_user(GUILD, USER, banned_by=1) is False


def test_unban() -> None:
    bans = UserBans()
    bans.ban_user(GUILD, USER, banned_by=1)
    assert bans.unban_user(GUILD, USER) is True
    assert bans.is_banned(GUILD, USER) is False
    assert bans.unban_user(GUILD, USER) is False


def test_ban_isolated_per_guild() -> None:
    bans = UserBans()
    bans.ban_user(GUILD, USER, banned_by=1)
    assert bans.is_banned(999, USER) is False


def test_list_and_mapping_round_trip() -> None:
    bans = UserBans()
    bans.ban_user(GUILD, USER, banned_by=42)
    assert len(bans.list_bans(GUILD)) == 1
    restored = UserBans.from_mapping(bans.to_mapping())
    assert restored.is_banned(GUILD, USER) is True
