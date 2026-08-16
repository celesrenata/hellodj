# permissions.py runtime-bug fix — evidence

## Bug (verified from live deployment)

`bot/permissions.py` lines 44 and 54 used `perms.value & flag.value` where `flag`
was a `flag_value` namedtuple from the installed discord.py. Those per-flag
objects have **no `.value` attribute**. On every `on_ready`,
`check_permissions` raised `AttributeError: 'flag_value' object has no attribute
'value'`, aborting the `oauth_store.write_guilds(...)` guild-data sync. The pod
stayed Running (discord.py swallows the event exception) but the OAuth guild
registry write failed on each reconnect.

## Root cause

- A member's permissions come from `member.guild_permissions`, a `Permissions`
  object.
- `Permissions` exposes a `.value` int bitfield **and** per-flag boolean
  properties (e.g. `perms.view_channel`, `perms.send_messages`, `perms.connect`,
  `perms.speak`).
- The old code iterated over `discord.Permissions.<attr>` flag descriptors and
  did `perms.value & flag.value`. `flag` is a `flag_value` namedtuple with no
  `.value`, so the comparison crashed.

## Fix (only in `bot/permissions.py`)

- Converted `REQUIRED_PERMISSIONS` and `VOICE_PERMISSIONS` from
  `discord.Permissions.<attr>` descriptors to plain snake_case attribute-name
  strings (`view_channel`, `send_messages`, `connect`, `speak`, ...).
- `check_permissions` now reads each boolean attribute off the member's
  `guild_permissions` object:

  ```python
  held = bool(getattr(perms, flag, False)) if perms is not None else False
  ```

  `granted[flag] = held` and missing flags appended when `held` is False.

- `missing_voice_permissions` now does the same via
  `bool(getattr(perms, flag, False))`.

- None-guards: `_perms_of(member)` returns `None` when
  `member.guild_permissions` is unavailable; both functions degrade to
  "all missing" instead of crashing.

- No `.value` bitwise access remains anywhere in the file.

## Validation

### 1. Syntax check

```
$ python -m py_compile bot/permissions.py
PY_COMPILE_OK
```

### 2. Stub harness (`evidence/permissions-fix/harness.py`)

discord.py is not installed locally, so a stub mimics its `Permissions` /
`flag_value` shape:

- `flag_value` namedtuples carry a `.name` + bit but **no `.value`**.
- `Permissions` exposes per-flag booleans plus a `.value` bitfield.

The harness loads `bot/permissions.py` via `importlib` with a stubbed `discord`
module, then asserts correct granted/missing maps.

Run: `python evidence/permissions-fix/harness.py`

```
PASS: granted map matches mock permission set
PASS: missing set = {manage_channels, manage_roles, manage_messages}
PASS: no voice permissions missing for granted member
PASS: all required permissions missing when none granted
PASS: granted map all-False when none granted
PASS: all voice permissions missing when none granted
PASS: unavailable perms -> all required missing
PASS: unavailable perms -> all voice missing
PASS: no '.value &' bitwise comparison in permissions.py
PASS: no 'flag.value' access in permissions.py
PASS: no 'perms.value' access in permissions.py

ALL HARNESS CHECKS PASSED
```

Combined final run also produced `ALL_OK` (harness + `py_compile`).

## Result

The corrected module no longer touches `.value` on `flag_value` objects, reads
the real boolean attributes off the `Permissions` object, and handles a missing
`guild_permissions` gracefully. The `oauth_store.write_guilds(...)` sync path in
`bot.py` no longer aborts on `AttributeError` each `on_ready` reconnect.
