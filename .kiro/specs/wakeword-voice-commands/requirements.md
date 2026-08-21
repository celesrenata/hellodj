# Requirements Document

## Introduction

Wire the existing "Hello DJ" wake word detection system into a full voice command pipeline. Replace the current keyword-based intent classifier with an LLM-powered agent (Ollama gemma4) that decomposes natural language utterances into structured playback actions, supports source-specific overrides, handles multi-action commands, and covers the complete set of player operations including volume, playlists, and queue management. Add a new `/wakeword` slash command for per-guild toggle control.

## Glossary

- **Bot**: The HelloDJ Discord bot application
- **Wake_Word_System**: The ONNX-based "Hello DJ" detection model and its audio pipeline
- **Intent_Engine**: The LLM-powered (Ollama gemma4) natural language understanding component that replaces the keyword-based intent classifier
- **Orchestrator**: The voice command state machine (`voice_commands.py`) that coordinates wake→STT→intent→action→TTS
- **Player**: The per-guild playback engine managing queue, source resolution, and wavelink integration
- **Source_Provider**: A music/video streaming backend (youtube, youtube_music, soundcloud, spotify, tidal, deezer)
- **STT_Engine**: The speech-to-text component that transcribes captured audio to text
- **TTS_Engine**: The text-to-speech component that speaks responses back into the voice channel
- **Guild**: A Discord server in which the Bot operates
- **SSRC**: Synchronisation Source identifier used by Discord voice to identify individual speakers
- **Queue**: The ordered list of tracks pending playback for a Guild
- **Structured_Action**: A JSON object representing a single discrete player command with fields for action type, query, and source override

## Requirements

### Requirement 1: Wakeword Toggle Slash Command

**User Story:** As a server administrator, I want a `/wakeword on` and `/wakeword off` command, so that I can toggle wake word listening per guild without using the legacy `/voice enable|disable` command.

#### Acceptance Criteria

1. WHEN a user with Manage Server permission invokes `/wakeword on` in a Guild, THE Bot SHALL enable wake word listening for that Guild and respond with an ephemeral confirmation message within 3 seconds
2. WHEN a user with Manage Server permission invokes `/wakeword off` in a Guild, THE Bot SHALL disable wake word listening for that Guild and respond with an ephemeral confirmation message within 3 seconds
3. IF a user without Manage Server permission invokes `/wakeword on` or `/wakeword off`, THEN THE Bot SHALL respond with an ephemeral error message indicating insufficient permissions
4. WHILE wake word listening is disabled for a Guild, THE Wake_Word_System SHALL ignore all audio frames received from that Guild without buffering or processing them
5. WHEN the wakeword toggle state changes for a Guild, THE Bot SHALL persist the new state to the credential store so that the setting is restored to its last value on bot restart
6. IF a user invokes `/wakeword on` while the Wake_Word_System ONNX model is not loaded, THEN THE Bot SHALL respond with an ephemeral error message indicating the wake word model is unavailable and SHALL NOT change the persisted toggle state
7. IF a user invokes `/wakeword on` while wake word listening is already enabled for that Guild, THEN THE Bot SHALL respond with an ephemeral message indicating wake word listening is already enabled without changing state

### Requirement 2: Per-User Audio Isolation

**User Story:** As a user in a busy voice channel, I want only my audio captured after I trigger the wake word, so that other speakers do not interfere with my voice command.

#### Acceptance Criteria

1. WHEN the Wake_Word_System detects a positive wake word from an SSRC, THE Orchestrator SHALL resolve the triggering user's identity (guild_id, channel_id, user_id) from the SSRC mapping
2. WHILE the Orchestrator is in the WAKE_TICK state for a specific user, THE STT_Engine SHALL receive PCM audio exclusively from that user's SSRC ring buffer
3. WHILE an active session is in WAKE_TICK or PROCESSING state for a guild, THE Orchestrator SHALL discard wake word detections from any other SSRC in that guild
4. WHEN the triggering user's SSRC audio RMS falls below 500 for at least one 200ms polling cycle (3200 samples at 16 kHz), THE Orchestrator SHALL finalize the audio capture and pass the accumulated PCM to the STT_Engine
5. IF the triggering user's SSRC does not reach the silence threshold within 10 seconds of entering WAKE_TICK, THEN THE Orchestrator SHALL finalize audio capture at that point and pass the accumulated PCM to the STT_Engine
6. IF the captured PCM is shorter than 1600 samples (100ms at 16 kHz), THEN THE Orchestrator SHALL discard the audio without invoking STT and transition the session back to IDLE

