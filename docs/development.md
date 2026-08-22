# Development Guide

## Prerequisites

- Python 3.11+
- Docker (for building images)
- kubectl + kube config pointing to the gremlin cluster
- Access to `registry.celestium.life` (Harbor)

## Local Development

### Setup

```bash
cd bot/
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-core.txt
pip install -r requirements-torch.txt  # Optional: heavy AI stack
pip install -r requirements-ai.txt     # Optional: STT/TTS

# Create .env from template
cp .env.example .env
# Fill in: DISCORD_TOKEN, HELLODJ_DB_KEY (at minimum)
```

### Running Locally

```bash
# With docker-compose (includes Lavalink sidecar)
docker-compose up

# Direct Python (requires external Lavalink)
python bot.py
```

### Environment for Local Dev

Key env vars for local development:
```env
DISCORD_TOKEN=your-bot-token
HELLODJ_DB_KEY=any-passphrase-for-encryption
LAVALINK_HOST=localhost
LAVALINK_PORT=2333
LAVALINK_PASSWORD=youshallnotpass
VOICE_ENABLED=false
HELLODJ_DEBUG=1
HELLODJ_DEBUG_MODULES=*
```

## Docker Build

```bash
cd bot/

# Build
docker build -t registry.celestium.life/hellodj/bot:<tag> .

# Push
docker push registry.celestium.life/hellodj/bot:<tag>

# Update kustomization.yaml with new tag
# Update init container image in deployment.yaml (hardcoded)
```

### Layer Strategy

The Dockerfile splits requirements into layers for faster pushes:
1. System deps + FFmpeg 9 build (cached, ~5min first build)
2. `requirements-core.txt` — Discord, wavelink, crypto, boto3
3. `requirements-torch.txt` — PyTorch + CUDA (~3GB)
4. `requirements-ai.txt` — faster-whisper, kokoro, librosa

Only the source COPY layer changes on code edits.

## Deployment Workflow

```bash
# 1. Make code changes
# 2. Build + push image
cd bot/
docker build -t registry.celestium.life/hellodj/bot:my-feature .
docker push registry.celestium.life/hellodj/bot:my-feature

# 3. Update manifests
# Edit kube/kustomization.yaml: newTag → my-feature
# Edit kube/deployment.yaml init container image (if changed)

# 4. Deploy
cd kube/
kubectl apply -k .

# 5. Watch rollout
kubectl rollout status deployment/hellodj -n hellodj-service

# 6. Check logs
kubectl logs -n hellodj-service deployment/hellodj -c bot -f
```

## Testing

```bash
cd bot/

# Run unit tests
python -m pytest tests/ -v

# Run specific test
python -m pytest tests/test_persistence.py -v

# Property-based tests (hypothesis)
python -m pytest tests/ -v --hypothesis-seed=0
```

Test files are colocated with modules:
- `playback/test_content_filter.py`
- `playback/test_user_bans.py`
- `video/test_*.py` (multiple test modules)

## Debug Framework

The bot has a built-in debug framework (`debug.py`):

```python
from debug import get_debug_logger

dbg = get_debug_logger("module_name")
dbg.info("message")
dbg.event("event_name", key=value, ...)
dbg.error("error: %s", exc)
```

### Configuration

| Env Var | Default | Purpose |
|---------|---------|---------|
| `HELLODJ_DEBUG` | 1 | Master switch (1=on, 0=off) |
| `HELLODJ_DEBUG_MODULES` | * | Comma-separated module filter |
| `HELLODJ_DEBUG_LEVEL` | DEBUG | Minimum level (DEBUG/INFO/WARNING) |
| `HELLODJ_DEBUG_TRACE` | 0 | Function entry/exit tracing |

### Available Modules

player, bot, tidal, music, voice_cmd, session, radio, stream, filters, autoplay, audio_pipeline, wakeword, stt, tts, intent, query, persistence

## Project Conventions

### Code Style
- Python 3.11+ (typing, match statements OK)
- asyncio throughout (no blocking calls in bot loop)
- Fire-and-forget pattern for non-critical callbacks
- All exceptions caught at module boundaries (audio independence)

### Import Pattern
```python
import player           # Direct module import (not from bot.player)
from config import cfg  # Config accessor
from credentials import creds  # Credential store
from debug import get_debug_logger  # Debug logging
```

### Error Handling
- Non-fatal features: wrap in try/except, log warning, continue
- Audio pipeline: NEVER let exceptions propagate from callbacks
- Network calls: always use timeouts, log failures, graceful fallback
- Track resolution: retry with backoff before advancing queue

### Session Persistence
- Auto-save on every queue change (session.save_guild)
- Two formats coexist: legacy (guild_id keys) and unified (composite keys)
- Video sessions are NOT auto-resumed (they require Activity launch)

## Feature Branches

Convention: `feat/<feature-name>` branches. Merged to `master` via merge commits.

Current feature freeze means: fix bugs only, no new features.

## Lavalink Plugin Development

Custom plugins are in `kube/lavalink/plugins/`:
- `youtube-plugin-sabr.jar` — SABR-capable YouTube plugin
- `lavasrc-plugin-4.8.3.jar` — LavasRC (Spotify/Tidal/Deezer sources)

Built from:
- `celesrenata/Lavalink` repo (branch `dev`)
- `celesrenata/lavaplayer` repo
- `celesrenata/LavaSrc` repo
- `celesrenata/youtube-source` repo

The Lavalink image is built from `kube/lavalink/Dockerfile` which layers plugins onto the official Lavalink v4 base.
