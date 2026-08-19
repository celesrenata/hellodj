#!/usr/bin/env bash
# HelloDJ — runmefirst.sh
# First-time setup + deployment: build images, push to Harbor, apply kustomize.
# Run this before runmelast.sh (or as the initial deploy of the project).
set -euo pipefail

REGISTRY="registry.celestium.life"
NS="hellodj-service"

echo "==> [1/7] Building bot image"
docker build -t "$REGISTRY/hellodj/bot:latest" bot/

echo "==> [2/7] Building web-ui image"
docker build -t "$REGISTRY/hellodj/web-ui:latest" web-ui/

echo "==> [3/7] Building tidal-stream image"
docker build -t "$REGISTRY/hellodj/tidal-stream:latest" tidal-stream/

echo "==> [4/7] Building spotify-stream image"
docker build -t "$REGISTRY/hellodj/spotify-stream:latest" spotify-stream/

echo "==> [5/7] Pushing to Harbor"
docker push "$REGISTRY/hellodj/bot:latest"
docker push "$REGISTRY/hellodj/web-ui:latest"
docker push "$REGISTRY/hellodj/tidal-stream:latest"
docker push "$REGISTRY/hellodj/spotify-stream:latest"

echo "==> [6/7] Applying Kubernetes manifests (kustomize)"
kubectl apply -k kube/

echo "==> [7/7] Waiting for rollout"
kubectl rollout status deployment/hellodj -n "$NS" --timeout=300s
kubectl rollout status deployment/hellodj-web-ui -n "$NS" --timeout=300s

echo ""
echo "==> Verifying"
kubectl get pods -n "$NS"
kubectl get svc -n "$NS"
kubectl get ingress -n "$NS"
curl -sk https://hellodj.celestium.life -o /dev/null -w "HTTP %{http_code}\n"

echo ""
echo "==> Stream services status:"
kubectl logs -n "$NS" -l app.kubernetes.io/name=hellodj -c tidal-stream --tail=5 2>/dev/null || echo "  tidal-stream: not yet ready"
kubectl logs -n "$NS" -l app.kubernetes.io/name=hellodj -c spotify-stream --tail=5 2>/dev/null || echo "  spotify-stream: not yet ready"

echo ""
echo "==> Done."
