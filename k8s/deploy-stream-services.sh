#!/bin/bash
# Deploy stream services as sidecars to the hellodj pod.
#
# Prerequisites:
#   1. Docker images built and pushed:
#      docker build -t registry.celestium.life/hellodj/tidal-stream:latest ./tidal-stream
#      docker push registry.celestium.life/hellodj/tidal-stream:latest
#      docker build -t registry.celestium.life/hellodj/spotify-stream:latest ./spotify-stream
#      docker push registry.celestium.life/hellodj/spotify-stream:latest
#
#   2. Bot image rebuilt with stream_resolver.py included
#
#   3. Tidal OAuth completed via https://hellodj.celestium.life/auth/tidal/callback
#      (tokens stored in data/oauth.json)

set -euo pipefail

NAMESPACE="hellodj-service"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Patching configmap with stream service URLs ==="
kubectl patch configmap hellodj-bot-config -n "$NAMESPACE" \
  --type=strategic \
  -p '{"data":{"TIDAL_STREAM_URL":"http://localhost:8801","SPOTIFY_STREAM_URL":"http://localhost:8802"}}'

echo ""
echo "=== Patching deployment with stream service sidecars ==="
kubectl patch deployment hellodj -n "$NAMESPACE" \
  --type=strategic \
  --patch-file="$SCRIPT_DIR/stream-services-patch.yaml"

echo ""
echo "=== Waiting for rollout ==="
kubectl rollout status deployment/hellodj -n "$NAMESPACE" --timeout=120s

echo ""
echo "=== Verifying pods ==="
kubectl get pods -n "$NAMESPACE" -l app.kubernetes.io/name=hellodj

echo ""
echo "=== Done! Check logs with: ==="
echo "  kubectl logs -n $NAMESPACE -l app.kubernetes.io/name=hellodj -c tidal-stream --tail=20"
echo "  kubectl logs -n $NAMESPACE -l app.kubernetes.io/name=hellodj -c spotify-stream --tail=20"
