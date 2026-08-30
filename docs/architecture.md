# Architecture

## System Overview

HelloDJ is a multi-container Discord bot deployed as a single Kubernetes pod on a home lab cluster (gremlin nodes, 10.1.1.12–15). The architecture separates concerns into cooperating containers that share a pod network namespace (localhost communication) and persistent volumes.

## Pod Layout

```
┌─────────────────────────────────────────────────────────────────────────┐
│  hellodj Pod (namespace: hellodj-service)                               │
│                                                                         │
│  ┌─────────────────┐   ┌─────────────────┐   ┌──────────────────────┐ │
│  │  init:           │   │  bot             │   │  lavalink            │ │
│  │  render-lavalink │──▶│  Python 3.11     │◀─▶│  Java (Lavalink v4)  │ │
│  │  -config         │   │  port 8090       │   │  port 2333           │ │
│  └─────────────────┘   │  (Activity HTTP)  │   │  (REST + WebSocket)  │ │
│                         └─────────────────┘   └──────────────────────┘ │
│                                                                         │
│  ┌─────────────────┐   ┌─────────────────┐                            │
│  │  tidal-stream    │   │  spotify-stream  │                            │
│  │  port 8801       │   │  port 8802       │                            │
│  │  (direct audio)  │   │  (proxy stream)  │                            │
│  └─────────────────┘   └─────────────────┘                            │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────┐   ┌──────────────────────────┐
│  yt-cipher       │   │  potoken-server           │
│  port 8001       │   │  port 4416                │
│  (YouTube sig)   │   │  (bgutil Proof-of-Origin) │
└─────────────────┘   └──────────────────────────┘

┌─────────────────┐
│  hellodj-web-ui  │
│  port 8080       │
│  (Admin portal)  │
└─────────────────┘
```

## Container Responsibilities

| Container | Image | Role |
|-----------|-------|------|
| `render-lavalink-config` (init) | bot image | Reads encrypted SQLite → renders `application.yml` |
| `bot` | `registry.celestium.life/hellodj/bot` | Discord bot, Activity HTTP server (8090) |
| `lavalink` | `registry.celestium.life/hellodj/lavalink` | Audio source resolution, streaming, filters |
| `tidal-stream` | `registry.celestium.life/hellodj/tidal-stream` | Direct Tidal audio streaming |
| `spotify-stream` | `registry.celestium.life/hellodj/spotify-stream` | Direct Spotify audio via librespot |

### Separate Deployments

| Deployment | Image | Role |
|-----------|-------|------|
| `yt-cipher` | `ghcr.io/kikkia/yt-cipher` | Remote YouTube signature deciphering |
| `potoken-server` | `brainicism/bgutil-ytdlp-pot-provider` | Fresh YouTube Proof-of-Origin tokens |
| `hellodj-web-ui` | `registry.celestium.life/hellodj/web-ui` | Flask admin portal |

## Data Flow

### Audio Playback

```
User: /play "song name"
  │
  ▼
PlaybackRouter (classify content)
  │
  ├─ audio? ──▶ Music Cog._play_song()
  │                │
  │                ▼
  │            player._resolve_and_play()
  │                │
  │                ├─ Spotify/Tidal URL? ──▶ stream_resolver ──▶ sidecar (8801/8802)
  │                │                              │
  │                │                              ▼
  │                │                         Direct CDN URL ──▶ Lavalink HTTP source
  │                │
  │                └─ Other? ──▶ Lavalink search (wavelink 3.5)
  │                                  │
  │                                  ├─ YouTube (TV OAuth + SABR)
  │                                  ├─ Tidal (LavasRC tdsearch:)
  │                                  ├─ Spotify (LavasRC spsearch:)
  │                                  └─ SoundCloud (scsearch:)
  │
  └─ video? ──▶ Video Cog.video_play()
                    │
                    ▼
              Source Resolution (YouTube/Tidal/Upload)
                    │
                    ▼
              HLS Transcode (FFmpeg + QSV)
                    │
                    ▼
              Activity Backend (port 8090)
                    │
                    ▼
              Discord Activity iframe
```

### Voice Activation

