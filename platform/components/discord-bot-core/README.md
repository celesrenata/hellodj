# discord-bot-core

The `discord-bot-core` component of the HelloDJ AWS platform.

## Responsibility

- Own the Discord **gateway** connection (sharded, scales by shard count).
- **Cog / command registration** scaffolding.
- **Guild policy**: new guilds are pending until an administrator approves them
  via the web-ui admin portal; unapproved guilds are auto-left after expiry.
- **Background watchdogs**: Discord bot token refresh (from Secrets Manager) and
  gateway health monitoring.
- Reads the Discord bot token from **AWS Secrets Manager** (boto3, injectable).
- **Delegates all playback** to the `playback-orchestrator` over HTTP/JSON — this
  component contains no playback logic itself.

This is an independently deployable, independently versioned component
(Requirements 15.1, 15.3). It is its own Nix-built image with its own semantic
version and its own CI/CD path.

## Package layout

```
discord_bot_core/
├── __init__.py            # package version + public exports
├── config.py             # environment-driven runtime settings
├── secrets.py            # Secrets Manager token provider (injectable/mockable)
├── gateway/
│   ├── __init__.py
│   └── client.py         # discord.py Bot bootstrap + lifecycle
├── commands/
│   ├── __init__.py
│   ├── registry.py       # cog & command registration scaffolding
│   └── playback_cog.py   # thin command cog that delegates to the orchestrator
├── policy/
│   ├── __init__.py
│   └── guild_policy.py   # guild authorization state machine
├── playback/
│   ├── __init__.py
│   └── client.py         # typed HTTP/JSON client to playback-orchestrator
├── watchdogs/
│   ├── __init__.py
│   ├── base.py           # periodic background-task base class
│   ├── token_refresh.py  # refreshes the Discord token from Secrets Manager
│   └── gateway_health.py # detects gateway READY stalls and reconnects
└── main.py               # entry point wiring everything together
```

## Interfaces

- **Discord gateway** — outbound WSS via discord.py.
- **playback-orchestrator** — internal HTTP/JSON (typed client in
  `discord_bot_core.playback.client`).
- **AWS Secrets Manager** — reads the Discord bot token (typed provider in
  `discord_bot_core.secrets`).

## Development

```bash
# From the platform root:
uvx ruff@0.6.9 check components/discord-bot-core
python3 tools/check_line_count.py components/discord-bot-core

# discord.py / wavelink may not be installed in every environment; the modules
# are import-structured so syntax can be verified without the runtime deps:
python3 -m py_compile components/discord-bot-core/discord_bot_core/**/*.py
```
