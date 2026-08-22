# Implementation Plan: Wakeword Voice Pipeline (LLM Intent Extraction)

## Overview

Replace the keyword-based intent classifier with an Ollama gemma4 LLM-powered pipeline. Implementation proceeds bottom-up from pure validation functions through the HTTP client, intent extractor, orchestrator modifications, and finally the slash command UI layer. Each module is testable in isolation before integration.

## Tasks

- [x] 1. Create schema validator module
  - [x] 1.1 Implement `voice/schema_validator.py` with pure validation functions
    - Create `strip_json_fences()` to remove markdown code fences and extract JSON array boundaries
    - Create `validate_command_object()` to validate and normalize a single Command_Object dict (action required as non-empty string, source/query as optional strings, arguments defaults to `{}`)
    - Create `validate_command_objects()` to filter a list, discarding invalid elements and logging warnings
    - _Requirements: 11.1, 11.3, 11.4_

  - [ ]* 1.2 Write property tests for schema validator (Properties 10, 11, 12)
    - **Property 10: Command_Object Schema Validation** — For any dict, `validate_command_object` returns normalized Command_Object iff dict contains non-empty string `action`; otherwise returns None. Arguments defaults to `{}` when missing or non-dict.
    - **Property 11: Markdown Fence Stripping** — For any valid JSON array string S, wrapping in code fences and passing to `strip_json_fences` produces parseable JSON equal to the original.
    - **Property 12: Partial Validation Preserves Valid Elements** — For any list of mixed valid/invalid dicts, `validate_command_objects` returns exactly valid elements in original order.
    - **Validates: Requirements 11.1, 11.3, 11.4**

- [x] 2. Create Ollama HTTP client module
  - [x] 2.1 Implement `voice/ollama_client.py` with async HTTP client
    - Create `OllamaClient` class with lazy `aiohttp.ClientSession` management
    - Read endpoint from `cfg("ollama.url")` with default `http://localhost:11434`
    - Read model from `cfg("ollama.model")` with default `gemma4`
    - Implement `chat()` method: POST to `/api/chat` with system+user messages, `stream: false`, `format: "json"`, 10s timeout
    - Implement `close()` for session cleanup
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_

  - [ ]* 2.2 Write unit tests for Ollama client
    - Test default config values (`gemma4`, `http://localhost:11434`)
    - Test HTTP error handling (non-2xx returns None)
    - Test timeout behavior (asyncio.TimeoutError propagation)
    - Mock aiohttp responses for success/failure paths
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_

- [x] 3. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Create LLM intent extractor module
  - [x] 4.1 Implement `voice/llm_intent.py` with LLMIntentExtractor class
    - Define `RECOGNIZED_ACTIONS` frozenset (play, skip, pause, resume, stop, shuffle, remove, repeat, queue, join, leave, load_playlist, save_playlist + admin actions)
    - Define `ADMIN_ACTIONS` frozenset (mute, kick, ban, timeout, revoke, restart, shutdown)
    - Define `SOURCE_MAP` dict mapping spoken names to normalized identifiers
    - Define `SYSTEM_PROMPT` instructing gemma4 to return JSON Command_Object arrays
    - Implement `extract()`: empty transcript → empty list; call Ollama with 10s timeout; parse response content; strip fences; validate schema; filter unrecognized actions; normalize sources; truncate to 10 commands
    - Implement `_fallback()`: log and delegate to keyword `classify_intent`, convert legacy format to Command_Object list
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 4.1, 4.2, 4.5, 5.1, 11.2_

  - [ ]* 4.2 Write property test for whitespace transcript (Property 2)
    - **Property 2: Whitespace Transcript Produces Empty Commands** — For any string of only whitespace chars, `extract()` returns empty list without HTTP request.
    - **Validates: Requirements 3.7**

  - [ ]* 4.3 Write property test for unrecognized action filtering (Property 3)
    - **Property 3: Unrecognized Action Filtering** — For any JSON array where some elements have actions not in RECOGNIZED_ACTIONS, extractor returns only recognized ones in original order.
    - **Validates: Requirements 3.8**

  - [ ]* 4.4 Write property test for source mapping (Property 4)
    - **Property 4: Source Mapping Roundtrip** — For any key in SOURCE_MAP, lookup produces expected normalized identifier; for any string NOT in SOURCE_MAP keys/values, source is set to null.
    - **Validates: Requirements 4.2, 4.5**

  - [ ]* 4.5 Write property test for command array truncation (Property 6)
    - **Property 6: Command Array Truncation at 10** — For any Command_Object array with length > 10, extractor returns exactly the first 10 in order.
    - **Validates: Requirements 5.1**

