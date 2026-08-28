---
name: hellodj-evidence-debugging
description: Evidence-based debugging methodology for HelloDJ. Never speculate about causes — every claim about system behavior must be backed by evidence from logs, config files, or runtime state. Use when debugging the bot, Lavalink, or web-ui by reproducing errors, reading exact logs, tracing code paths, and verifying hypotheses before claiming a fix.
---

# Evidence-Based Debugging

## When to use

- Debugging any HelloDJ runtime issue (bot, Lavalink, web-ui) where the cause is unknown
- Reviewing claims about system behavior that need verification
- When you're about to speculate instead of investigate

## When NOT to use

- Building/deploying images (use `hellodj-deployment` / `hellodj-session-context`)
- Visual/Playwright website debugging (use `hellodj-visual-debug` / `hellodj-website-debug`)
- Understanding architecture (use `hellodj-architecture`)

## Core rule

Never speculate about causes. Every claim about system behavior MUST be backed by evidence from logs, config files, or runtime state. If you haven't verified it, say "I don't know yet, let me check" and go check.

## Banned phrases without evidence

These signal guessing instead of investigating:
- "probably" — go look at the actual logs/config
- "likely" — verify with kubectl logs / grep / runtime check
- "usually" — cite the docs or test it
- "known issue" — link to the actual issue/PR or show the error message that proves it
- "should work" — prove it by checking the output
- "might be" — investigate and confirm before speaking

## Debugging workflow

1. **Reproduce** — get the exact error from `kubectl logs` or runtime output
2. **Read the actual error** — quote the exact message, don't paraphrase
3. **Trace the code path** — follow the error through the actual source code
4. **Verify your hypothesis** — check config/state/logs to confirm before claiming a cause
5. **Test the fix** — after applying a change, check logs to confirm it worked

## Lavalink/HelloDJ specifics

- The Lavalink sidecar reads its config from the rendered `application.yml` (produced by the init container via `render_lavalink_config.py`), NOT from `bot/lavalink/application.yml` in the Docker image.
- Always check `kubectl logs -c lavalink` for the actual plugin initialization and client errors.
- Always check `kubectl logs -c bot` for the Python-side resolve/playback events.
- After any config change: apply configmap, restart deployment, then CHECK THE LOGS to confirm the change took effect.
- The `clients:` list in the youtube-source config maps display names differently (TV → TVHTML5, MUSIC → WEB_REMIX).

## Anti-patterns

- Do NOT chain multiple untested config changes in one deploy.
- Do NOT assume a provider/client works because docs say it should — check the runtime log.
- Do NOT keep retrying the same broken approach — if something fails twice, step back and investigate the root cause from logs.
- Do NOT make claims about what YouTube/SoundCloud/Spotify "currently" does without evidence from THIS deployment's logs.
