# HelloDJ Nix-Native Delivery — Reproducible Verification & Deploy Path

> Spec: `hellodj-nix-native-delivery`, task 20.1 — **Requirement 12.8**.
>
> This document is the enumerated, **copy-runnable** command set that reproduces the entire
> build-and-deploy path end to end:
>
> **push to the `hellodj` account → Nix build with no paid build server → publish to the
> S3-backed Nix binary cache + ECR → promote Beta → Staging → Production on the single GPU host.**
>
> Every command below is real and exists in this repo today. Commands are grouped by phase; each
> phase names the Requirement 12 acceptance criterion it verifies. If any command in §3
> (verification) exits non-zero or reports a failure, verification is **failed** and the failing
> command + artifact must be identified (R12.7) — the aggregating harness in `task 20.2`
> automates that.

All paths are relative to `platform/` unless a `working-directory` is called out. Run everything
on a machine with a Nix builder for the target system (`aarch64-linux` for the Graviton images and
the GPU AMI; `x86_64-linux` is the documented fallback). The user's workstation runs NixOS, so
`nix` (with `nix-command flakes` enabled) is already the native toolchain.

---

## 0. One-time prerequisites

```bash
# Nix with flakes (already the case on NixOS; shown for portability).
export NIX_CONFIG="experimental-features = nix-command flakes"

# Python + the platform test/tooling venv (ruff, pytest, hypothesis).
cd platform
python3 -m venv .venv-pbt && . .venv-pbt/bin/activate
pip install -e '.[test,dev]'   # test = pytest/hypothesis, dev = ruff (see pyproject.toml)

# CDK / jest toolchain for the infra app.
cd infra && npm ci && cd ..
```

---

## 1. Source of truth — push to the `hellodj` account (R1, R10.1)

The four JVM forks and the app/`platform/` monorepo live under the `hellodj` account. Each fork
keeps its `upstream` remote so `nix flake update <input>` can pull future upstream merges
(R11.3/11.4). Pushing to a tracked branch (`main` for the app, `dev` for Lavalink) is what triggers
the build (R10.1).

```bash
# Fork remotes are: origin -> github:hellodj/<repo>, upstream -> <original upstream>.
# Verify a fork is wired correctly (run inside each fork checkout):
#   /home/celes/sources/celesrenata/{Lavalink,lavaplayer,LavaSrc,youtube-source}
git remote -v          # expect origin=github:hellodj/<repo>, upstream=<original>

# Push the app/platform monorepo tracked branch to the hellodj account.
git push origin main

# Push the Lavalink fork build branch (R1.3).
#   (from the Lavalink fork checkout)
git push origin dev
```

The fork-migration decision (halt on first failure, leave prior forks unchanged) is the pure,
property-tested `hellodj_platform_logic.migration.migrate_forks` (Property 1); the fork inputs are
pinned in `pins.toml` as `github:hellodj/<repo>/<branch>`.

---

## 2. Nix build with no paid build server (R2, R3, R5, R6)

The single selected `Build_Trigger` is **GitHub Actions with Nix** (`.github/workflows/nix-build.yml`)
— ephemeral runners, **$0 idle**, no persistent paid build server (R6.1/6.5). The commands the
workflow runs are exactly the ones below, so you can reproduce any of them locally.

### 2.1 `nix flake check` — every Fork_Flake and Component_Flake (R12.1, R2.7)

```bash
# The four JVM forks (built in dependency order: lavaplayer -> plugins -> Lavalink).
nix flake check github:hellodj/lavaplayer/main
nix flake check github:hellodj/LavaSrc/tidal-v2-api
nix flake check github:hellodj/youtube-source/main
nix flake check github:hellodj/Lavalink/dev

# Every platform component flake (skip any component whose Nix flake is still pending).
for d in components/*/; do
  [ -f "$d/flake.nix" ] || continue
  echo "== nix flake check $d =="
  ( cd "$d" && nix flake check . )
done

# The GPU AMI flake.
( cd infra/ami && nix flake check . )
```

### 2.2 `nix build .#jar` / `.#image` — real jars and OCI images, no placeholder (R12.2, R2.6, R4.6)

