# Requirements Document

## Introduction

Replace the existing keyword-based intent classification system in HelloDJ's voice command pipeline with an LLM-driven (Ollama gemma4) approach. The new pipeline retains the existing wake word detection, per-user audio isolation, STT, and TTS infrastructure, but replaces the simplistic keyword router (`voice/intent.py`) with a structured JSON extraction call to a local Ollama instance running gemma4. This enables compound utterance decomposition, source override parsing, and richer action extraction without brittle keyword matching.

## Glossary

- **Voice_Pipeline**: The end-to-end system from wake word detection through STT transcription, intent extraction, action execution, and TTS confirmation
- **Orchestrator**: The `VoiceCommandOrchestrator` class in `voice/voice_commands.py` that manages the state machine (IDLE → WAKE_TICK → PROCESSING → CONFIRM → RESPONDING)
- **Intent_Extractor**: The new Ollama-backed module that replaces the keyword-based `intent.py` classifier, parsing utterances into structured JSON commands
- **Ollama_Client**: The HTTP client component that communicates with the Ollama inference API endpoint for gemma4 model invocation
- **Command_Object**: A structured JSON object representing a single parsed voice command with fields: action, source, query, and arguments
- **Source_Override**: A per-command directive (e.g., "on spotify") that overrides the guild's default `source_provider` for that specific request only
- **Compound_Utterance**: A single spoken input that contains multiple distinct commands (e.g., "play Shape of You on Spotify and then add Blinding Lights")
- **Wake_Word_Model**: The ONNX-based "Hello DJ" detection model that runs every 80ms on CPU via `voice/wakeword.py`
- **SSRC**: Synchronization Source identifier used by Discord voice to identify individual speakers in a channel
- **Guild**: A Discord server instance; each guild maintains independent playback state and voice pipeline sessions
- **Credential_Store**: The encrypted SQLite database accessed via `cfg()` that stores all bot configuration and secrets
- **Wakeword_Command**: The `/wakeword` slash command that toggles wake word listening per guild

## Requirements

### Requirement 1: Wakeword Slash Command

**User Story:** As a server administrator, I want a `/wakeword on|off` slash command, so that I can toggle wake word listening independently of the legacy `/voice` command.

#### Acceptance Criteria

1. WHEN a user with the Manage Guild permission invokes `/wakeword on`, THE Wakeword_Command SHALL enable wake word listening for the invoking guild and respond with an ephemeral message confirming activation within 3 seconds
2. WHEN a user with the Manage Guild permission invokes `/wakeword off`, THE Wakeword_Command SHALL disable wake word listening for the invoking guild and respond with an ephemeral message confirming deactivation within 3 seconds
3. THE Wakeword_Command SHALL be registered as a separate slash command entry from the existing `/voice enable|disable` command, with both commands callable independently without either unregistering or overwriting the other
4. WHEN either `/wakeword on` or `/voice enable` is invoked, THE Voice_Pipeline SHALL treat the guild as wake-word-active and begin processing incoming audio frames for wake word detections
5. WHEN `/wakeword off` is invoked while `/voice enable` was previously used, THE Voice_Pipeline SHALL stop processing wake word detections for the guild while preserving the `/voice` enabled state for non-wake-word voice features
6. IF a user without the Manage Guild permission invokes `/wakeword on` or `/wakeword off`, THEN THE Wakeword_Command SHALL respond with an ephemeral error message indicating insufficient permissions and take no action on the guild wake word state
7. IF `/wakeword on` is invoked when the bot is not connected to a voice channel in the guild, THEN THE Wakeword_Command SHALL enable the wake-word-active flag for the guild and confirm activation, with detection beginning once the bot joins a voice channel

### Requirement 2: Per-User Audio Isolation

**User Story:** As a user in a voice channel with multiple speakers, I want only my audio to be processed after I say the wake word, so that other conversations do not interfere with my command.

#### Acceptance Criteria