- [x] 5. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Modify orchestrator for Command_Object array processing
  - [x] 6.1 Update `voice/voice_commands.py` to use LLMIntentExtractor
    - Import and instantiate `LLMIntentExtractor` in `VoiceCommandOrchestrator.__init__`
    - Replace `classify_intent` call in `_wait_for_speech_end` with `self.llm_intent.extract(transcript)`
    - Add `_process_commands()` method: iterate Command_Object array sequentially, route admin vs non-admin, halt on failure, build summary TTS
    - Add `_execute_command()` method: dispatch single Command_Object to action handler with per-command source override (use `cmd["source"]` or fall back to guild `source_provider`)
    - Ensure guild `source_provider` is NEVER mutated by source overrides
    - Add `_build_summary()` method: produce ≤150 char TTS summary for compound utterances
    - _Requirements: 3.1, 3.2, 4.3, 4.4, 5.2, 5.3, 6.1–6.11, 8.1, 8.2, 8.3, 10.4, 10.5_

  - [x] 6.2 Wire admin command routing through CONFIRM state
    - When Command_Object action is in ADMIN_ACTIONS, enter CONFIRM state with 15s verbal timeout
    - Non-admin commands in compound utterances execute immediately; admin commands trigger individual CONFIRM flows
    - Preserve existing `_confirm_and_execute` logic but adapt for Command_Object input
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5_

  - [ ]* 6.3 Write property test for TTS confirmation length (Property 8)
    - **Property 8: TTS Confirmation Length Bound** — For any CommandResult, generated TTS message has ≤150 characters.
    - **Validates: Requirements 8.1**

  - [ ]* 6.4 Write property test for admin action classification (Property 9)
    - **Property 9: Admin Action Classification** — For any Command_Object with admin action, orchestrator routes through CONFIRM; for non-admin action, executes immediately.
    - **Validates: Requirements 10.4**

  - [ ]* 6.5 Write property test for sequential execution halt on failure (Property 7)
    - **Property 7: Sequential Execution Halts on Failure** — For any command array of length N where command at index i fails, exactly i+1 commands attempted, rest skipped.
    - **Validates: Requirements 5.3**

  - [ ]* 6.6 Write property test for source override immutability (Property 5)
    - **Property 5: Source Override Does Not Mutate Guild State** — For any command with non-null source, guild `source_provider` unchanged after execution.
    - **Validates: Requirements 4.3, 4.4**

- [x] 7. Checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Add /wakeword slash command
  - [x] 8.1 Implement `/wakeword` command in `cogs/voice.py`
    - Add `_wakeword_guilds: set[int]` to `VoiceCog.__init__`
    - Register `/wakeword` as a separate slash command with `on|off` choice parameter
    - Require `Manage Guild` permission; respond with ephemeral error if insufficient
    - On "on": add guild to `_wakeword_guilds`, respond with ephemeral confirmation
    - On "off": discard guild from `_wakeword_guilds`, respond with ephemeral confirmation
    - Update `_should_listen()` to also check `_wakeword_guilds` membership (either `/wakeword on` OR `/voice enable` activates listening)
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7_

  - [ ]* 8.2 Write unit tests for /wakeword command
    - Test permission gate (non-admin gets error)
    - Test toggle state changes (on enables, off disables)
    - Test independence from /voice enable/disable
    - Test that enabling wakeword while bot not in voice still sets the flag
    - _Requirements: 1.1, 1.2, 1.3, 1.5, 1.6, 1.7_

- [x] 9. Integration wiring and playlist operations
  - [x] 9.1 Implement playlist voice commands in orchestrator
    - Add `load_playlist` action handler: load named playlist into queue, begin playback if queue was empty, report error with suggestions if not found
    - Add `save_playlist` action handler: validate name (1–64 chars), save current queue, confirm track count, handle name collision and empty queue
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

  - [x] 9.2 Wire LLMIntentExtractor lifecycle into cog setup/teardown
    - Call `llm_intent.close()` in `VoiceCommandOrchestrator` cleanup
    - Ensure aiohttp session is properly closed on cog unload
    - Add `ollama.url` and `ollama.model` to credential store documentation/key map in `config.py`
    - _Requirements: 9.1, 9.2_

  - [ ]* 9.3 Write integration tests for full pipeline (mocked Ollama)
    - Test STT transcript → LLM → Command_Object → action execution flow with mocked HTTP
    - Test fallback activation on timeout/connection error
    - Test compound utterance with mixed sources
    - Test admin CONFIRM flow end-to-end
    - Test TTS failure resilience (log warning, don't crash)
    - _Requirements: 3.4, 3.5, 5.2, 5.3, 8.4, 10.1, 10.2, 10.3_

  - [ ]* 9.4 Write property test for SSRC isolation (Property 1)
    - **Property 1: SSRC Isolation** — For any set of active SSRCs, wake word on SSRC X captures only SSRC X's ring buffer samples.
    - **Validates: Requirements 2.1, 2.3**

- [x] 10. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The keyword classifier (`voice/intent.py`) is preserved as fallback — it is NOT deleted
- Ollama runs on gremlin-1's NVIDIA GPU; the client reads endpoint from `cfg("ollama.url")`
- All new modules live under `bot/voice/` alongside existing pipeline code
- Test files go in `tests/` following existing project conventions

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1"] },
    { "id": 1, "tasks": ["1.2", "2.2"] },
    { "id": 2, "tasks": ["4.1"] },
    { "id": 3, "tasks": ["4.2", "4.3", "4.4", "4.5"] },
    { "id": 4, "tasks": ["6.1", "8.1"] },
    { "id": 5, "tasks": ["6.2", "6.3", "6.4", "6.5", "6.6", "8.2"] },
    { "id": 6, "tasks": ["9.1", "9.2"] },
    { "id": 7, "tasks": ["9.3", "9.4"] }
  ]
}
```
