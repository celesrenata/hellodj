# Implementation Plan

## Overview

This plan implements the Unified Playback system for HelloDJ — consolidating music and video commands under a single routing layer, re-keying sessions by (guild_id, channel_id), adding multi-instance music orchestration, and deprecating legacy `/video` commands.

## Tasks

- [ ] 1. Create ContentClassifier module (`bot/playback/classifier.py`) — Implement `ContentType` enum, `ClassificationResult` dataclass, and `classify()` function with priority-ordered rules: explicit mode → attachment MIME → YouTube Music → Spotify → Tidal video path → Tidal audio → SoundCloud → video extension → YouTube default audio → unknown URL default video → plain text default audio. Handle edge cases (empty, whitespace, long URLs, unusual schemes). **Reqs: 3.1–3.10**
- [ ] 2. Create SessionRegistry with composite key (`bot/playback/session_registry.py`) — Implement `ChannelSession` dataclass and `SessionRegistry` class keyed by `(guild_id, channel_id)` tuple. Methods: `register()`, `unregister()`, `get()`, `get_by_guild()`, `get_audio_sessions()`, `get_video_sessions()`, `active_keys()`. Include grace period management with asyncio.Task per composite key. **Reqs: 4.1–4.5, 7.1**
- [ ] 3. Create PlaybackRouter (`bot/playback/router.py`) — Central dispatch: `_resolve_user_channel()` extracts channel from interaction, `_resolve_session()` looks up by composite key. Implement `play()` (classify → constraint check → create/enqueue), `skip()`, `stop()`, `pause()`, `queue()`, `clear()`. Dual-session tie-breaking by `started_at` timestamp. Error responses: not in VC, no session, audio busy in another channel. **Reqs: 1.1–1.6, 2.1–2.9, 5.1–5.5, 7.1–7.6**
- [ ] 4. Create PlaybackCog — unified command surface (`bot/cogs/playback.py`) — Register `/play` (query + optional mode + optional attachment), `/skip`, `/stop`, `/pause`, `/queue`, `/clear`. Each delegates to PlaybackRouter. Wire into bot.py cog loading. **Reqs: 1.1–1.6, 2.1–2.9**
- [ ] 5. Create unified queue display — Embed builder with 🎵/🎬 prefix per item, title truncation (100 chars), duration format (M:SS / H:MM:SS / "Live"), pagination (10/page, disabled buttons at boundaries), dual-queue mode for simultaneous audio+video sessions. Wire into PlaybackRouter.queue(). **Reqs: 8.1–8.6**
- [ ] 6. Create InstanceOrchestrator (`bot/playback/orchestrator.py`) — `BotInstance` dataclass, `InstanceOrchestrator` class. Load credentials from store (`instance.<N>.token/app_id/name`). `assign_instance()` picks first available, `release_instance()` frees within 5s, `health_check()` marks unhealthy after 10s timeout. Shared Lavalink node across all instances. **Reqs: 6.1–6.8**
- [ ] 7. Integrate InstanceOrchestrator with PlaybackRouter — On audio play: check existing instance in channel → use primary if available → call assign_instance() → error if all occupied. On stop: release_instance(). Inactivity timeout (5 min no humans) triggers release. **Reqs: 5.1, 5.3, 5.5, 6.2–6.5**
- [ ] 8. Create session persistence with composite keys (`bot/playback/persistence.py`) — `save_session()`, `load_all()`, `clear_session()`, `migrate_legacy()`. JSON key format `"guild_id:channel_id"`. Add `session_type` and `bot_instance_index` fields. Migrate legacy guild_id-only keys using stored voice_channel_id. Handle missing voice_channel_id (skip with warning). Restore audio sessions with auto_resume. Mark suspended on failure. Skip video auto-resume. **Reqs: 10.1–10.7**
- [ ] 9. Modify VideoCog for legacy deprecation — Check `cfg("playback.legacy_video_enabled")` in each `/video` subcommand. If enabled: execute via PlaybackRouter + append deprecation notice. If guild has immediate migration: reject with ephemeral unified equivalent. If globally disabled: reject all with replacement listing. **Reqs: 9.1–9.5**
- [ ] 10. Refactor Music cog to delegate to PlaybackRouter — Remove conflicting command registrations (/play group, /skip, /stop, /pause, /clear, /queue). Keep internal helpers callable from router. Preserve /join, /leave, /source, /remote, /sleep, /chime, /continue, /add, /seek, /volume. Ensure autoplay, crossfade, filters, session resume, chimes still work. **Reqs: 1.1–1.6, 2.1–2.9**
- [ ] 11. Refactor video SessionRegistry usage to composite key — Update all `self._registry.get(guild_id)` calls in video.py to `get(guild_id, channel_id)`. Update `_now_playing_messages`, `_seek_bar_tasks`, `_activity_urls` to use composite key tuples. Update `on_voice_state_update` and ActivityBackend WebSocket hub routing. Update HLS cleanup tracking. **Reqs: 4.1–4.4**
- [ ] 12. Wire everything together in bot.py — Instantiate classifier, registry, orchestrator, router at startup. Pass router to PlaybackCog and VideoCog. Call orchestrator.initialize() in on_ready. Start health_check background task. Integrate new persistence load_all with migration into session restore flow. **Reqs: All (integration)**
- [ ] 13. Add credential store keys for multi-instance — Admin command or config mechanism to store `instance.<N>.token/app_id/name`. Add `playback.instance_count` and `playback.legacy_video_enabled` config keys. Document Discord Developer Portal setup (creating additional bot apps, verification, guild invites). **Reqs: 6.6, 9.1**
- [ ] 14. Write property-based tests for ContentClassifier — `tests/test_classifier.py`: Properties 3–6 (audio domain, video indicators, default audio, unknown URL default video). Minimum 100 iterations per property using hypothesis. **Reqs: 3.1–3.10 (validation)**
- [ ] 15. Write tests for SessionRegistry, PlaybackRouter, and other components — `tests/test_session_registry.py` (Property 7), `tests/test_router_dispatch.py` (Properties 1, 2), `tests/test_channel_exclusivity.py` (Properties 8–10), `tests/test_orchestrator.py` (Properties 11, 12), `tests/test_tie_breaking.py` (Property 13), `tests/test_queue_display.py` (Properties 14, 15), `tests/test_persistence.py` (Properties 18–20), `tests/test_legacy_deprecation.py` (Properties 16, 17). **Reqs: All (validation)**

## Task Dependency Graph

```json
{
  "waves": [
    [1, 2, 6, 8, 13],
    [3, 14],
    [4, 5, 7, 9, 11],
    [10],
    [12],
    [15]
  ]
}
```

## Notes

- Tasks 1, 2, 6, 8, and 13 have no dependencies and can be started in parallel.
- Task 3 (Router) requires tasks 1 and 2 to be complete.
- Task 4 (PlaybackCog) requires task 3.
- Task 7 (Orchestrator integration) requires tasks 3 and 6.
- Task 10 (Music cog refactor) requires task 4.
- Task 12 (final wiring) requires tasks 4, 5, 7, 8, 9, 10, 11.
- Task 15 (all tests) requires task 12 for integration tests but property tests (14) can run after task 1.
- The InstanceOrchestrator (tasks 6, 7, 13) can be developed last as an enhancement — the unified commands work without it (single-instance mode with the same-channel constraint).
