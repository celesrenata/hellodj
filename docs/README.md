# HelloDJ Documentation

Voice-activated Discord music bot with multi-source playback, video streaming, and intelligent voice commands.

## Table of Contents

| Document | Description |
|----------|-------------|
| [Architecture](./architecture.md) | System architecture, pod layout, data flow |
| [Bot Core](./bot-core.md) | Entry point, startup sequence, background tasks |
| [Playback Engine](./playback-engine.md) | Queue system, track resolution, session persistence |
| [Voice Pipeline](./voice-pipeline.md) | Wake word, STT, LLM intent, TTS response cycle |
| [Video Streaming](./video-streaming.md) | Activity backend, HLS transcoding, lyrics overlay |
| [Unified Playback](./unified-playback.md) | Router, multi-instance orchestrator, session registry |
| [Configuration](./configuration.md) | Credential store, config accessor, secrets management |
| [Kubernetes Deployment](./kubernetes.md) | Pod structure, services, deployment, troubleshooting |
| [Authentication & Security](./auth-security.md) | YouTube OAuth, PoToken, Tidal refresh, guild policy |
| [Commands Reference](./commands.md) | All slash commands with descriptions |
| [Development Guide](./development.md) | Local dev, Docker, testing, feature workflow |

## Quick Start

```bash
# Local development
cd bot/
cp .env.example .env  # Fill in DISCORD_TOKEN, HELLODJ_DB_KEY
docker-compose up

# Kubernetes deployment
cd kube/
kubectl apply -k .
```

## Project Layout

```
hellodj/
├── bot/                    # Python Discord bot (entry: bot.py)
│   ├── cogs/              # Discord slash command cogs
│   ├── voice/             # Wake word + voice activation pipeline
│   ├── video/             # Video streaming + Activity backend
│   ├── playback/          # Unified multi-instance playback system
│   ├── player.py          # Core playback engine (queue, resolution)
│   ├── session.py         # Per-guild session persistence
│   ├── credentials.py     # Encrypted SQLite credential store
│   ├── config.py          # Unified configuration accessor
│   └── render_lavalink_config.py  # Init container config renderer
├── web-ui/                # Admin portal (Flask)
├── kube/                  # Kubernetes manifests (kustomize)
├── training/              # Wake word model training
├── tidal-stream/          # Direct Tidal streaming sidecar
├── spotify-stream/        # Direct Spotify streaming sidecar
└── docs/                  # This documentation
```

## Current Status (Feature Freeze — 2026-08-21)

All features merged to master. Active systems:
- Multi-source audio playback (YouTube, Spotify, Tidal, SoundCloud)
- Voice activation with custom "Hello DJ" wake word (ONNX)
- LLM-powered voice intent extraction (Ollama gemma4)
- Video streaming via Discord Activity (HLS + QSV transcoding)
- Synced lyrics overlay (LRCLIB + Genius providers)
- Music video resolution (unified queue integration)
- Whiteboard overlay (collaborative drawing during video)
- Multi-instance orchestrator (multiple voice channels per guild)
- Content filtering and user ban system
- Guild authorization policy (admin approval required)
- Direct stream sidecars (Tidal/Spotify bypass YouTube mirroring)
