---
inclusion: manual
---

# HelloDJ Deployment

## When to Use

- Building and pushing Docker images for the bot and web-ui services
- Deploying to the Kubernetes cluster via `kubectl` or `kustomize`
- Performing rolling updates or rollbacks
- Managing Harbor registry credentials and image tags
- Updating configuration via `kube/configmap.yaml` and `kube/web-ui-deployment.yaml`

## Infrastructure

- **Registry**: `registry.celestium.life` (Harbor)
- **Namespace**: `hellodj-service`
- **Images**: `registry.celestium.life/hellodj/bot:<tag>`, `registry.celestium.life/hellodj/web-ui:<tag>`
- **ImagePullSecret**: `harbor-credentials`

## Workflow

### 1. Build Docker Images

```bash
# Build bot image
docker build -t registry.celestium.life/hellodj/bot:<tag> bot/

# Build web-ui image
docker build -t registry.celestium.life/hellodj/web-ui:<tag> web-ui/
```

### 2. Push to Harbor Registry

```bash
docker push registry.celestium.life/hellodj/bot:<tag>
docker push registry.celestium.life/hellodj/web-ui:<tag>
```

### 3. Apply Kubernetes Manifests

```bash
# Using kustomize (preferred)
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

### 4. Verify Deployment

```bash
kubectl get pods -n hellodj-service
kubectl get svc -n hellodj-service
kubectl logs -n hellodj-service -l app=bot
kubectl logs -n hellodj-service -l app=web-ui
```

### 5. Rolling Update

```bash
kubectl rollout restart deployment/bot -n hellodj-service
kubectl rollout restart deployment/web-ui -n hellodj-service
```

### 6. Rollback

```bash
kubectl rollout undo deployment/bot -n hellodj-service
kubectl rollout undo deployment/web-ui -n hellodj-service
```

## Key Files

| File | Purpose |
|------|---------|
| `kube/deployment.yaml` | Bot deployment manifest |
| `kube/web-ui-deployment.yaml` | Web UI deployment manifest |
| `kube/service.yaml` | Bot service definition |
| `kube/web-ui-service.yaml` | Web UI service definition |
| `kube/ingress.yaml` | Ingress routing rules |
| `kube/configmap.yaml` | Shared configuration |
| `kube/kustomization.yaml` | Kustomize overlay config |
| `docker-compose.yml` | Local development deployment |
| `bot/.env.example` | Bot environment variables |

## Troubleshooting

### ImagePullBackOff
- Verify Harbor creds: `docker info | grep Registry`
- Check image exists: `docker pull registry.celestium.life/hellodj/bot:<tag>`
- Verify secret: `kubectl get secret harbor-credentials -n hellodj-service`

### Pod Not Starting
- Check logs: `kubectl logs -n hellodj-service <pod-name>`
- Check events: `kubectl describe pod -n hellodj-service <pod-name>`
- Verify configmap: `kubectl get configmap -n hellodj-service`

### Service Not Accessible
- Verify service: `kubectl get svc -n hellodj-service`
- Check ingress: `kubectl get ingress -n hellodj-service`
- Test endpoint: `curl https://hellodj.celestium.life`
