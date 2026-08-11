#!/usr/bin/env bash
# HelloDJ — runmelast.sh
# Teardown / rollback: delete the deployed resources from the cluster.
# Run this last, after runmefirst.sh, to fully remove the deployment.
set -euo pipefail

NS="hellodj-service"

echo "==> [1/3] Deleting kustomize resources"
kubectl delete -k kube/ --ignore-not-found

echo "==> [2/3] Cleaning up orphaned resources (best effort)"
kubectl delete pvc hellodj-data-pvc hellodj-config-pvc hellodj-models-pvc -n "$NS" --ignore-not-found
kubectl delete pv hellodj-config-nfs-pv hellodj-models-nfs-pv --ignore-not-found

echo "==> [3/3] Final namespace check"
kubectl get pods -n "$NS" 2>/dev/null || echo "namespace $NS empty/gone"

echo "==> Done."