1. WHEN the Wake_Word_Model detects a wake word on a specific SSRC, THE Voice_Pipeline SHALL stream only the audio from that SSRC to the STT engine for a maximum of 10 seconds or until silence is detected (RMS below threshold for 200 milliseconds), whichever comes first
2. WHEN the Wake_Word_Model detects a wake word, THE Voice_Pipeline SHALL pass a context object containing guild_id, channel_id, user_id, and ssrc to the STT stage
3. WHILE the Voice_Pipeline is capturing post-wake-word audio for a specific SSRC, THE Voice_Pipeline SHALL ignore audio from all other SSRCs for that interaction session, where the session ends when STT capture completes, the 10-second maximum elapses, or silence is detected
4. IF a blacklisted user triggers the wake word, THEN THE Voice_Pipeline SHALL discard the detection and remain in IDLE state without forwarding any audio to the STT engine
5. IF the Voice_Pipeline cannot resolve the SSRC to a user_id via the SSRC-to-user mapping, THEN THE Voice_Pipeline SHALL discard the wake word detection and not initiate an interaction session

### Requirement 3: Ollama LLM Intent Extraction

**User Story:** As a user, I want my voice commands parsed by an LLM instead of keyword matching, so that natural phrasing and complex requests are understood correctly.

#### Acceptance Criteria

1. WHEN the STT engine produces a non-empty transcript (at least one non-whitespace character), THE Intent_Extractor SHALL send the transcript to the Ollama gemma4 model via the `/api/chat` endpoint for intent extraction
2. THE Intent_Extractor SHALL return a JSON array of Command_Objects, where each Command_Object contains: action (string, required), source (string, nullable), query (string, nullable), and arguments (object, required, may be empty)
3. THE Intent_Extractor SHALL recognize the following actions: play, skip, pause, resume, stop, shuffle, remove, repeat, queue, join, leave, load_playlist, save_playlist
4. IF the Ollama endpoint is unreachable or returns a non-2xx HTTP status, THEN THE Intent_Extractor SHALL fall back to the keyword-based classifier and log a warning
5. IF the LLM inference call exceeds 10 seconds, THEN THE Intent_Extractor SHALL cancel the request, fall back to keyword-based classification, and log the timeout event
6. THE Intent_Extractor SHALL include a system prompt instructing gemma4 to return only valid JSON conforming to the Command_Object schema
7. IF the STT engine produces an empty or whitespace-only transcript, THEN THE Intent_Extractor SHALL return an empty Command_Object array without invoking the LLM
8. IF the LLM response contains an action value not in the recognized actions list, THEN THE Intent_Extractor SHALL discard that Command_Object from the result array and log the unrecognized action

### Requirement 4: Source Override Extraction

**User Story:** As a user, I want to say "on spotify" or "on tidal" within my voice command, so that the bot searches that specific source instead of the guild's default.

#### Acceptance Criteria

1. WHEN a transcript contains a source directive ("on spotify", "on tidal", "on youtube", "on youtube music", "on soundcloud", "on deezer"), THE Intent_Extractor SHALL populate the source field of the corresponding Command_Object with the normalized source identifier and SHALL NOT include the source directive phrase in the query field
2. THE Intent_Extractor SHALL map spoken source names to internal identifiers: "spotify" → "spotify", "tidal" → "tidal", "youtube" → "youtube", "youtube music" → "youtube_music", "soundcloud" → "soundcloud", "deezer" → "deezer"
3. WHEN a Command_Object has a non-null source field, THE Orchestrator SHALL use that source for track resolution instead of the guild's configured `source_provider`
4. THE Source_Override SHALL apply only to the individual Command_Object; THE Orchestrator SHALL NOT modify the guild's persistent `source_provider` setting
5. IF the Intent_Extractor encounters a source name that does not match any entry in the supported source mapping, THEN THE Intent_Extractor SHALL set the source field to null and THE Orchestrator SHALL fall back to the guild's default `source_provider`

### Requirement 5: Compound Utterance Decomposition

**User Story:** As a user, I want to give multiple commands in a single utterance (e.g., "play X and then skip"), so that I do not need to trigger the wake word multiple times.

#### Acceptance Criteria

