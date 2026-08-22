# Bot Core

## Entry Point: `bot.py`

The bot entry point handles startup sequencing, cog loading, background tasks, and global event handlers.

## Startup Sequence

```
1. Import modules (optional: unified playback with graceful ImportError handling)
2. Load .env (python-dotenv)
3. Configure logging (console + rotating file at /app/config/bot.log)
4. Create Bot instance (intents: default + message_content + members + presences)
5. Initialize unified playback components (SessionRegistry, Orchestrator, Router)
6. setup_hook() executes:
   a. Load all data stores (storage, session, oauth, blacklist, allowlist, guild_settings, sleep, metrics, guild_policy)
   b. Cleanup old uploaded files + stale video temp files
   c. Connect to Lavalink (poll REST API with 30 retries)
   d. Push YouTube OAuth + PoToken to Lavalink (single POST /youtube)
   e. Wait 5s for token exchange
   f. Push PoToken (now a no-op — combined in step d)
   g. Refresh Tidal token + push to LavasRC
   h. Load cogs in order:
      - music, playlists, filters, autoplay, admin, lyrics, info, help, radio, voice, stream
      - video (optional, non-fatal)
      - Wire lyrics service into track-start callback
      - playback (optional, requires unified modules)
      - admin_panel (optional)
   i. Install voice debug raw listeners
   j. Sync slash commands (global first, per-guild fallback on 50240 Entry Point conflict)
7. on_connect() → start gateway health watchdog
8. on_ready() → log in, recheck guild policy, sync per-guild commands, resume sessions, start background tasks
```

## Background Tasks

| Task | Interval | Purpose |
|------|----------|---------|
| `_token_refresh_watchdog` | 5 min | Refresh Tidal token, re-push YouTube OAuth+PoToken |
| `_potoken_refresh_task` | 1 hour | Fetch fresh PoToken from bgutil server |
| `_gateway_health_watchdog` | 30s checks | Detect READY stalls, force-reconnect, escalate to restart |
| `_guild_policy_watchdog` | 1 hour | Expire pending guilds, re-check authorization |
| `_orchestrator_health_loop` | 30s | Multi-instance health checks |

## Global Permission Check

Every interaction passes through `permission_check()`:

```python
1. Allow /activate command through unconditionally
2. Check guild activation (guild.<id>.activated == "true")
3. Check guild authorization (guild_policy.is_authorized)
4. Apply mode-based restriction:
   - "allow_all" mode: only allowlisted users
   - default mode: block blacklisted users
```

## Session Resume

On `on_ready()`, the bot iterates `session.all()` and resumes sessions where:
- `auto_resume == True`
- Guild is activated
- Voice channel has non-bot members present
- Key format is legacy (not composite `guild_id:channel_id` — those are handled by unified persistence)

## Event Handlers

| Event | Handler |
|-------|---------|
| `on_wavelink_track_start` | Update now-playing, fire visualizer/lyrics callbacks |
| `on_wavelink_track_end` | Advance queue, handle repeat modes |
| `on_wavelink_track_exception` | Retry with backoff (up to 3 attempts) |
| `on_message` | File upload playback detection |
| `on_guild_join` | Guild policy check (approve/pending/deny) |
| `on_guild_remove` | Clear policy, update guild list |
| `on_error` | Global error handler (CommandNotFound → refresh hint) |

## Cog Loading Order

The loading order matters because some cogs depend on others being loaded first:

1. **music** — Core playback commands (must be first for player wiring)
2. **playlists** — Playlist management
3. **filters** — Audio filter commands
4. **autoplay** — Auto-play when queue empties
5. **admin** — Admin commands (/blacklist, /allowlist, /mode)
6. **lyrics** — Lyrics display commands
7. **info** — /nowplaying, /queue info
8. **help** — Help command
9. **radio** — Radio station support
10. **voice** — Wake word + voice activation (requires player module)
11. **stream** — Tidal video streaming (/stream)
12. **video** — Activity-based video (optional, non-fatal)
13. **playback** — Unified playback router cog (optional, requires unified modules)
14. **admin_panel** — /hellodj command group (optional)

## File Upload Playback

The bot listens for message attachments and auto-plays audio/video files when:
- Bot is @mentioned, OR
- Message is in the player's bound text channel, OR
- Guild has a configured file_autoplay_channel

Files go through `file_handler.process_upload()` which detects media type, downloads to a temp location, and queues for playback.
