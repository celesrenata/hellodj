# Voice Connect Failure — Confirmed Diagnosis (per-channel Connect/Speak denied)

## Date
2026-08-16

## Symptom
`/play` fails with `ChannelTimeoutException` on voice channel `🌐│Social #1`
(guild "Under The Influence" guild_id=1501686893765595296,
channel_id=1501688238165721128). The bot sends op-4 (voice join) but never
completes the voice handshake — no `VOICE_STATE_UPDATE` / `VOICE_SERVER_UPDATE`
for itself returns.

## Instrumentation (switchable debug layer)
Added `bot/voice_debug.py` (gated by env `HELLODJ_VOICE_DEBUG`, default "1" = on,
set to "0" to disable). It logs:
- `PER-CHANNEL perms` — `channel.permissions_for(guild.me)` on the exact target
  voice channel (honors per-channel role/overwrites; the prior diagnostic used
  guild-level `guild.me.guild_permissions`, which ignores channel overwrites).
- `op-4 SEND` — the voice-join opcode being sent to the gateway.
- `raw VOICE_STATE_UPDATE / VOICE_SERVER_UPDATE` — whether the bot's OWN voice
  events arrive on the gateway, and whether a voice client is registered at
  arrival time (the registration-race discriminator).

Wired into `bot/player.py` (connect path) and `bot/bot.py` (raw-listener install).
Exposed as `HELLODJ_VOICE_DEBUG: "1"` in `kube/bot-configmap.yaml`. Added
`voice_debug.py` to the Dockerfile COPY list.

## Live evidence (deployed debug build, image `voicedbg-20260816`)
Captured from the pod persistent log `/app/config/bot.log` across 3 `/play`
attempts (02:03, 03:14, 03:18). Each attempt reproduced the same result.

### Per-channel permission (the decisive line)
```
VOICE_DEBUG[connect_player] PER-CHANNEL perms channel=1501688238165721128 member=HelloDJ#8609 connect=False speak=False view_channel=True manage_channels=False move_members=False use_voice_activity=False
VOICE_DEBUG[connect_player] *** per-channel Connect/Speak DENIED on 🌐│Social #1 — Discord silently drops the voice join; this is the likely ChannelTimeoutException cause. Guild-level check reports 'holds all' but the channel overwrite denies it.
```

The bot's **per-channel** permissions on `🌐│Social #1` are `connect=False,
speak=False, view_channel=True`. Discord silently drops the bot's op-4 voice
join when `Connect` is denied on that channel, so no `VOICE_STATE_UPDATE` /
`VOICE_SERVER_UPDATE` for the bot is ever emitted.

### Handshake never completes (consistent with silent drop)
```
connect_player TIMEOUT/FAILURE for channel=🌐│Social #1 guild_id=1501686893765595296 connection_event.set=False session_id=None token=None endpoint=None voice_state={'voice': {}}
cogs.music: Play failed (ChannelTimeoutException): Unable to connect to 🌐│Social #1 as it exceeded the timeout of 10.0 seconds.
```

### The misleading guild-level check
```
connect_player: guild_id=1501686893765595296 bot member HelloDJ#8609 holds all voice permissions (no Connect/Speak/ViewChannel denial)
```
This is the false negative that obscured the cause: `guild.me.guild_permissions`
is **guild-level only** and does not reflect the **channel overwrite** that
denies Connect/Speak on `🌐│Social #1`.

## Root Cause (confirmed)
The bot **lacks the per-channel `Connect` (and `Speak`) permission** on voice
channel `🌐│Social #1` (1501688238165721128). A channel overwrite (or the
channel's role setup) denies Connect/Speak to the bot despite the guild-level
permission being granted. Discord silently ignores the bot's op-4 voice join when
Connect is denied → no voice handshake events → 30s timeout →
`ChannelTimeoutException`.

The `403 Forbidden (error code: 50001): Missing Access` on the session-resume
path is the same permission family surfacing on the resume attempt.

## Fix (Discord-side configuration — NOT a code bug)
Grant the bot (`1534778518137995325`) the `Connect` and `Speak` permissions on
`🌐│Social #1` (channel 1501688238165721128) in "Under The Influence"
(guild 1501686893765595296):
1. Open the channel → Channel Settings → Permissions.
2. Add/override the bot's role (or @HelloDJ) with **Connect** and **Speak**
   allowed, or add the bot as a channel-level member override.
3. After granting, `/play` should complete the voice handshake.

## Debug layer cleanup
The debug layer is switchable via `HELLODJ_VOICE_DEBUG` (default "1" = on).
After the permission fix is verified, set `HELLODJ_VOICE_DEBUG: "0"` in
`kube/bot-configmap.yaml` and redeploy to disable the instrumentation.
