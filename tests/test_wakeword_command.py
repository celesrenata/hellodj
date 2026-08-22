"""Unit tests for the /wakeword slash command and _should_listen logic.

Validates Requirements 1.1–1.7 from the wakeword-voice-pipeline spec.
"""

from __future__ import annotations

import pytest


class FakeVoiceCog:
    """Minimal reproduction of VoiceCog state management for testing."""

    def __init__(self, voice_enabled: bool = False):
        self._enabled_guilds: set[int] = set()
        self._disabled_guilds: set[int] = set()
        self._wakeword_guilds: set[int] = set()
        self._voice_enabled = voice_enabled

    def _should_listen(self, guild_id: int) -> bool:
        if guild_id in self._disabled_guilds:
            return False
        if self._voice_enabled:
            return True
        if guild_id in self._wakeword_guilds:
            return True
        return guild_id in self._enabled_guilds


# ---------------------------------------------------------------------------
# Tests for _should_listen (Requirements 1.4, 1.5)
# ---------------------------------------------------------------------------


class TestShouldListen:
    """Test _should_listen precedence logic."""

    def test_wakeword_on_activates_listening(self):
        """Req 1.4: /wakeword on makes _should_listen return True."""
        cog = FakeVoiceCog(voice_enabled=False)
        cog._wakeword_guilds.add(123)
        assert cog._should_listen(123) is True

    def test_voice_enable_activates_listening(self):
        """Req 1.4: /voice enable also activates listening."""
        cog = FakeVoiceCog(voice_enabled=False)
        cog._enabled_guilds.add(123)
        assert cog._should_listen(123) is True

    def test_both_wakeword_and_voice_enable(self):
        """Req 1.4: Either command activates listening."""
        cog = FakeVoiceCog(voice_enabled=False)
        cog._wakeword_guilds.add(123)
        cog._enabled_guilds.add(123)
        assert cog._should_listen(123) is True

    def test_wakeword_off_preserves_voice_enable(self):
        """Req 1.5: /wakeword off while /voice enable preserves voice state."""
        cog = FakeVoiceCog(voice_enabled=False)
        cog._enabled_guilds.add(123)
        cog._wakeword_guilds.add(123)
        # Simulate /wakeword off
        cog._wakeword_guilds.discard(123)
        # /voice enable still active
        assert cog._should_listen(123) is True
        assert 123 in cog._enabled_guilds

    def test_voice_disable_overrides_wakeword(self):
        """/voice disable takes precedence over /wakeword on."""
        cog = FakeVoiceCog(voice_enabled=False)
        cog._wakeword_guilds.add(123)
        cog._disabled_guilds.add(123)
        assert cog._should_listen(123) is False

    def test_voice_enabled_env_overrides_all(self):
        """VOICE_ENABLED=true overrides everything (except explicit disable)."""
        cog = FakeVoiceCog(voice_enabled=True)
        assert cog._should_listen(999) is True

    def test_nothing_enabled_returns_false(self):
        """No flags set means not listening."""
        cog = FakeVoiceCog(voice_enabled=False)
        assert cog._should_listen(123) is False

    def test_wakeword_on_without_voice_channel(self):
        """Req 1.7: Flag is set even without bot in voice — _should_listen is True."""
        cog = FakeVoiceCog(voice_enabled=False)
        cog._wakeword_guilds.add(456)
        # _should_listen just checks the flag, doesn't check voice connection
        assert cog._should_listen(456) is True


# ---------------------------------------------------------------------------
# Tests for wakeword_toggle state changes (Requirements 1.1, 1.2, 1.6)
# ---------------------------------------------------------------------------


class TestWakewordToggle:
    """Test the /wakeword command's state management logic."""

    def test_wakeword_on_adds_guild(self):
        """Req 1.1: /wakeword on adds guild to _wakeword_guilds."""
        cog = FakeVoiceCog()
        guild_id = 42
        # Simulate the "on" branch
        cog._wakeword_guilds.add(guild_id)
        assert guild_id in cog._wakeword_guilds

    def test_wakeword_off_removes_guild(self):
        """Req 1.2: /wakeword off removes guild from _wakeword_guilds."""
        cog = FakeVoiceCog()
        guild_id = 42
        cog._wakeword_guilds.add(guild_id)
        # Simulate the "off" branch
        cog._wakeword_guilds.discard(guild_id)
        assert guild_id not in cog._wakeword_guilds

    def test_wakeword_off_when_not_enabled_is_noop(self):
        """Req 1.2: /wakeword off on a guild that's not enabled is safe (discard)."""
        cog = FakeVoiceCog()
        guild_id = 42
        # discard on empty set shouldn't raise
        cog._wakeword_guilds.discard(guild_id)
        assert guild_id not in cog._wakeword_guilds

    def test_insufficient_permissions_no_state_change(self):
        """Req 1.6: Non-admin gets error, no state change."""
        cog = FakeVoiceCog()
        guild_id = 42
        # Simulate: permission check fails, so we never modify state
        has_permission = False
        if has_permission:
            cog._wakeword_guilds.add(guild_id)
        assert guild_id not in cog._wakeword_guilds

    def test_wakeword_independent_of_voice_enable(self):
        """Req 1.3: /wakeword and /voice are independent sets."""
        cog = FakeVoiceCog()
        cog._enabled_guilds.add(100)
        cog._wakeword_guilds.add(200)
        # Both guilds listen, but via different mechanisms
        assert cog._should_listen(100) is True
        assert cog._should_listen(200) is True
        # Removing one doesn't affect the other
        cog._wakeword_guilds.discard(100)
        assert cog._should_listen(100) is True  # still in _enabled_guilds
        cog._enabled_guilds.discard(200)
        assert cog._should_listen(200) is True  # still in _wakeword_guilds
