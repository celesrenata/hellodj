# Implementation Plan: Unified Queue Pipeline

## Overview

Audit and fix all callers of `_play_next_from_queue` to ensure they acquire the per-guild `asyncio.Lock` before mutating queue state. Route legacy video skip through `unified_controls`, remove stale suppression flags, and validate correctness with property-based and unit tests. The lock infrastructure already exists — this is a completion and hardening pass.

## Tasks

- [x] 1. Lock coverage for auto-start paths in player.py
  - [x] 1.1 Wrap `add_track` auto-start path with queue lock
    - In `bot/player.py`, locate the branch in `add_track` where the player is idle and playback auto-starts
    - Acquire `_get_queue_lock(guild_id)` before calling `_play_next_from_queue`
    - Ensure the lock is only held around the advancement call, not the entire enqueue operation
    - _Requirements: 6.8, 1.2, 1.3_

  - [x] 1.2 Wrap `enqueue_and_start` auto-start path with queue lock
    - In `bot/player.py`, locate the branch in `enqueue_and_start` that calls `_play_next_from_queue`
    - Acquire `_get_queue_lock(guild_id)` before calling `_play_next_from_queue`
    - Verify no nested lock acquisition (caller must not already hold the lock)
    - _Requirements: 6.8, 1.2, 1.3, 1.6_

  - [ ]* 1.3 Write unit tests for auto-start lock acquisition
    - Test that `add_track` with idle player acquires lock before advancing
    - Test that `enqueue_and_start` acquires lock before advancing
    - Test that enqueueing without auto-start does NOT acquire the lock
    - _Requirements: 6.8, 1.2_

- [x] 2. Lock coverage for unified_remote _video_skip
  - [x] 2.1 Wrap `_video_skip` else-branch with queue lock in unified_remote.py
    - In `bot/views/unified_remote.py`, locate the `_video_skip` method's else-branch that calls `_play_next_from_queue` directly
    - Acquire `_get_queue_lock(guild_id)` before calling `_play_next_from_queue`
    - Ensure the lock is acquired BEFORE any `player.stop()` call (to prevent `on_track_end` racing)
    - _Requirements: 6.1, 1.2, 3.3_

  - [ ]* 2.2 Write unit test for _video_skip lock acquisition
    - Test that `_video_skip` acquires lock before advancing queue
    - Test that `on_track_end` is suppressed when `_video_skip` holds the lock
    - _Requirements: 6.1, 3.3_

- [x] 3. Route video_skip through unified_controls
  - [x] 3.1 Refactor `video_skip` in `bot/cogs/video.py` to use `unified_controls.unified_skip`
    - Replace the inline queue advancement logic in `video_skip` with a call to `unified_controls.unified_skip`
    - Remove any direct calls to `_play_next_from_queue` from this method
    - Preserve the existing behavior for the queue-empty case (already uses lock)
    - _Requirements: 6.5, 3.1, 3.2_

  - [ ]* 3.2 Write unit test for video_skip routing
    - Test that `video_skip` delegates to `unified_controls.unified_skip`
    - Test that no direct `_play_next_from_queue` call exists in the cog method
    - _Requirements: 6.5_

- [x] 4. Checkpoint — Verify lock coverage audit
  - Ensure all tests pass, ask the user if questions arise.
  - Grep the codebase for any remaining direct calls to `_play_next_from_queue` that are NOT under the queue lock
  - Confirm no nested lock acquisitions exist (deadlock check)

- [x] 5. Remove legacy suppression flags
  - [x] 5.1 Remove time-based guards and stale debounce logic
    - In `bot/player.py`, remove any 5s debounce or time-guard patterns that were used to prevent rapid-fire advances (the lock makes these unnecessary)
    - Keep `_video_transition` flag (semantic signal for video setup blocking audio)
    - Keep the `lock.locked()` non-blocking check in `on_track_end` (this IS the serialization mechanism)
    - _Requirements: 8.1, 1.4_

  - [ ]* 5.2 Write unit test verifying suppression removal doesn't break behavior
    - Test that rapid sequential skips each advance exactly once (lock serializes them)
    - Test that `on_track_end` still suppresses correctly via lock check
    - _Requirements: 8.1, 3.3_

