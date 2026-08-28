#!/usr/bin/env bash
# Deploy the HelloDJ workloads manifests (all stages) at the current HEAD commit.
#
# WHY THIS EXISTS
# ---------------
# The workloads' Kubernetes manifests live in the `hellodj-eks` foundation stack
# (via `cluster.addManifest`), NOT in the per-stage WorkloadsStack the pipeline
# deploys. This is deliberate: `selfMutation` is OFF because applying K8s
# manifests through the EKS kubectl handler Lambda inside a self-mutating
# pipeline triggers cross-stack custom-resource failures (see
# .kiro/specs/cdk-standalone-package/design.md). So a `git push` rebuilds the
# component IMAGES via the pipeline, but the running pods only roll when the
# manifests are re-applied by `cdk deploy hellodj-eks` with a changing,
# immutable image tag.
#
# This wrapper makes that second step correct and footgun-free:
#   * pins the tag to the exact HEAD commit via `-c hellodj:imageTag=<sha>`
#     (context ALWAYS wins over the env var — see bin/hellodj.ts), so a stale
#     `CODEBUILD_RESOLVED_SOURCE_VERSION` shell export can never poison the tag
#     (that once shipped a non-existent `web-ui:<garbage>` tag → ImagePullBackOff);
#   * refuses to deploy until the images for HEAD actually exist in ECR (so the
#     pods never roll to a tag the pipeline hasn't pushed yet);
#   * runs from a clean env and a private cdk output dir (avoids the shared
#     cdk.out lock when other CLIs are running).
#
# USAGE
#   platform/tools/deploy_workloads.sh [--component <name>] [--all-components]
#
#   --component <name>   Verify only this component's image exists at HEAD
#                        before deploying (default: web-ui).
#   --all-components      Verify EVERY component image exists at HEAD (use when a
#                        change spans multiple components).
#
# Requires: AWS_PROFILE=hellodj (or a configured default), git, aws, npx cdk.
set -euo pipefail

PROFILE="${AWS_PROFILE:-hellodj}"
REGION="${AWS_REGION:-us-east-1}"
ECR_PREFIX="hellodj"
INFRA_DIR="$(cd "$(dirname "$0")/../infra" && pwd)"

VERIFY_COMPONENTS=("web-ui")
if [[ "${1:-}" == "--all-components" ]]; then
  VERIFY_COMPONENTS=(
    discord-bot-core playback-orchestrator lavalink tidal-stream spotify-stream
    yt-cipher potoken-server activity-backend hls-transcode voice-pipeline
    web-ui config-renderer
  )
elif [[ "${1:-}" == "--component" && -n "${2:-}" ]]; then
  VERIFY_COMPONENTS=("$2")
fi

COMMIT="$(git -C "$INFRA_DIR" rev-parse HEAD)"
echo "==> Deploying workloads at HEAD commit: $COMMIT"

# Refuse to deploy a tag the pipeline hasn't built+pushed yet.
for comp in "${VERIFY_COMPONENTS[@]}"; do
  echo "==> Verifying ECR image $ECR_PREFIX/$comp:$COMMIT exists ..."
  if ! AWS_PROFILE="$PROFILE" aws ecr describe-images \
      --repository-name "$ECR_PREFIX/$comp" \
      --image-ids "imageTag=$COMMIT" \
      --region "$REGION" >/dev/null 2>&1; then
    echo "ERROR: $ECR_PREFIX/$comp:$COMMIT is not in ECR yet." >&2
    echo "       Wait for the pipeline's ComponentBuilds (build-$comp) to finish," >&2
    echo "       then re-run this script. Deploying now would ImagePullBackOff." >&2
    exit 1
  fi
done

# Clean env (drop any stale CODEBUILD_RESOLVED_SOURCE_VERSION) + private output
# dir (avoid the shared cdk.out lock). Context pins the tag authoritatively.
OUT_DIR="$(mktemp -d /tmp/hellodj-cdkout.XXXXXX)"
trap 'rm -rf "$OUT_DIR"' EXIT

echo "==> cdk deploy hellodj-eks (imageTag=$COMMIT) ..."
cd "$INFRA_DIR"
env -u CODEBUILD_RESOLVED_SOURCE_VERSION \
  AWS_PROFILE="$PROFILE" \
  npx cdk deploy hellodj-eks \
    -c "hellodj:imageTag=$COMMIT" \
    --output "$OUT_DIR" \
    --require-approval never

echo "==> Done. Verify the rollout with:"
echo "    KUBECONFIG=/tmp/hellodj-eks-kubeconfig kubectl get pods -n hellodj-beta"