```bash
# --- Fork jars (.#jar) ---------------------------------------------------------
nix build github:hellodj/lavaplayer/main#lavaplayerJar        --print-out-paths
nix build github:hellodj/LavaSrc/tidal-v2-api#lavasrcPlugin   --print-out-paths
nix build github:hellodj/youtube-source/main#youtubeSabrPlugin --print-out-paths
nix build github:hellodj/Lavalink/dev#lavalinkJar             --print-out-paths

# --- Lavalink OCI image (.#image), wiring the real plugin jars (R4) ------------
nix build github:hellodj/Lavalink/dev#image --print-out-paths

# --- Every platform component OCI image (.#image) ------------------------------
for d in components/*/; do
  [ -f "$d/flake.nix" ] || continue
  echo "== nix build $d#image =="
  ( cd "$d" && nix build .#image --print-out-paths --no-link )
done
```

Property 2 (`Feature: hellodj-nix-native-delivery, Property 2`) asserts each built jar declares a
`Main-Class`/plugin entrypoint, contains `.class` files, and has **no** `PLACEHOLDER ARTIFACT`
marker. The forks build hermetically (`--offline`, vendored gradle2nix repo) on **Temurin 25**
(R2.5/R3).

### 2.3 Base-image gate — hard gate, PASS for every component, zero SKIP (R12.3, R5.6/5.7)

```bash
python3 tools/gate_base_image.py            # scan every component under components/
python3 tools/gate_base_image.py --self-test # smoke check + full scan (as CI runs it)
```

Non-zero exit fails the build and blocks the compliance claim (R5.9). The end-state target is
**PASS for every component, SKIP for zero** (reached once the companion `nix-image-packaging`
flakes land for the remaining Python components).

### 2.4 Pin verification — latest verified upstream pins (R11)

```bash
python3 tools/gate_pins.py                  # verify every input in pins.toml vs pins.upstream.toml
python3 tools/gate_pins.py temurin          # verify one input (Temurin MUST be feature version 25)
python3 tools/gate_pins.py --self-test
```

`verify_pin` (Property 13) accepts iff the pinned identifier equals the resolved upstream
identifier; a mismatch rejects and names the input, an unresolved upstream fails and names the
input — the prior pinned revision is retained in both failure paths (R11.5/11.6).

### 2.5 GPU AMI — `nixos-generate -f amazon` (or the `infra/ami` flake build) (R12.4, R5.2)

```bash
# Preferred: the infra/ami flake build (identical amazon-image generator).
( cd infra/ami && nix build .#amazonImage --print-out-paths )

# Equivalent nixos-generators CLI form:
nixos-generate -f amazon --system aarch64-linux -c infra/ami/gpu-node.nix
```

Both produce the **one shared** EBS-backed GPU AMI (disk image + `nix-support/image-info.json`
metadata) used across all three stages (R8.4). The `infra/ami` flake exposes a
`checks.amazonImageBuild` integration check that asserts exit 0 and the produced artifact.

### 2.6 CDK synth — reconciled stage names + single-host endpoints (R12.5, R9)

```bash
cd infra
npx cdk synth          # synthesizes with Beta / Staging / Production + per-stage Stage_Endpoints
cd ..
```

`PROMOTION_ORDER` is `['beta','staging','production']` (zero `gamma`); the three
`StageEndpoint`s (namespace `hellodj-<stage>` + hostname `<stage>.<region>.hellodj.bot`) are wired
on the single shared cluster/GPU host.

### 2.7 jest — the infra test suite passes (R12.6)

```bash
cd infra
npm test               # or: npx jest
cd ..
```

Includes the example tests for pipeline wiring (gate present, resolve/verify steps, build precedes
deploy) and the fast-check stage-model property mirror (Property 9/10 CDK mirror).

### 2.8 Python pure-logic unit + property tests (supports R2/R6/R7/R8/R9/R10/R11)

```bash
python3 -m pytest                          # unit + Hypothesis property tests (13 properties)
python3 -m ruff check .                     # PEP 8
python3 tools/check_line_count.py           # 500-line-max per file
```

---