```
Voice Channel Audio (Opus frames)
  │
  ▼
PipelineSink (discord.ext.voice_recv)
  │
  ▼
VoiceCommandOrchestrator.on_voice_receive()
  │
  ▼
AudioPipeline (decode Opus → mel-spectrogram)
  │
  ▼
WakeWordModel.predict() [every 80ms]
  │
  ├─ Not detected → discard
  │
  └─ Detected! → Accumulate speech → STT (faster-whisper / cloud)
                      │
                      ▼
                LLMIntentExtractor.extract() [Ollama gemma4]
                      │
                      ▼
                Command_Objects [{action, source, query, arguments}]
                      │
                      ├─ Music command → player.py
                      ├─ Admin command → (permission check)
                      └─ General query → QueryHandler (LLM + MCP tools)
                                              │
                                              ▼
                                         TTS response → voice channel
```

## Storage Architecture

| Volume | Type | Mount | Purpose |
|--------|------|-------|---------|
| `hellodj-data-pvc` | Longhorn 1Gi | `/app/data` | SQLite DB, sessions, oauth, playlists |
| `hellodj-config-pvc` | NFS (Synology) | `/app/config` | Bot logs |
| `hellodj-models-pvc` | NFS (Synology) | `/app/models` | Hello_DJ.onnx wake word model |
| `hellodj-config-backups` | emptyDir | `/app/config-backups` | Runtime config backups |
| `lavalink-config-rendered` | emptyDir | `/opt/Lavalink/application.yml` | Init container output |
| `hls-tmp` | emptyDir (Memory) 2Gi | `/tmp/hellodj_hls` | HLS segment scratch space |
| `nvidia.com/gpu` | device plugin | (container resource) | NVIDIA GPU for NVENC transcoding (AWS: time-sliced T4g on g5g.xlarge) |

## Network Architecture

```
Internet ──▶ Traefik Ingress ──▶ hellodj-web-ui:8080
              (hellodj.celestium.life)

Discord Gateway ◀──▶ bot (WebSocket)
Discord Voice   ◀──▶ bot (UDP, port negotiated)
Discord CDN     ◀──▶ bot Activity (HTTPS tunnel via Discord)

In-cluster DNS:
  hellodj.hellodj-service.svc.cluster.local:2333    (Lavalink)
  yt-cipher.hellodj-service.svc.cluster.local:8001  (yt-cipher)
  potoken-server.hellodj-service.svc.cluster.local:4416 (potoken)
  speaches.speaches-service.svc.cluster.local:8000  (TTS)
```

## Credential Store

All secrets live in an encrypted SQLite database (`/app/data/hellodj.db`). The encryption key (`HELLODJ_DB_KEY`) is the ONLY environment variable secret. Everything else is read from the database at runtime.

```
┌─────────────────────────────────────────┐
│  hellodj.db (Fernet-encrypted SQLite)    │
│                                          │
│  discord.token          → bot login      │
│  youtube.oauth_refresh  → TV client      │
│  youtube.pot_token      → PoToken        │
│  tidal.access_token     → LavasRC        │
│  tidal.refresh_token    → refresh cycle  │
│  spotify.client_id/sec  → LavasRC        │
│  ytcipher.api_token     → cipher auth    │
│  guild.<id>.activated   → activation key │
│  ...                                     │
└─────────────────────────────────────────┘
```

## Technology Stack

| Layer | Technology |
|-------|-----------|
| Bot Runtime | Python 3.11, discord.py 2.x, wavelink 3.5+ |
| Audio Server | Lavalink v4 (Java, custom SABR-enabled youtube plugin) |
| Video Transcode | FFmpeg 9 with NVIDIA NVENC (h264_nvenc); Graviton-tuned libx264 CPU floor |
| Wake Word | ONNX Runtime (custom trained model) |
| STT | faster-whisper (local) or cloud (AWS Transcribe) |
| TTS | Speaches (kokoro voices, in-cluster) |
| LLM | Ollama gemma4 (intent extraction) or OpenAI-compatible |
| Web UI | Flask (admin portal) |
| Storage | SQLite (encrypted), JSON files, NFS, Longhorn |
| Orchestration | Kubernetes (k3s), kustomize |
| Registry | Harbor (registry.celestium.life) |
| Ingress | Traefik + cert-manager (Let's Encrypt) |