1. WHEN a transcript contains multiple distinct commands (up to a maximum of 10 commands per utterance), THE Intent_Extractor SHALL decompose the utterance into an ordered JSON array of Command_Objects preserving the spoken order
2. WHEN the Orchestrator receives a Command_Object array with more than one element, THE Orchestrator SHALL execute the Command_Objects sequentially in array order, awaiting completion of each before starting the next
3. IF any Command_Object in the sequence fails execution, THEN THE Orchestrator SHALL halt execution of remaining commands, report via TTS which commands succeeded and which failed, and return to IDLE state
4. WHEN a compound utterance contains per-command source overrides, THE Intent_Extractor SHALL associate each source override with its corresponding Command_Object independently

### Requirement 6: Playback Action Execution

**User Story:** As a user, I want all existing playback commands (play, skip, pause, resume, stop, shuffle, remove, repeat, queue, join, leave) to work via the new LLM pipeline, so that functionality is preserved.

#### Acceptance Criteria

1. WHEN a Command_Object has action "play", THE Orchestrator SHALL resolve the query field as a track search using the applicable source and enqueue the first result via the existing player system; IF no results are found, THE Orchestrator SHALL report the failure via TTS
2. WHEN a Command_Object has action "skip", THE Orchestrator SHALL advance to the next track in the queue; IF no track is playing, THE Orchestrator SHALL report via TTS that there is nothing to skip
3. WHEN a Command_Object has action "pause", THE Orchestrator SHALL pause current playback; IF no track is playing, THE Orchestrator SHALL report via TTS that there is nothing to pause
4. WHEN a Command_Object has action "resume", THE Orchestrator SHALL resume paused playback; IF playback is not paused, THE Orchestrator SHALL report via TTS that nothing is paused
5. WHEN a Command_Object has action "stop", THE Orchestrator SHALL stop playback, clear the queue, and reset the current track state
6. WHEN a Command_Object has action "shuffle", THE Orchestrator SHALL randomize the current queue order and persist the change
7. WHEN a Command_Object has action "remove" with an index argument, THE Orchestrator SHALL remove the track at that 1-based index from the queue; IF the index is out of range or the track name is not found, THE Orchestrator SHALL report the error via TTS
8. WHEN a Command_Object has action "repeat" with a mode argument (off, single, queue), THE Orchestrator SHALL set the guild's repeat mode accordingly
9. WHEN a Command_Object has action "queue", THE Orchestrator SHALL report via TTS the current queue length and the titles of up to 5 upcoming tracks
10. WHEN a Command_Object has action "join", THE Orchestrator SHALL connect the bot to the requesting user's voice channel; IF the user is not in a voice channel, THE Orchestrator SHALL report the error via TTS
11. WHEN a Command_Object has action "leave", THE Orchestrator SHALL disconnect the bot from the voice channel, clear the queue, and reset playback state

### Requirement 7: Playlist Operations

**User Story:** As a user, I want to load and save playlists by voice, so that I can manage my saved collections hands-free.

#### Acceptance Criteria

1. WHEN a Command_Object has action "load_playlist" with a playlist name argument, THE Orchestrator SHALL load the named playlist's tracks into the guild's queue in append mode and begin playback if the queue was previously empty
2. WHEN a Command_Object has action "save_playlist" with a playlist name argument of 1 to 64 characters, THE Orchestrator SHALL save all tracks currently in the guild's queue as a new playlist with the specified name and confirm the saved track count via TTS
3. IF the specified playlist name does not match any existing playlist (case-insensitive) during a load operation, THEN THE Orchestrator SHALL report the error via TTS and list up to 5 available playlist names from the guild's storage
4. IF a save operation is requested when the guild's queue is empty, THEN THE Orchestrator SHALL respond via TTS with a message indicating there are no tracks to save
5. IF a save operation specifies a playlist name that already exists (case-insensitive), THEN THE Orchestrator SHALL respond via TTS with a message indicating the name is taken and request an alternative name

### Requirement 8: TTS Confirmation

**User Story:** As a user, I want brief spoken confirmation after each command executes, so that I know my request was understood and acted upon.

#### Acceptance Criteria