## 3. Publish — S3-backed Nix binary cache + ECR (R6.2, R7)

After the gate PASSES and every artifact builds, closures are **signed**, **pushed** to the
S3-backed cache, **verified retrievable** (narinfo read-back) **before** being marked available,
then images are pushed to ECR (R7.7). This is `build-once`: the store-path hash is the identity all
three stages reuse (R7.2/7.3).

```bash
# S3-backed cache URI (backend selected in closures.toml [cache]; injected at build time).
export NIX_CACHE_S3_URI='s3://hellodj-nix-cache?region=us-east-1'

# For one artifact (the workflow loops over all forks + components + the AMI):
out="$(nix build github:hellodj/Lavalink/dev#image --print-out-paths --no-link)"

# 1. sign the closure with the cache secret key (R7.1)
nix store sign --key-file "$NIX_CACHE_KEY_FILE" --recursive "$out"
# 2. push the closure to the S3-backed cache (R7.2)
nix copy --to "$NIX_CACHE_S3_URI" "$out"
# 3. verify retrievable (narinfo read-back) BEFORE marking available (R7.7)
nix path-info --store "$NIX_CACHE_S3_URI" "$out"
# 4. mark available — record the build-once store-path hash in closures.toml (R7.3)
python3 tools/record_closure.py --name lavalink --store-path "$out"

# Push a component OCI image to ECR (after the same sign/push/verify/record):
aws ecr get-login-password --region "$AWS_REGION" \
  | skopeo login --username AWS --password-stdin "$ECR_REGISTRY"
skopeo copy "docker-archive:${out}" "docker://${ECR_REGISTRY}/lavalink:${GITHUB_SHA}"
```

### 3.1 Explicit rebuild (R7.5) and cache-unreachable local rebuild (R7.6)

```bash
# Explicit rebuild + re-push even if an identical closure is already cached (R7.5):
nix build github:hellodj/Lavalink/dev#image --rebuild --print-out-paths --no-link

# The pure decision that folds explicit-rebuild with cache-unreachability (R7.5/7.6):
python3 tools/ephemeral_builder.py rebuild-decision \
  --explicit-rebuild false --cache-responded false --retries 3
```

### 3.2 Ephemeral-builder fallback teardown safety (R6.6–6.9)

Only the on-demand ephemeral aarch64 builder fallback (large AMI builds) provisions real compute;
its teardown is bounded and alerting:

```bash
python3 tools/ephemeral_builder.py teardown \
  --resource-id i-0123456789abcdef0 \
  --stopped-confirmed true \
  --timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

Torn down within ≤300 s of build completion; hard ≤10800 s (3 h) max-lifetime cap; a non-zero exit
with `::error::ALERT` naming the still-running compute when the stop is not confirmed (R6.8);
records id + teardown timestamp on a confirmed stop (R6.9). Modeled by `ephemeral_teardown`
(Property 4).

---

## 4. Promote Beta → Staging → Production on the single GPU host (R8, R10)

CDK Pipelines is retained for **orchestration/deploy only** — its per-stage steps **resolve +
verify prebuilt closures from the cache/ECR** (no CodeBuild compute is billed for building —
R6.3/6.4). Promotion runs in fixed order and halts on the first failure (R10.3–10.5).

```bash
cd infra

# Deploy the pipeline itself (self-mutating CDK Pipeline). Subsequent pushes to the
# tracked branch drive Beta -> Staging -> Production automatically.
npx cdk deploy hellodj-pipeline

cd ..
```

What the pipeline's build/deploy steps run (reproducible locally per stage):

```bash
# Resolve + verify a component's prebuilt closure by store-path hash for a stage
# (build-once/deploy-thrice; halts + surfaces the missing store path if absent — R7.4):
python3 tools/resolve_closure.py --component web-ui --verify --stage beta
python3 tools/resolve_closure.py --component web-ui --verify --stage staging
python3 tools/resolve_closure.py --component web-ui --verify --stage production

# Resolve + verify the single shared GPU AMI closure (R8.4):
python3 tools/resolve_closure.py --ami --verify

