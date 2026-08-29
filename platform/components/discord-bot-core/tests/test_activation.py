"""Tests for the per-guild activation gate (on-prem /activate parity).

Covers `discord_bot_core.policy.activation` + the pure gate decision in
`discord_bot_core.commands.activation_cog.command_allowed`:

* an unactivated guild is locked; a validated key activates it; a wrong/absent
  key does not (and never crashes);
* the pure command gate blocks commands in a locked guild EXCEPT `activate`,
  and always allows DMs (no guild).

Uses an in-memory ActivationStore — no AWS, no discord.py.
"""

from __future__ import annotations

from typing import Any

from discord_bot_core.commands.activation_cog import (
    allowed_command_names,
    command_allowed,
)
from discord_bot_core.policy.activation import GuildActivation


class _MemStore:
    """In-memory ActivationStore keyed by guild id string."""

    def __init__(self, items: dict[str, dict[str, Any]] | None = None) -> None:
        self._items = items or {}

    def get_activation_data(self, guild_id: str) -> dict[str, Any] | None:
        return self._items.get(guild_id)

    def set_activated(self, guild_id: str, activated: bool) -> None:
        self._items.setdefault(guild_id, {})["activated"] = activated


def test_unactivated_guild_is_locked() -> None:
    act = GuildActivation(_MemStore({"1": {"key": "SECRET", "activated": False}}))
    assert act.is_activated(1) is False


def test_valid_key_activates() -> None:
    store = _MemStore({"1": {"key": "SECRET", "activated": False}})
    act = GuildActivation(store)

    assert act.activate(1, "SECRET") is True
    assert act.is_activated(1) is True


def test_wrong_key_does_not_activate() -> None:
    act = GuildActivation(_MemStore({"1": {"key": "SECRET", "activated": False}}))

    assert act.activate(1, "nope") is False
    assert act.is_activated(1) is False


def test_key_whitespace_is_ignored() -> None:
    act = GuildActivation(_MemStore({"1": {"key": "SECRET", "activated": False}}))
    assert act.activate(1, "  SECRET  ") is True


def test_no_stored_key_cannot_activate() -> None:
    act = GuildActivation(_MemStore({"1": {"key": "", "activated": False}}))
    assert act.activate(1, "anything") is False


def test_missing_guild_item_is_locked() -> None:
    act = GuildActivation(_MemStore({}))
    assert act.is_activated(999) is False


# -- pure command gate ------------------------------------------------------


def _act(activated: bool) -> GuildActivation:
    return GuildActivation(_MemStore({"1": {"key": "K", "activated": activated}}))


def test_gate_blocks_commands_in_locked_guild() -> None:
    assert (
        command_allowed(_act(False), command_name="play", guild_id=1) is False
    )


def test_gate_allows_activate_in_locked_guild() -> None:
    assert (
        command_allowed(_act(False), command_name="activate", guild_id=1) is True
    )


def test_gate_allows_all_commands_when_activated() -> None:
    assert command_allowed(_act(True), command_name="play", guild_id=1) is True


def test_gate_allows_dms() -> None:
    # No guild context (DM) is always allowed — activation is per-guild.
    assert (
        command_allowed(_act(False), command_name="play", guild_id=None) is True
    )


def test_gate_allows_help_in_locked_guild() -> None:
    # /help is allowed even when locked (users can get help before activating).
    assert (
        command_allowed(_act(False), command_name="help", guild_id=1) is True
    )


# -- pure command VISIBILITY (per-guild sync subset) ------------------------


_ALL = {"activate", "help", "play", "skip", "pause"}


def test_visible_unactivated_only_activate_and_help() -> None:
    assert allowed_command_names(False, _ALL) == {"activate", "help"}


def test_visible_activated_hides_activate_shows_rest() -> None:
    visible = allowed_command_names(True, _ALL)
    assert "activate" not in visible
    assert visible == {"help", "play", "skip", "pause"}


def test_visible_unactivated_intersects_defined_commands() -> None:
    # If the bot doesn't define /help yet, unactivated shows only /activate.
    assert allowed_command_names(False, {"activate", "play"}) == {"activate"}


def test_visible_activated_without_activate_is_noop_on_activate() -> None:
    # Removing activate when it isn't present is harmless.
    assert allowed_command_names(True, {"play", "skip"}) == {"play", "skip"}
