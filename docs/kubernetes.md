# Kubernetes Deployment

## Cluster

- Platform: k3s on NixOS gremlin nodes (10.1.1.12–15)
- Namespace: `hellodj-service`
- Strategy: Recreate (single replica, stateful)
- Registry: `registry.celestium.life` (Harbor)
- ImagePullSecret: `harbor-credentials`

## Deployment Commands

```bash
# Apply all manifests
cd kube/
kubectl apply -k .

# Check pod status
kubectl get pods -n hellodj-service

# View bot logs
kubectl logs -n hellodj-service deployment/hellodj -c bot -f

# View lavalink logs
kubectl logs -n hellodj-service deployment/hellodj -c lavalink --tail=50

# Restart (new image pull)
kubectl rollout restart deployment/hellodj -n hellodj-service

# Watch rollout
kubectl rollout status deployment/hellodj -n hellodj-service
```

## Manifest Structure (kustomize)

```
kube/
├── kustomization.yaml          # Resource list + image tags
├── namespace.yaml              # hellodj-service namespace
├── deployment.yaml             # Main pod (bot + lavalink + sidecars)
├── service.yaml                # ClusterIP for lavalink:2333 + activity:8090
├── configmap.yaml              # Lavalink application.yml (legacy sed-based)
├── bot-configmap.yaml          # Bot env vars (non-secret)
├── youtube-secret.yaml         # YouTube secret (intentionally empty)
├── yt-cipher-secret.yaml       # yt-cipher API token
├── pvc.yaml                    # hellodj-data-pvc (Longhorn 1Gi)
├── nfs-pv.yaml + nfs-pvc.yaml  # NFS config volume
├── models-nfs-pv.yaml + models-pvc.yaml  # NFS models volume
├── backups-nfs-pv.yaml + backups-pvc.yaml  # NFS backups
├── web-ui-deployment.yaml      # Web UI pod
├── web-ui-service.yaml         # Web UI service
├── ingress.yaml                # Traefik ingress (hellodj.celestium.life)
├── yt-cipher-deployment.yaml   # yt-cipher pod
├── yt-cipher-service.yaml      # yt-cipher service
├── potoken-server-deployment.yaml  # bgutil pod
└── potoken-server-service.yaml    # bgutil service
```

## Image Tags

Update in `kustomization.yaml`:

```yaml
images:
  - name: registry.celestium.life/hellodj/bot
    newTag: feature-freeze-2026-08-21
  - name: registry.celestium.life/hellodj/web-ui
    newTag: latest
  - name: registry.celestium.life/hellodj/tidal-stream
    newTag: latest
  - name: registry.celestium.life/hellodj/spotify-stream
    newTag: latest
```

The init container image is hardcoded in `deployment.yaml` (not affected by kustomize images).

## Building & Pushing

```bash
cd bot/
docker build -t registry.celestium.life/hellodj/bot:<tag> .
docker push registry.celestium.life/hellodj/bot:<tag>
```

## Pod Security

```yaml
securityContext:
  runAsUser: 1000
  runAsGroup: 1000
  fsGroup: 1000
  supplementalGroups: [26]     # video group for /dev/dri
  sysctls:
    - name: net.ipv4.tcp_keepalive_time
      value: "60"              # Prevents Discord gateway stalls
```

Bot container runs as `privileged: true` for GPU device access.

## DNS Configuration

```yaml
dnsPolicy: ClusterFirst
dnsConfig:
  nameservers:
    - 192.168.42.1    # pfSense (fallback for external DNS)
    - 192.168.99.42   # Secondary DNS
```

**Warning:** DO NOT add `search celestium.life` — wildcard DNS hijacks Lavalink plugin downloads.

## Probes

### Lavalink
- **Startup:** GET `/v4/info` every 3s, 40 failures allowed (120s startup budget)
- **Readiness:** GET `/v4/info` every 5s, 6 failures
- **Liveness:** GET `/v4/info` every 30s, 3 failures

### Tidal Stream / Spotify Stream
- **Readiness:** GET `/health` every 10s
- **Liveness:** GET `/health` every 30s

## Secrets

| Secret | Keys | Purpose |
|--------|------|---------|
| `hellodj-secret` | DISCORD_TOKEN, DISCORD_APPID, DISCORD_PUBKEY, SPOTIFY_*, GENIUS_*, TIDAL_*, LLM_API_KEY, NEWS_API_KEY, STOCKS_API_KEY | Bot credentials |
| `hellodj-db-key` | HELLODJ_DB_KEY | SQLite encryption key |
| `youtube-secret` | (empty) | Legacy — bot pushes at runtime |
| `yt-cipher-secret` | API_TOKEN | yt-cipher authentication |
| `harbor-credentials` | .dockerconfigjson | Registry pull |

## Troubleshooting

### Bot won't start
```bash
# Check init container
kubectl logs -n hellodj-service <pod> -c render-lavalink-config

# Check for missing secrets
kubectl get secrets -n hellodj-service

# Describe pod for events
kubectl describe pod -n hellodj-service -l app.kubernetes.io/name=hellodj
```

### Lavalink not reachable
```bash
# Bot polls Lavalink at startup (30 retries, 2s apart)
kubectl logs -n hellodj-service <pod> -c lavalink --tail=20

# Verify rendered config
kubectl exec -n hellodj-service <pod> -c lavalink -- cat /opt/Lavalink/application.yml
```

### YouTube playback fails
```bash
# Check OAuth push
kubectl logs -n hellodj-service <pod> -c bot | grep youtube-auth

# Check PoToken refresh
kubectl logs -n hellodj-service <pod> -c bot | grep potoken

# Check yt-cipher
kubectl logs -n hellodj-service deployment/yt-cipher
```

### Tidal stream not working
```bash
# Check sidecar health
kubectl logs -n hellodj-service <pod> -c tidal-stream --tail=10

# If "No valid Tidal session": re-auth via web UI (/auth/tidal/login)
```

### Gateway reconnect loop
- Check `net.ipv4.tcp_keepalive_time` sysctl (should be 60)
- Bot has gateway health watchdog that force-reconnects after 120s stall
- After 3 failed reconnects → process exit (k8s restarts)

### Volume permissions
- All containers run as UID 1000 (fsGroup: 1000)
- NFS volumes must be accessible by UID 1000
- Longhorn PVC auto-formats with correct permissions
