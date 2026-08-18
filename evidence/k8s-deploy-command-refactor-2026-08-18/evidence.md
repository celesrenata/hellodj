# K8s Deployment — Command Refactor 2026-08-18

## Scope
- Service changed: **bot** only
- web-ui: NOT modified, NOT redeployed
- Lavalink filters config: `bot/lavalink/application.yml` + `kube/configmap.yaml` (10 DSP filters)

## Image Tag
- `registry.celestium.life/hellodj/bot:command-refactor-2026-08-18`
- Build: SUCCESS (docker build, exit 0)
- Push: SUCCESS — digest `sha256:feaab251e6fa4f2e3304a228869c70274f4c9404b6e70381fb25cdfd290e550e`

## kubectl Commands
1. `kubectl apply -f kube/configmap.yaml -n hellodj-service` → `configmap/lavalink-config configured`
2. Updated `kube/deployment.yaml` line 157 image → `command-refactor-2026-08-18`; updated `kube/kustomization.yaml` newTag (bot only; web-ui kept at v2026-08-17)
3. `kubectl apply -f kube/deployment.yaml -n hellodj-service` → `deployment.apps/hellodj configured`
4. `kubectl rollout status deployment/hellodj -n hellodj-service --timeout=300s` → `deployment "hellodj" successfully rolled out`

Note: deployment name is `hellodj` (not `bot`), so `kubectl set image deployment/bot` would target a non-existent deployment. Manifest-based apply was the correct mechanism (tag hardcoded in deployment.yaml).

## Pod Status
- `hellodj-5cdfc546c9-kksg5` — 2/2 Running, 0 restarts
- Deployed image verified: `registry.celestium.life/hellodj/bot:command-refactor-2026-08-18`

## Bot Logs (container: bot)
Clean startup, no errors:
- Lavalink reachable + connected
- youtube-oauth refresh token pushed (status=204)
- VOICE_ENABLED=true, wake word model loaded (Hello_DJ.onnx)
- Voice orchestrator initialized, tick loop started
- `HelloDJ slash commands synced.`
- Gateway connected, `on_ready fired with 2 guilds`
- guild_policy: 2 guilds authorized

## Lavalink Sidecar Logs (container: lavalink)
Clean startup, no filter errors:
- YouTube source initialised, Spotify manager registered
- `Lavalink is ready to accept connections.`
- YouTube access token refreshed successfully

## ConfigMap Filters Block — CONFIRMED
`kubectl get configmap lavalink-config -n hellodj-service -o yaml` shows `filters.enabled: true` with 10 DSP filters:
volume, equalizer, karaoke, timescale, tremolo, vibrato, distortion, rotation, lowPass, channelMix — all `enabled: true`.

## Issues
- None. No ImagePullBackOff, no startup errors, no command sync failures.
