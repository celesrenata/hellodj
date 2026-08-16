"""Stub harness for bot/permissions.py fix validation.

The installed discord.py builds per-flag `flag_value` namedtuples that have no
`.value` attribute, and `Permissions` exposes per-flag boolean properties plus a
`.value` bitfield. This stub mimics that shape so we can prove the fixed
permissions.py never touches `flag.value` and returns correct granted/missing
maps for a mocked permission set.

Run:  python evidence/permissions-fix/harness.py
"""

import types
import sys

# --- Load the module under test without requiring real discord.py -------------
# Provide a stub `discord` package so bot/permissions.py's `import discord`
# resolves; the module only needs discord.Member as an annotation.
stub_discord = types.ModuleType("discord")
stub_discord.Member = object
sys.modules["discord"] = stub_discord

import importlib.util

spec = importlib.util.spec_from_file_location(
    "permissions", "bot/permissions.py"
)
perm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(perm)
sys.modules["permissions"] = perm


# --- Mock discord.py Permissions / flag_value shape --------------------------
# flag_value: a namedtuple WITHOUT a `.value` attribute (mirrors installed
# discord.py). It only carries a `.name` and a bit `.flag`.
def make_flag(name, flag):
    return (name, flag)


class PermissionsStub:
    """Mimics discord.py Permissions: per-flag booleans, no per-flag `.value`."""

    def __init__(self, granted):
        self._granted = set(granted)
        # Real Permissions also exposes a `.value` bitfield; we keep it to prove
        # the fixed code reads attributes, not this bitfield.
        self.value = 0

    def __getattr__(self, name):
        # Namedtuples' attributes are plain bools in discord.py; here we mimic
        # them via the granted set. Missing attrs raise AttributeError just like
        # real Permissions for unknown flag names.
        if name in self._granted:
            return True
        return False


class MemberStub:
    def __init__(self, guild_permissions):
        self.guild_permissions = guild_permissions


class MemberNoPerms:
    # Simulate a member whose guild_permissions attribute is unavailable.
    # Standalone (not a MemberStub subclass) so it has no instance attr that
    # would shadow the property.
    @property
    def guild_permissions(self):
        raise AttributeError("guild_permissions unavailable")


def assert_(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print(f"PASS: {msg}")


def main():
    # Case 1: a member holding a subset of REQUIRED_PERMISSIONS.
    perms = PermissionsStub({
        "view_channel",
        "send_messages",
        "connect",
        "speak",
        "add_reactions",
        "read_message_history",
        "embed_links",
        "attach_files",
    })
    member = MemberStub(perms)
    granted, missing = perm.check_permissions(member)

    expected_granted = {
        "view_channel": True,
        "send_messages": True,
        "connect": True,
        "speak": True,
        "add_reactions": True,
        "read_message_history": True,
        "manage_channels": False,
        "manage_roles": False,
        "manage_messages": False,
        "embed_links": True,
        "attach_files": True,
    }
    assert_(granted == expected_granted, "granted map matches mock permission set")

    expected_missing = {"manage_channels", "manage_roles", "manage_messages"}
    assert_(set(missing) == expected_missing, "missing set = {manage_channels, manage_roles, manage_messages}")

    voice_missing = perm.missing_voice_permissions(member)
    # view_channel/connect/speak are all granted -> none missing.
    assert_(voice_missing == [], "no voice permissions missing for granted member")

    # Case 2: a member with NO permissions at all.
    member_none = MemberStub(PermissionsStub(set()))
    granted2, missing2 = perm.check_permissions(member_none)
    assert_(set(missing2) == set(perm.REQUIRED_PERMISSIONS), "all required permissions missing when none granted")
    assert_(all(not v for v in granted2.values()), "granted map all-False when none granted")
    assert_(
        set(perm.missing_voice_permissions(member_none))
        == {"view_channel", "connect", "speak"},
        "all voice permissions missing when none granted",
    )

    # Case 3: guild_permissions unavailable -> graceful all-missing, no crash.
    granted3, missing3 = perm.check_permissions(MemberNoPerms())
    assert_(set(missing3) == set(perm.REQUIRED_PERMISSIONS), "unavailable perms -> all required missing")
    assert_(
        set(perm.missing_voice_permissions(MemberNoPerms()))
        == {"view_channel", "connect", "speak"},
        "unavailable perms -> all voice missing",
    )

    # Case 4: prove no `.value` bitwise access remains in the fixed module.
    src = open("bot/permissions.py").read()
    assert_(".value &" not in src, "no '.value &' bitwise comparison in permissions.py")
    assert_("flag.value" not in src, "no 'flag.value' access in permissions.py")
    assert_("perms.value" not in src, "no 'perms.value' access in permissions.py")

    print("\nALL HARNESS CHECKS PASSED")


if __name__ == "__main__":
    main()