### Requirement 3: LLM Intent Extraction

**User Story:** As a user, I want my natural language voice commands understood accurately, so that I do not have to use rigid keyword-based phrasing.

#### Acceptance Criteria

1. WHEN the STT_Engine produces a transcript, THE Intent_Engine SHALL send the transcript to Ollama gemma4 via the configured LLM endpoint (`llm.api_url`, `llm.model`) using the OpenAI-compatible `/v1/chat/completions` endpoint
2. THE Intent_Engine SHALL use a system prompt that instructs the LLM to return one or more Structured_Action objects as a JSON array, where each Structured_Action contains at minimum an `action` field, a `query` field (string or null), a `source_override` field (string or null), and an `args` field (object or null)
3. WHEN the LLM returns a JSON response that is both parseable and conforms to the Structured_Action schema (each object contains the required `action` field), THE Intent_Engine SHALL parse the response into a list of Structured_Action objects
4. IF the LLM endpoint is unreachable or returns a non-2xx HTTP status, THEN THE Intent_Engine SHALL fall back to the existing keyword-based classifier and return the keyword classifier's result for the same transcript
5. IF the LLM returns unparseable JSON or a JSON response where any object is missing the required `action` field, THEN THE Intent_Engine SHALL fall back to the keyword-based classifier and log the raw response at warning level
6. IF the LLM inference does not complete within 10 seconds, THEN THE Intent_Engine SHALL cancel the request, fall back to the keyword-based classifier, and log the timeout
7. IF the LLM returns a valid JSON response containing an empty array, THEN THE Intent_Engine SHALL fall back to the keyword-based classifier for the same transcript
8. WHEN the transcript exceeds 500 characters, THE Intent_Engine SHALL truncate it to 500 characters before sending to the LLM endpoint

### Requirement 4: Source-Specific Playback Override

**User Story:** As a user, I want to say "play Shape of You on Spotify" and have the system use Spotify regardless of the guild's default source, so that I can pick my preferred platform per request.

#### Acceptance Criteria

1. WHEN the Intent_Engine detects a source phrase ("on spotify", "on tidal", "on youtube", "on youtube music", "on soundcloud", "on deezer") in the transcript, THE Structured_Action SHALL include a source_override field set to the corresponding internal identifier using the mapping: "spotify" → "spotify", "tidal" → "tidal", "youtube" → "youtube", "youtube music" → "youtube_music", "soundcloud" → "soundcloud", "deezer" → "deezer"
2. WHEN a Structured_Action contains a source_override field, THE Player SHALL use that Source_Provider's search prefix or TrackSource for Lavalink track resolution instead of the Guild's configured default source_provider, and SHALL attempt the corresponding direct-stream sidecar first if one exists for that provider
3. WHEN a subsequent play command does not include a source_override field, THE Player SHALL use the Guild's persisted default source_provider without modification from any prior override
4. IF the source_override provider returns no results from Lavalink search within 10 seconds, THEN THE Player SHALL inform the user via TTS that the requested source could not fulfill the request and SHALL NOT fall back to another provider automatically
5. IF the source_override value is not one of the six valid internal identifiers ("youtube", "youtube_music", "soundcloud", "spotify", "tidal", "deezer"), THEN THE Intent_Engine SHALL discard the source_override field and THE Player SHALL use the Guild's default source_provider

### Requirement 5: Queue Integration

**User Story:** As a user, I want tracks resolved from voice commands added to the existing queue, so that voice and text-based commands share the same playback state.

#### Acceptance Criteria

1. WHEN the Intent_Engine resolves a play action, THE Player SHALL convert the resolved track using _track_entry and call add_track(state, guild_id, entry) to append the track to the Guild's queue list, following the same function call path as slash-command playback
2. WHEN the Queue is empty and a play action is received, THE Player SHALL begin playback by calling _play_next_from_queue within 500ms of the track being added, transitioning the player to the playing state
3. WHEN the Queue is not empty and a play action is received, THE Player SHALL append the track to the end of the Queue and confirm via TTS with a message containing the track title (e.g., "Added {title} to the queue")
4. THE Player SHALL call persist(guild_id) after every queue mutation triggered by a voice command, writing the same session.json structure (voice_channel_id, text_channel_id, current, queue, source_provider, repeat_mode) as slash-command-triggered persistence
5. IF the Intent_Engine resolves a play action but Playable.search returns no results, THEN THE Player SHALL respond via TTS with a message indicating the song was not found and SHALL NOT modify the Queue

