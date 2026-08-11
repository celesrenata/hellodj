#!/usr/bin/env bash
# HelloDJ — runmefirst.sh
# First-time setup + deployment: build images, push to Harbor, apply kustomize.
# Run this before runmelast.sh (or as the initial deploy of the project).
set -euo pipefail

REGISTRY="registry.celestium.life"
NS="hellodj-service"

echo "==> [1/5] Building images"
docker build -t "$REGISTRY/hellodj/bot:latest" bot/
docker build -t "$REGISTRY/hellodj/web-ui:latest" web-ui/

echo "==> [2/5] Pushing to Harbor"
docker push "$REGISTRY/hellodj/bot:latest"
docker push "$REGISTRY/hellodj/web-ui:latest"

echo "==> [3/5] Applying Kubernetes manifests (kustomize)"
kubectl apply -k kube/

echo "==> [4/5] Waiting for rollout"
kubectl rollout status deployment/hellodj -n "$NS" --timeout=300s
kubectl rollout status deployment/hellodj-web-ui -n "$NS" --timeout=300s

echo "==> [5/5] Verifying"
kubectl get pods -n "$NS"
kubectl get svc -n "$NS"
kubectl get ingress -n "$NS"
curl -sk https://hellodj.celestium.life -o /dev/null -w "HTTP %{http_code}\n"

echo "==> Done."