# Resolve + verify every recorded closure at once:
python3 tools/resolve_closure.py --all --verify
```

Single-host isolation: all three stages run on the one shared GPU host, isolated by distinct
`Stage_Endpoint`s (namespace `hellodj-<stage>`, hostname `<stage>.<region>.hellodj.bot`); there is
**no per-stage GPU instance** (R8.1–8.4). A request to one `Stage_Endpoint` routes only to that
stage's workload (`route_endpoint`, Property 9). The shared time-sliced GPU NodePool scales to zero
after the idle window (default 300 s, 60–900 s configurable) and back up on GPU workload arrival
(`gpu_idle_decision`, Property 8; R8.5/8.6).

---

## 5. End-to-end verification (all of R12.1–6 in order)

Run this block from `platform/` to reproduce the full verification path. Any non-zero exit means
verification **failed**; identify the failing command + artifact (R12.7). The `task 20.2` harness
aggregates these into a single pass/fail with the failing command named.

```bash
set -euo pipefail
cd platform

# R12.1 — nix flake check (forks + components + AMI)
nix flake check github:hellodj/lavaplayer/main
nix flake check github:hellodj/LavaSrc/tidal-v2-api
nix flake check github:hellodj/youtube-source/main
nix flake check github:hellodj/Lavalink/dev
for d in components/*/; do [ -f "$d/flake.nix" ] && ( cd "$d" && nix flake check . ); done
( cd infra/ami && nix flake check . )

# R12.2 — nix build .#jar / .#image (real artifacts, no placeholder)
nix build github:hellodj/Lavalink/dev#lavalinkJar --print-out-paths --no-link
nix build github:hellodj/Lavalink/dev#image       --print-out-paths --no-link
for d in components/*/; do [ -f "$d/flake.nix" ] && ( cd "$d" && nix build .#image --print-out-paths --no-link ); done

# R12.3 — base-image gate: PASS every component, zero SKIP
python3 tools/gate_base_image.py

# R12.4 — GPU AMI amazon-image build
( cd infra/ami && nix build .#amazonImage --print-out-paths --no-link )

# R12.5 — CDK synth with reconciled stage names + endpoints
( cd infra && npx cdk synth >/dev/null )

# R12.6 — jest suite
( cd infra && npm test )

# Supporting: pins, pure-logic tests, style
python3 tools/gate_pins.py
python3 -m pytest -q
python3 -m ruff check .
python3 tools/check_line_count.py

echo "ALL VERIFICATION COMMANDS PASSED (R12.1-6)"
```

---

## Command → Requirement quick reference

| Command | Verifies |
|---|---|
| `git push origin main` / `git push origin dev` | R1, R10.1 — source in `hellodj` account, tracked-branch trigger |
| `nix flake check <flake>` | R12.1, R2.7 — flakes evaluate to exit 0 |
| `nix build .#<jar>` / `.#image` | R12.2, R2.6, R4.6 — real jars/images, no placeholder |
| `python3 tools/gate_base_image.py` | R12.3, R5.6/5.7 — PASS every component, zero SKIP, no distro base |
| `python3 tools/gate_pins.py` | R11 — latest verified upstream pins (Temurin == 25) |
| `nixos-generate -f amazon` / `nix build .#amazonImage` | R12.4, R5.2 — GPU AMI produced |
| `npx cdk synth` | R12.5, R9 — Beta/Staging/Production + single-host endpoints |
| `npm test` (jest) | R12.6 — infra suite green |
| `nix store sign` + `nix copy --to s3://…` + `nix path-info --store s3://…` + `record_closure.py` | R6.2, R7.1/7.2/7.7 — sign, push, verify retrievable, mark available |
| `nix build … --rebuild` / `ephemeral_builder.py rebuild-decision` | R7.5/7.6 — explicit + cache-unreachable rebuild |
| `ephemeral_builder.py teardown` | R6.6–6.9 — bounded teardown + alert |
| `resolve_closure.py --component <c> --verify --stage <s>` / `--ami --verify` | R6.3/6.4, R7.2/7.3/7.4 — deploy pulls prebuilt closure, halts if missing |
| `cdk deploy hellodj-pipeline` | R8, R10 — single-host promotion Beta→Staging→Production |