### Requirement 6: Multi-Action Decomposition

**User Story:** As a user, I want to say "play Shape of You on Spotify and then add Blinding Lights from YouTube" in a single utterance, so that the system handles compound requests without requiring separate wake word activations.

#### Acceptance Criteria

1. WHEN the LLM identifies multiple discrete commands within a single transcript, THE Intent_Engine SHALL return a JSON array containing one Structured_Action per command in the order spoken, with a maximum of 10 Structured_Actions per utterance
2. WHEN the Intent_Engine returns a JSON array containing a single Structured_Action, THE Orchestrator SHALL execute that action identically to a multi-action sequence of length one
3. THE Orchestrator SHALL execute each Structured_Action sequentially in the order returned by the Intent_Engine, waiting for each action to complete or fail before starting the next
4. IF one action in a multi-action sequence fails, THEN THE Orchestrator SHALL continue executing the remaining actions and include in the consolidated TTS response which action failed and its position in the sequence
5. IF all actions in a multi-action sequence succeed, THEN THE Orchestrator SHALL provide a single consolidated TTS response summarizing the result of each action in spoken order
6. IF the LLM fails to parse the transcript into valid Structured_Actions, THEN THE Intent_Engine SHALL return an error indication and THE Orchestrator SHALL respond via TTS with a message indicating the command could not be understood
7. THE Orchestrator SHALL complete execution of all actions in a multi-action sequence within 30 seconds of receiving the JSON array from the Intent_Engine

### Requirement 7: Playback Control via Voice

**User Story:** As a user, I want to control all standard player operations by voice, so that I do not need to type slash commands while listening to music.

#### Acceptance Criteria

1. WHEN the Intent_Engine classifies a "skip" or "next" action and the Player is currently playing or paused, THE Player SHALL stop the current track and advance to the next track in the Queue
2. IF the Intent_Engine classifies a "skip" or "next" action and no track is currently playing or paused, THEN THE Player SHALL respond via TTS indicating there is nothing to skip
3. WHEN the Intent_Engine classifies a "pause" action and the Player is currently playing, THE Player SHALL pause the currently playing track
4. IF the Intent_Engine classifies a "pause" action and no track is currently playing, THEN THE Player SHALL respond via TTS indicating there is nothing playing
5. WHEN the Intent_Engine classifies a "resume" action and the Player is currently paused, THE Player SHALL resume the paused track
6. IF the Intent_Engine classifies a "resume" action and no track is currently paused, THEN THE Player SHALL respond via TTS indicating there is nothing paused
7. WHEN the Intent_Engine classifies a "stop" action, THE Player SHALL stop playback, clear the Queue, and clear the current track reference
8. WHEN the Intent_Engine classifies a "remove" action with a numeric track position, THE Player SHALL remove the track at that 1-based position from the Queue and persist the updated Queue
9. IF the Intent_Engine classifies a "remove" action with a track position that is less than 1 or greater than the current Queue length, THEN THE Player SHALL respond via TTS indicating an invalid track number
10. WHEN the Intent_Engine classifies a "shuffle" action, THE Player SHALL randomize the order of all tracks in the Queue and persist the updated Queue
11. WHEN the Intent_Engine classifies a "repeat" action with a mode value of "off", "single", or "queue", THE Player SHALL set the repeat mode to the specified value and persist the change
12. WHEN the Intent_Engine classifies a "volume" action with a level between 0 and 100 inclusive, THE Player SHALL set the playback volume to that percentage (mapped to a 0.0–1.0 float for the underlying player)
13. IF a "volume" action specifies a level below 0, THEN THE Player SHALL set the volume to 0 and inform the user via TTS that the value was clamped to 0
14. IF a "volume" action specifies a level above 100, THEN THE Player SHALL set the volume to 100 and inform the user via TTS that the value was clamped to 100
15. WHEN the Intent_Engine classifies a "playlist save" action with a name of 1 to 100 characters, THE Player SHALL save the current Queue as a named playlist in the guild-scoped playlist store
16. IF a "playlist save" action specifies a name that already exists in the guild playlist store, THEN THE Player SHALL respond via TTS indicating a playlist with that name already exists and take no further action
17. WHEN the Intent_Engine classifies a "playlist load" action with a name that matches an existing playlist (case-insensitive), THE Player SHALL replace the Queue with the tracks from the named playlist and begin playback of the first track
18. IF a "playlist load" action specifies a name that does not match any existing playlist in the guild, THEN THE Player SHALL respond via TTS indicating no playlist was found with that name
19. WHEN the Intent_Engine classifies a "playlist list" action, THE Player SHALL enumerate the names of all saved playlists for the guild via TTS, up to a maximum of 25 playlist names
20. THE Orchestrator SHALL respond to every successfully recognized and executed voice command with a TTS confirmation describing the action taken, within 3 seconds of command execution completing

