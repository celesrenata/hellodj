---
name: hellodj-deployment
description: Deploy and manage the HelloDJ Discord bot and web UI service to Kubernetes via Harbor registry and kustomize. Use when building Docker images, pushing to registry.celestium.life, applying Kubernetes manifests, or performing rolling updates.
---

# HelloDJ Deployment

## When to use

- Building and pushing Docker images for the bot and web-ui services
- Deploying to the Kubernetes cluster via `kubectl` or `kustomize`
- Performing rolling updates or rollbacks
- Managing Harbor registry credentials and image tags
- Updating configuration via `kube/configmap.yaml` and `kube/web-ui-deployment.yaml`

## When NOT to use

- Debugging Discord bot logic (use the `hellodj-debug` skill)
- Editing web UI templates or static assets (use the `hellodj-webui` skill)
- Managing Lavalink music playback (use the `hellodj-lavalink` skill)

## Inputs required

- **Service name**: `bot` or `web-ui` (or both for full deployment)
- **Image tag**: defaults to `latest`
- **Kubernetes namespace**: `hellodj-service`

## Workflow

### 1. Build Docker images

```bash
# Build bot image
docker build -t registry.celestium.life/hellodj/bot:<tag> bot/

# Build web-ui image
docker build -t registry.celestium.life/hellodj/web-ui:<tag> web-ui/
```

### 2. Push to Harbor registry

```bash
docker push registry.celestium.life/hellodj/bot:<tag>
docker push registry.celestium.life/hellodj/web-ui:<tag>
```

### 3. Apply Kubernetes manifests

```bash
# Using kustomize
kubectl apply -k kube/

# Or individual manifests
kubectl apply -f kube/namespace.yaml
kubectl apply -f kube/configmap.yaml
kubectl apply -f kube/deployment.yaml
kubectl apply -f kube/web-ui-deployment.yaml
kubectl apply -f kube/service.yaml
kubectl apply -f kube/web-ui-service.yaml
kubectl apply -f kube/ingress.yaml
```

### 4. Verify deployment

```bash
# Check pod status
kubectl get pods -n hellodj-service

# Check service endpoints
kubectl get svc -n hellodj-service

# View logs
kubectl logs -n hellodj-service -l app=bot
kubectl logs -n hellodj-service -l app=web-ui
```

### 5. Rolling update (optional)

```bash
kubectl rollout restart deployment/bot -n hellodj-service
kubectl rollout restart deployment/web-ui -n hellodj-service
```

## Harbor Credentials

- **Registry**: `registry.celestium.life`
- **Username**: `admin`
- **Auth**: Base64 encoded in `~/.docker/config.json`
- **ImagePullSecret**: `harbor-credentials` (configured in deployment manifests)

## Key Files

| File | Purpose |
|------|---------|
| [`kube/deployment.yaml`](kube/deployment.yaml) | Bot deployment manifest |
| [`kube/web-ui-deployment.yaml`](kube/web-ui-deployment.yaml) | Web UI deployment manifest |
| [`kube/service.yaml`](kube/service.yaml) | Bot service definition |
| [`kube/web-ui-service.yaml`](kube/web-ui-service.yaml) | Web UI service definition |
| [`kube/ingress.yaml`](kube/ingress.yaml) | Ingress routing rules |
| [`kube/configmap.yaml`](kube/configmap.yaml) | Shared configuration |
| [`kube/kustomization.yaml`](kube/kustomization.yaml) | Kustomize overlay config |
| [`docker-compose.yml`](docker-compose.yml) | Local development deployment |
| [`bot/.env.example`](bot/.env.example) | Bot environment variables |
| [`web-ui/app.py`](web-ui/app.py) | Web UI application code |

## Examples

### Deploy full stack

```bash
docker build -t registry.celestium.life/hellodj/bot:latest bot/
docker build -t registry.celestium.life/hellodj/web-ui:latest web-ui/
docker push registry.celestium.life/hellodj/bot:latest
docker push registry.celestium.life/hellodj/web-ui:latest
kubectl apply -k kube/
```

### Deploy with custom tag

```bash
TAG=v1.2.3
docker build -t registry.celestium.life/hellodj/bot:$TAG bot/
docker build -t registry.celestium.life/hellodj/web-ui:$TAG web-ui/
docker push registry.celestium.life/hellodj/bot:$TAG
docker push registry.celestium.life/hellodj/web-ui:$TAG
kubectl set image deployment/bot bot=registry.celestium.life/hellodj/bot:$TAG -n hellodj-service
kubectl set image deployment/web-ui web-ui=registry.celestium.life/hellodj/web-ui:$TAG -n hellodj-service
```

### Rollback to previous version

```bash
kubectl rollout undo deployment/bot -n hellodj-service
kubectl rollout undo deployment/web-ui -n hellodj-service
```

## Troubleshooting

### ImagePullBackOff error

- Verify Harbor credentials: `docker info | grep Registry`
- Check image exists: `docker pull registry.celestium.life/hellodj/bot:<tag>`
- Verify imagePullSecret: `kubectl get secret harbor-credentials -n hellodj-service`

### Pod not starting

- Check logs: `kubectl logs -n hellodj-service <pod-name>`
- Check events: `kubectl describe pod -n hellodj-service <pod-name>`
- Verify configmap: `kubectl get configmap -n hellodj-service`

### Service not accessible

- Verify service: `kubectl get svc -n hellodj-service`
- Check ingress: `kubectl get ingress -n hellodj-service`
- Test endpoint: `curl https://hellodj.celestium.life`
