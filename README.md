# HelloDJ

A voice-activated Discord music bot with a custom "Hello DJ" wake word, now fully Kubernetes-native with a web configuration UI.

## Project Structure

```
hellodj/
├── bot/              # Discord music bot (wavelink + Lavalink)
│   ├── bot.py        # Entry point
│   ├── player.py     # Shared playback engine
│   ├── session.py    # Guild session persistence
│   ├── storage.py    # Playlist storage
│   ├── blacklist.py  # Shared blacklist
│   ├── cogs/         # Discord command modules
│   └── lavalink/     # Lavalink config for Docker Compose
├── web-ui/           # Configuration web interface (Flask)
│   ├── app.py        # Flask backend
│   ├── templates/    # Jinja2 templates
│   ├── Dockerfile
│   └── requirements.txt
├── kube/             # Kubernetes manifests (kustomize)
│   ├── namespace.yaml
│   ├── configmap.yaml
│   ├── pvc.yaml
│   ├── nfs-pv.yaml       # NFS PV for config storage
│   ├── nfs-pvc.yaml      # NFS PVC for config storage
│   ├── service.yaml
│   ├── deployment.yaml
│   ├── web-ui-service.yaml
│   ├── web-ui-deployment.yaml
│   ├── ingress.yaml       # hellodj.celestium.life
│   └── kustomization.yaml
├── training/         # Custom wake word model training
└── docker-compose.yml
```

## Quick Start

### Docker Compose (local dev)

```bash
cd bot/
cp .env.example .env
# Edit .env with your Discord bot token and API keys

# From repo root:
docker compose up -d
```

### Kubernetes (production)

```bash
# Copy manifests to your kube directory
cp -r kube/ ~/sources/kube/HelloJD/

# Create secret (token and API keys)
kubectl create secret generic hellodj-secret \
  --namespace hellodj-service \
  --from-literal=DISCORD_TOKEN=your_token \
  --from-literal=SPOTIFY_CLIENT_ID=... \
  --from-literal=SPOTIFY_CLIENT_SECRET=... \
  --from-literal=GENIUS_API_KEY=...

# Apply manifests
kubectl apply -f ~/sources/kube/HelloJD/

# Build and push images
docker build -t registry.celestium.life/hellodj/bot:latest bot/
docker push registry.celestium.life/hellodj/bot:latest

docker build -t registry.celestium.life/hellodj/web-ui:latest web-ui/
docker push registry.celestium.life/hellodj/web-ui:latest

# Rollout
kubectl rollout restart deployment/hellodj -n hellodj-service
kubectl rollout restart deployment/hellodj-web-ui -n hellodj-service
```

### NFS Configuration Storage

Configurations are stored on NFS at:
```
nfs://192.168.42.8:/volume1/Kubernetes/HelloDJ/data
```

The web UI provides:
- Dashboard with guild/playlist/backup status
- Configuration editor (Discord token, Lavalink, Spotify, Genius)
- Guild session viewer
- Playlist inventory
- Backup creation and restore
- Blacklist management

## Bot Features

- Slash commands: `/play`, `/queue`, `/skip`, `/pause`, `/resume`, `/stop`
- Playlist management: `/playlist create`, `/playlist play`, etc.
- Audio filters: bassboost, nightcore, 8D, custom EQ
- Autoplay with genre-based recommendations
- Session persistence and auto-resume after restarts
- Paginated queue and now-playing progress bar

## Deployment

### Kubernetes

The bot and web UI run in the `hellodj-service` namespace.

| Component | Deployment | Service | Ingress |
|-----------|-----------|---------|---------|
| Bot + Lavalink | `hellodj` | `hellodj` (port 2333 Lavalink) | — |
| Web UI | `hellodj-web-ui` | `hellodj-web-ui` (port 8080) | `hellodj.celestium.life` |

### Volumes

- `hellodj-data-pvc`: Longhorn 1Gi for bot sessions/playlists
- `hellodj-config-pvc`: NFS 10Gi for web UI config storage (192.168.42.8:/volume1/Kubernetes/HelloDJ/data)

## Training

Custom "Hello DJ" wake word model using openWakeWord and piper-sample-generator.

See `training/README.md` for setup and usage.

## License

MIT
