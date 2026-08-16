# No-Audio Diagnosis — HelloDJ connects but produces no sound

## Date
2026-08-16

## Symptom (differs from prior connect-failure work)
The bot now **connects successfully** to voice channels (handshake COMPLETE, per-channel
permissions fine) but **no audio is heard coming from it**. This is an audio-output
problem, not the earlier connection/permission failure.

Live evidence (guild 1538245165708476468, "Meeting Room 1", channel 1538265473681063976):
```
VOICE_DEBUG[connect_player] PER-CHANNEL perms ... connect=True speak=True view_channel=True ...
connect_player handshake COMPLETE attempt=1 (guild_id=1538245165708476468)
```

## Instrumentation / code paths inspected
- `bot/player.py::connect_player` — constructs `HybridPlayer`, sends op-4, waits for
  `_connection_event` / handshake fields, returns player. Callers store into
  `state["player"]` **after** connect returns.
- `bot/cogs/voice.py::on_voice_state_update` / `_start_receive` — intended to register the
  voice_recv `PipelineSink` when the bot joins voice.
- `bot/voice/hybrid_player.py` — `HybridPlayer(wavelink.Player, voice_recv.VoiceRecvClient)`.
- `bot/voice/voice_commands.py::_speak` / `bot/voice/tts.py::TTSPLayer.play_pcm` — TTS send path
  via `VoiceClient.send_audio_packet(opus, encode=False)`.

## Root-cause candidates considered (7 → distilled to 2)
1. Receive sink never registers (player-setup race). **PRIMARY — TTS/voice-activation output dead.**
2. HybridPlayer's real voice `_connection` never connected → `send_audio_packet`/`listen()` fail.
3. Lavalink music never streams — YouTube source fails (`All clients failed to load the item`).
4. `/start` slash alias broken (`TypeError: 'Command' object is not callable`).
5. Lavalink node connect flakiness at boot.
6. TTS engine (speaches) misconfigured.
7. Voice permissions (Meeting Room 1 is fine — connect=True speak=True).

## Primary root cause (voice-activation output dead)
The cog's `on_voice_state_update` listener fires on the bot's own voice-state update, but at
that instant `state["player"]` is still `None`:
```
2026-08-16 03:31:30 INFO cogs.voice: No player yet for guild 1538245165708476468 — receive will start on connect
2026-08-16 03:31:30 INFO player: connect_player handshake COMPLETE attempt=1 (guild_id=1538245165708476468)
```
`_start_receive` hits the `player_obj is None` branch and returns. **No later event re-triggers
`_start_receive`** and no "Voice receiver started" line ever appears → the wake-word sink is
never registered → the wake word is never heard → **TTS responses are never triggered**.
Even if `listen()` ran, `voice_recv.VoiceRecvClient.listen()` calls `is_connected()` →
`self._connection.is_connected()`, and wavelink's `Player.connect()` never establishes the
real voice socket/SSRC/secret_key, so it would raise "Not connected".

## Secondary root cause (no music either)
Repeated Lavalink `TrackException`: `(yts.version: 1.18.2) All clients failed to load the item.`
(YouTube plugin 1.18.2). No `on_track_start` events. So even if playback were attempted there
is no music to hear.

## Validation logging to add (gated, switchable)
- In `_start_receive`: log the `player_obj` existence/`connected`, and log the exact
  `listen()` outcome (success vs "Not connected" exception).
- In `on_voice_state_update`: log every bot voice-state event with before/after channel and
  whether `state["player"]` exists at that moment — proves the race.
- In `connect_player` after handshake COMPLETE: log whether the HybridPlayer's real
  `_connection.is_connected()` is True (socket established) vs False (Lavalink-only forward).
- In `tts.py::TTSPLayer.play_pcm`: log the `voice_client` type, `is_connected()`, and the
  first `send_audio_packet` success/failure — proves whether TTS out can work.