### Requirement 8: Structured Action Schema

**User Story:** As a developer, I want a well-defined schema for LLM output, so that the Orchestrator can reliably parse and execute voice commands.

#### Acceptance Criteria

1. THE Intent_Engine SHALL define the Structured_Action schema as: `{"action": string, "query": string|null, "source_override": string|null, "args": object|null}` where `action` is required and must be one of the supported action types, `query` is a search string of at most 200 characters, `source_override` is one of the valid Source_Provider values (youtube, youtube_music, spotify, tidal, soundcloud, deezer) or null, and `args` is an action-specific parameter object or null
2. THE Intent_Engine SHALL validate each Structured_Action against the schema before execution, rejecting objects that contain unrecognized `action` values, `query` strings exceeding 200 characters, unrecognized `source_override` values, or `args` fields that do not match the expected keys for the given action type
3. IF a Structured_Action fails schema validation, THEN THE Intent_Engine SHALL skip that action, log the validation error, and continue executing subsequent actions in the sequence
4. THE Intent_Engine SHALL support the following action types with their required args: play (args: null), skip (args: null), pause (args: null), resume (args: null), stop (args: null), remove (args: {"index": integer 1-based} or {"title": string}), shuffle (args: null), repeat (args: {"mode": one of "off", "single", "queue"}), volume (args: {"level": integer 0–100}), playlist_save (args: {"name": string, max 100 characters}), playlist_load (args: {"name": string}), playlist_list (args: null), join (args: null), leave (args: null)
5. IF a Structured_Action contains fields not defined in the schema, THEN THE Intent_Engine SHALL ignore the unrecognized fields and process only the defined fields

### Requirement 9: LLM Agent Prompt Design

**User Story:** As a developer, I want the LLM system prompt to reliably produce structured output, so that natural language commands are consistently reduced to executable actions.

#### Acceptance Criteria

1. THE Intent_Engine SHALL include a system prompt that specifies the exact JSON output format, the list of valid action types (play, skip, pause, resume, stop, remove, shuffle, repeat, volume, playlist_save, playlist_load, playlist_list, join, leave), the list of valid source_override values (youtube, youtube_music, soundcloud, spotify, tidal, deezer), and at least 3 few-shot examples covering single-action, multi-action, and source-override transcripts
2. THE Intent_Engine SHALL include the Guild's current playback context in the user prompt: now_playing title (or "nothing" if idle), queue length (integer), and current default source_provider, so the LLM can make contextual decisions such as interpreting "remove the last one" or "play something similar"
3. WHEN the transcript is ambiguous, THE system prompt SHALL instruct the LLM to prefer the most common interpretation (e.g., "play" means search and queue a track, not resume) rather than returning an empty array or asking for clarification
4. THE system prompt SHALL instruct the LLM to request JSON mode output format and THE Intent_Engine SHALL set the `response_format` parameter to `{"type": "json_object"}` in the API request when the endpoint supports it

### Requirement 10: Deezer Source Provider

**User Story:** As a user, I want to request tracks "on Deezer" via voice command, so that I have access to all major streaming platforms.

#### Acceptance Criteria

1. WHEN the Intent_Engine detects "on deezer" (case-insensitive) in the transcript, THE Structured_Action SHALL set source_override to "deezer"
2. THE Player SHALL support "deezer" as a valid Source_Provider by mapping the identifier "deezer" to the Lavalink search prefix "dzsearch:" in the _source_for() helper function
3. IF Deezer search via Lavalink returns no results for the query within 10 seconds, THEN THE Player SHALL inform the user via TTS that no results were found on Deezer and SHALL NOT automatically fall back to another provider
4. THE Player SHALL add "deezer" to the list of valid source_provider values accepted by the `/source` command and the voice source override system
