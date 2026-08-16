# Voice Connect Failure — Root Cause Diagnosis & Fix

## Date
2026-08-15

## Symptom
`/play` fails with `ChannelTimeoutException` on voice channel `🌐│Social #1` in guild
"Under The Influence" (guild_id=1501686893765595296, channel_id=1501688238165721128).
The bot sends op-4 (voice join) but never completes the voice handshake
(no `VOICE_STATE_UPDATE`/`VOICE_SERVER_UPDATE` for itself returns).

## Instrumentation & Evidence
Deployed instrumented builds (op-4 send-path wrap + real permission-bit dump +
voice-event parser wrap) and captured live gateway logs.

### 1. Op-4 send path WORKS
```
gateway SEND op=4 voice_state_update guild_id=1501686893765595296 channel_id=1501688238165721128 self_mute=False self_deaf=False
```
The bot sends the voice-join opcode to the correct guild/channel. Not a send bug.

### 2. Bot's own voice reply never arrives; socket alive
- Only OTHER users' `VOICE_STATE_UPDATE` arrive (`user_id=650299934171463690`, …), always `forwarded=False`.
- Zero `VOICE_STATE_UPDATE` for the bot itself (`self_id=1534778518137995325`) and zero `VOICE_SERVER_UPDATE`.
- So the socket delivers other users' events but the bot's own voice events never land → handshake never completes.

### 3. Gateway pre-on_ready stall (masking issue, now fixed)
A fresh pod connected to the gateway (Session ID logged, which comes from the READY payload)
but `on_ready` NEVER fired and the guild cache never populated (no GUILD_CREATE, no slash sync,
no /play received). This blocked the earlier permission dump and obscured the real cause.

### 4. Permission dump (after gateway fix enabled on_ready)
```
PERMISSION dump channel=🌐│Social #1 guild=Under The Influence(1501686893765595296) member_id=1534778518137995325 connect=False view_channel=True speak=False is_connectable=None raw_chan_perms=212212253781063
```
Decoded bitfield `212212253781063`:
- present: ADD_REACTIONS, ATTACH_FILES, BAN_MEMBERS, CREATE_INSTANT_INVITE, EMBED_LINKS,
  KICK_MEMBERS, MANAGE_MESSAGES, SEND_MESSAGES, VIEW_CHANNEL
- **CONNECT: False**, **SPEAK: False**

## Root Cause
The bot **lacks the `Connect` (and `Speak`) permission** on voice channel `🌐│Social #1`.
Discord silently ignores the bot's op-4 voice join when `Connect` is denied, so no
`VOICE_STATE_UPDATE`/`VOICE_SERVER_UPDATE` for the bot is ever emitted → the handshake
times out → `ChannelTimeoutException`.

## Fix (Discord-side configuration — not a code bug)
Grant the bot (`1534778518137995325`) the `Connect` and `Speak` permissions on
`🌐│Social #1` (channel 1501688238165721128) in "Under The Influence" (1501686893765595296).
After granting, `/play` should complete the voice handshake.

## Code change kept (resilience improvement)
`bot/bot.py`: added a **gateway-health watchdog** that detects the pre-on_ready stall
(READY received but guild cache never populates) and forces a clean reconnect /
process restart. This resolved the masking stall so the permission issue became visible.
Configurable via env: `GATEWAY_READY_TIMEOUT` (default 120s), `GATEWAY_RECONNECT_BACKOFF`
(default 30s), `GATEWAY_RESTART_AFTER` (default 3).

## Cleanup
Removed all diagnostic instrumentation from `bot/player.py` (op-4 send wrap, permission dump,
voice-event parser wrap). Final image: `registry.celestium.life/hellodj/bot:gw-fix-final-20260815`.
Verified: `on_ready fired with 1 guilds`, watchdog active, no instrumentation logs.