- [ ] 6. Property-based tests for queue pipeline correctness
  - [ ]* 6.1 Write property test for mutual exclusion (Property 1)
    - **Property 1: Mutual Exclusion of Queue Operations**
    - **Validates: Requirements 1.2, 1.3, 1.4**
    - Use Hypothesis to generate random concurrent Queue_Operation pairs targeting the same guild
    - Verify operations execute sequentially (no interleaved state mutations)
    - Mock `_resolve_and_play` and `_start_video_from_queue` to avoid network calls

  - [ ]* 6.2 Write property test for advance-exactly-one invariant (Property 2)
    - **Property 2: Advance-Exactly-One Invariant**
    - **Validates: Requirements 3.1, 3.2**
    - Generate random non-empty queues (varying lengths, mixed audio/video entries)
    - Verify each advance removes exactly one non-blocked entry and sets it as current

  - [ ]* 6.3 Write property test for history push idempotence (Property 3)
    - **Property 3: History Push Idempotence**
    - **Validates: Requirements 7.1, 7.2**
    - Generate random queue states with varying history depths
    - Verify outgoing `current` appears in history exactly once after advancement
    - Verify history never exceeds 50 entries

  - [ ]* 6.4 Write property test for previous navigation correctness (Property 4)
    - **Property 4: Previous Navigation Correctness**
    - **Validates: Requirements 4.1, 4.2**
    - Generate random queue states with non-empty history
    - Verify previous sets current to most recent history entry
    - Verify outgoing current is placed at queue position 0

  - [ ]* 6.5 Write property test for on_track_end suppression (Property 5)
    - **Property 5: on_track_end Suppression Under Lock**
    - **Validates: Requirements 3.3, 1.4**
    - Simulate `on_track_end` arriving while lock is held
    - Verify handler returns without mutating state

  - [ ]* 6.6 Write property test for cross-media dispatch (Property 6)
    - **Property 6: Cross-Media Dispatch Correctness**
    - **Validates: Requirements 5.1, 5.2, 5.3, 5.4**
    - Generate mixed queues with audio and `music_video` entries in random order
    - Verify each advancement dispatches to correct backend
    - Verify video-to-audio transitions terminate video session first

  - [ ]* 6.7 Write property test for behavioral equivalence (Property 7)
    - **Property 7: Behavioral Equivalence (Single-Caller)**
    - **Validates: Requirements 8.1, 8.2, 8.3**
    - Generate random single Queue_Operations with no concurrency
    - Verify resulting state matches expected pre-refactor behavior (repeat modes, blocked track skipping)

  - [ ]* 6.8 Write property test for lock release on exception (Property 8)
    - **Property 8: Lock Release on Exception**
    - **Validates: Requirements 1.6**
    - Inject random exceptions during queue operations
    - Verify lock is always released after exception propagates

- [ ] 7. Integration tests
  - [ ]* 7.1 Write integration test for full skip sequence
    - Enqueue 5 tracks, skip through all, verify each becomes `current` in order
    - Verify history grows correctly
    - _Requirements: 3.1, 7.1_

  - [ ]* 7.2 Write integration test for concurrent skip simulation
    - Schedule 3 `unified_skip` calls on the same event loop tick
    - Verify only one advance happens per call (no double-advance)
    - _Requirements: 1.4, 3.3_

  - [ ]* 7.3 Write integration test for video-to-audio transition
    - Enqueue `[music_video, audio]`, advance, verify video session terminated before audio starts
    - _Requirements: 5.3, 5.5_

  - [ ]* 7.4 Write integration test for on_track_end suppression under lock
    - Hold lock manually, fire `on_track_end`, verify no state mutation
    - _Requirements: 3.3, 1.4_

- [x] 8. Final checkpoint — Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.
  - Run full test suite: `pytest tests/test_queue_pipeline_props.py tests/test_queue_pipeline_unit.py -v`
  - Verify no deadlock scenarios exist (no nested lock acquisitions across all modified files)

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- The lock infrastructure (`_queue_locks`, `_get_queue_lock`) already exists in `player.py` — no new primitives needed
- The design explicitly uses `async with lock:` for guaranteed release (Property 8)
- `on_track_end` uses the non-blocking `lock.locked()` pattern — this is intentional, NOT a bug
- `_video_transition` flag is KEPT — it's semantic (blocks audio during video setup), not a suppression hack
- Property tests use Hypothesis (already in the project) with minimum 100 iterations
- All mocks avoid network calls to Lavalink/Discord — tests are pure state-machine verification
- Key files: `bot/player.py`, `bot/playback/unified_controls.py`, `bot/views/unified_remote.py`, `bot/cogs/video.py`

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "2.1", "3.1"] },
    { "id": 1, "tasks": ["1.3", "2.2", "3.2", "5.1"] },
    { "id": 2, "tasks": ["5.2", "6.1", "6.2", "6.3", "6.4"] },
    { "id": 3, "tasks": ["6.5", "6.6", "6.7", "6.8"] },
    { "id": 4, "tasks": ["7.1", "7.2", "7.3", "7.4"] }
  ]
}
```