1. WHEN the Orchestrator completes execution of a Command_Object, THE Voice_Pipeline SHALL generate a TTS confirmation message of no more than 150 characters that states the action performed and the affected target (e.g., track title, queue length, channel name)
2. WHEN executing a compound utterance, THE Orchestrator SHALL provide a single summary TTS response after all commands complete that states the count of commands executed and the outcome of each in spoken order, rather than confirming each individually
3. IF command execution fails, THEN THE Voice_Pipeline SHALL speak an error message that identifies the failed action and the reason for failure (e.g., "Could not play — no results found", "Could not skip — nothing playing")
4. IF TTS synthesis or playback fails, THEN THE Voice_Pipeline SHALL log the failure at warning level, skip the spoken confirmation, and resume normal operation without interrupting subsequent command processing

### Requirement 9: Ollama Configuration

**User Story:** As the bot operator, I want the Ollama endpoint and model to be configurable via the credential store, so that I can point the bot to different Ollama instances or models without code changes.

#### Acceptance Criteria

1. THE Ollama_Client SHALL read the Ollama API endpoint from the Credential_Store using key `ollama.url` at initialization time and on each request
2. THE Ollama_Client SHALL read the model name from the Credential_Store using key `ollama.model` at initialization time and on each request
3. WHEN `ollama.model` is not configured or is empty, THE Ollama_Client SHALL default to "gemma4"
4. WHEN `ollama.url` is not configured or is empty, THE Ollama_Client SHALL default to "http://localhost:11434"
5. THE Ollama_Client SHALL use the Ollama `/api/chat` endpoint with the configured model for all intent extraction requests, sending a POST request with JSON body containing model, messages array, and stream set to false

### Requirement 10: Admin Command Routing

**User Story:** As a server administrator, I want admin commands (mute, kick, ban, timeout, revoke, restart, shutdown) to still require verbal confirmation, so that destructive actions are not executed accidentally.

#### Acceptance Criteria

1. WHEN the Intent_Extractor classifies a command as an admin action (mute, kick, ban, timeout, revoke, restart, shutdown), THE Orchestrator SHALL transition to the CONFIRM state, speak a TTS prompt describing the pending action, and wait up to 15 seconds for a verbal confirmation or denial from the requesting user's SSRC
2. WHEN the user speaks a confirmation phrase ("yes", "confirm", "do it", "go ahead") within the 15-second window, THE Orchestrator SHALL execute the admin action and provide a TTS confirmation of the result
3. IF the user speaks a denial phrase ("no", "cancel", "nevermind", "stop") or the 15-second confirmation window expires without recognized speech, THEN THE Orchestrator SHALL discard the pending admin action, notify the user via TTS that the command was cancelled, and return to IDLE state
4. THE Intent_Extractor SHALL classify commands by setting the action field of the Command_Object to one of the admin action types (mute, kick, ban, timeout, revoke, restart, shutdown), allowing the Orchestrator to distinguish admin actions from music/playback actions by action type
5. WHEN a compound utterance mixes admin and non-admin commands, THE Orchestrator SHALL execute non-admin commands immediately in sequence and then enter the CONFIRM state individually for each admin command in order

### Requirement 11: LLM Response Schema Validation

**User Story:** As the bot operator, I want the LLM response to be validated against the expected schema, so that malformed responses do not crash the pipeline.

#### Acceptance Criteria

1. WHEN the Ollama gemma4 model returns a response, THE Intent_Extractor SHALL validate that the response is a JSON array and that each element conforms to the Command_Object schema (action: string, source: string|null, query: string|null, arguments: object|null) before passing commands to the Orchestrator
2. IF the LLM response fails schema validation as a whole (not valid JSON, not an array, or empty array), THEN THE Intent_Extractor SHALL fall back to keyword-based classification using the original STT transcript and log the malformed response at WARNING level
3. THE Intent_Extractor SHALL attempt to extract valid JSON from the LLM response by stripping markdown code fences (``` and ```json) and any non-JSON preamble or trailing text before validation
4. IF individual Command_Objects within an otherwise valid JSON array fail schema validation, THEN THE Intent_Extractor SHALL discard only the invalid elements, pass the remaining valid Command_Objects to the Orchestrator, and log each discarded element at WARNING level
