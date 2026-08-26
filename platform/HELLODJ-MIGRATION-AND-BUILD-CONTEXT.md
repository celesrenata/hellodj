# Context for a New Spec: HelloDJ Repo Migration + Native-Nix Build/Deploy Pipeline (No Build Server)

Paste this whole file into a new chat and ask it to **create a spec** (requirements → design → tasks)
covering repo migration to the `hellodj` account, per-repo Nix build recipes for the forked JVM repos,
a serverless (no CodeBuild-server-to-pay-for) native-Nix build path for all artifacts (containers +
AMIs), and a single-GPU-host, three-stage (beta/staging/production) deployment on shared hardware.

Suggested opening prompt for the new chat:

> Create a spec named `hellodj-nix-native-delivery` for migrating the HelloDJ code and our modified
> upstream forks into our `hellodj` GitHub/Git account, giving each fork its own repo + Nix build
> recipe + Nix container, and building ALL artifacts (OCI containers and the GPU AMI) natively with
> Nix so we don't pay for a build server. Beta/staging/production run on the SAME hardware (one GPU
> host), each stage pointing at a different endpoint on that shared host. Use the context below.
> IMPORTANT: build everything from the LATEST upstream versions — verify current versions against
> upstream (GitHub releases / tags / nixpkgs) at spec time, do not rely on training-data memory.

---

## 0. Non-negotiable ground rules (from the user)

1. **Cost first.** "Shit costs too much." Do not provision anything that bills when idle if it can be
   avoided. **No dedicated build server** if a serverless / on-demand / local build path can produce
   the same artifacts.
2. **One GPU host, three stages.** Beta, staging, and production **live on the same hardware** (the
   NixOS GPU host). There is **no reason to run them on separate instances.** Each stage is isolated
   by **pointing at a different endpoint on the SAME GPU host** (different ports / DNS names / EKS
   namespaces / systemd instances — decide in design), not by duplicating the instance. Maintain the
   single NixOS GPU instance cheaply.
3. **Everything built with Nix, natively.** Nix builds OCI **containers** natively
   (`dockerTools.buildLayeredImage`) and **AMIs** natively (`nixos-generators -f amazon`). Ensure the
   spec has a concrete build solution for **every** artifact type. No Ubuntu/Debian/**Alpine** bases
   anywhere (the current custom Lavalink Dockerfile even uses `eclipse-temurin:21-jre-alpine` — that
   must go).
4. **Latest upstream versions.** Build from current upstream. **Verify versions live** (upstream repo
   releases/tags, nixpkgs) during spec authoring — do not assume from memory.

---

## 1. What exists today (facts, verified in the workspace)

The workspace is a multi-root workspace with these roots (each is its own git repo):

| Repo (workspace root) | What it is | Build system | Current container/build note |
|---|---|---|---|
| `hellodj/` | The bot, web-ui, kube manifests, and the new `platform/` (CDK + components) | Python + AWS CDK (TS) | The `aws-saas-replatform` spec is fully built under `platform/` |
| `Lavalink/` | Fork of `lavalink-devs/Lavalink` (branch `dev`) — custom `Lavalink.jar` w/ fMP4 HLS patches | Gradle/Kotlin, JVM 21 | has `Dockerfile.custom` → `FROM eclipse-temurin:21-jre-alpine` (Alpine, must be replaced by Nix) |
| `lavaplayer/` | Fork of `lavalink-devs/lavaplayer` | Gradle/Kotlin | provides the fMP4 HLS lavaplayer patch consumed by the Lavalink build |
| `LavaSrc/` | Fork of `topi314/LavaSrc` (Spotify/Tidal source plugin) | Gradle | produces `lavasrc-plugin` jar |
| `youtube-source/` | Fork of `lavalink-devs/youtube-source` (SABR support) | Gradle/Kotlin | produces `youtube-plugin-sabr.jar` |

**The user's requirement:** each of these forked repos (`Lavalink`, `lavaplayer`, `LavaSrc`,
`youtube-source`) must **become its own repo under the `hellodj` account, with its own Nix build
recipe and its own Nix container** (where it produces a runnable artifact; the plugins are jars that
feed the Lavalink image, so decide packaging: a plugin flake output vs. baked into the Lavalink
image — see §4).

### The platform already has (from `aws-saas-replatform`, under `hellodj/platform/`):
- **CDK Pipelines Beta→Gamma→Prod** at `infra/lib/pipeline-stack.ts` (currently CodePipeline +
  CodeBuild + per-component build steps + build gates). **NOTE:** this uses CodeBuild — which is
  exactly the "build server we don't want to pay for." The new spec should reconcile: either make the
  build steps native-Nix and cheap/on-demand, or replace the CodeBuild path with a Nix-native build
  (Hydra / `nix build` on the GPU host / GitHub Actions with Nix / a Nix binary cache). Resolve in
  design against the cost rule.
- **Pre-baked NixOS GPU AMI** at `infra/ami/gpu-node.nix` + `infra/ami/flake.nix`
  (`nixos-generators` `amazon-image`, aarch64) — this is the native-Nix AMI build to reuse/extend.
- **Nix OCI image flakes** already exist for `lavalink`, `spotify-stream`, `yt-cipher`,
  `potoken-server` (`platform/components/<name>/flake.nix`). The lavalink flake currently uses
  **placeholder JAR derivations** with `TODO(artifact-source)` — this migration is what wires the
  real jars from the four forked repos.
- **7 Python components still need Nix image flakes** — see the companion file
  `platform/NIX-CONVERSION-CONTEXT.md` (discord-bot-core, playback-orchestrator, config-renderer,
  activity-backend, voice-pipeline, web-ui, migration). That Nix-conversion work is a prerequisite /
  sibling of this delivery spec; reference it, don't duplicate it.
- **base-image gate** (`platform/tools/gate_base_image.py`) enforcing "Nix-produced, no distro base."
- **EKS on the GPU host / node groups + Karpenter g5g** in `infra/lib/eks-stack.ts`, and the
  **WorkloadsStack** wiring 12 components at `infra/lib/workloads-stack.ts` +
  `infra/lib/component-workloads.ts`.

## 2. Scope of the new spec

### A. Repo migration to the `hellodj` account
1. Move/mirror the four forked repos (`Lavalink`, `lavaplayer`, `LavaSrc`, `youtube-source`) into the
   `hellodj` account as **independent repos**, preserving upstream remote (`upstream`) for future
   syncs, on the branches currently in use (Lavalink `dev`, etc.).
2. Move/push the `hellodj` code itself to the account so **the code pipeline takes over from there**
   and builds beta → staging → production.
3. Each migrated repo gets its **own Nix build recipe** (`flake.nix`) producing:
   - the JVM jar artifact(s) (Gradle build wrapped in Nix — `gradle2nix`/`stdenv.mkDerivation`), and
   - where applicable, its **own Nix OCI container** (Lavalink is the runnable one; plugins are jar
     outputs consumed by the Lavalink image — decide the packaging boundary).
4. **Latest upstream versions** for the base of each fork; verify current Lavalink / lavaplayer /
   LavaSrc / youtube-source / JDK (Temurin) versions against upstream at spec time.

### B. Native-Nix build for ALL artifacts (no build server)
5. Every build artifact — **all component OCI containers AND the GPU AMI** — is produced by
   **Nix natively**. Provide a concrete recipe/output per artifact:
   - Containers: `dockerTools.buildLayeredImage` (pattern already in the repo).
   - AMI: `nixos-generators -f amazon` (pattern already in `infra/ami/`).
   - JVM jars: Nix-wrapped Gradle builds in each fork's flake.
6. **No paid build server.** Design a build strategy that avoids a persistent CodeBuild/Jenkins/Hydra
   fleet. Options the design should evaluate (pick with cost justification):
   - build locally / on the single GPU host (it already runs NixOS) and push to a **Nix binary cache**
     + a container registry (ECR) so the pipeline only *deploys* prebuilt closures;
   - GitHub Actions with Nix (free tier for public repos / cheap) producing the images/AMI and pushing
     to the cache + registry;
   - on-demand ephemeral builder only when a build is actually needed (spot, torn down after) — but
     prefer the two above if they satisfy the cost rule.
   - Reconcile with the **existing CDK Pipelines/CodeBuild** in `pipeline-stack.ts`: keep CDK Pipelines
     for orchestration/deploy but move the actual *build* to Nix outputs pulled from the cache, so no
     CodeBuild compute is paid for building images; or replace it. State the decision + cost impact.
7. **Nix binary cache** strategy so images/AMIs are built once and reused across beta/staging/prod
   (S3-backed cache / attic / cachix — evaluate cost).

### C. Single-GPU-host, three-stage delivery on shared hardware
8. Beta, staging, production **all run on the one NixOS GPU host** (no per-stage instances). Isolate
   stages by **distinct endpoints on the same host**: e.g. per-stage EKS namespaces + per-stage
   ports/DNS (`beta.<region>.hellodj.bot`, `staging...`, `prod...`), or per-stage systemd/nixos
   service instances — design chooses, but the invariant is **one host, three endpoints**.
   - NOTE: the existing DNS logic (`dns_naming.py`) uses `beta`/`gamma`/`prod`; the user says
     **beta/staging/production**. Reconcile the naming (rename `gamma`→`staging`, or map staging↔gamma)
     and keep it consistent across `dns_naming.py`, `promotion.py`, `pipeline-stack.ts`, and the DNS
     records.
9. The **GPU AMI/instance is maintained cheaply**: one GPU host serves all three stages; the hybrid
   gas/electric GPU model (scale-to-zero) still applies so the GPU only bills under load. Each stage
   points at a **different endpoint within the same GPU host**, not a separate GPU instance.
10. The code pipeline, once code is in the `hellodj` account, builds and promotes beta → staging →
    production automatically (fixed order, halt on failure — the existing `promotion.py` logic, renamed
    stages).

## 3. Requirements the new spec should cover (draft)

- Each fork (`Lavalink`, `lavaplayer`, `LavaSrc`, `youtube-source`) is its own `hellodj`-account repo
  with `upstream` remote preserved, its own `flake.nix` (Nix-wrapped Gradle build), and — for
  Lavalink — its own Nix OCI container consuming the plugin jars from the sibling flakes.
- Every artifact (all component containers + the GPU AMI) is built by Nix natively; the lavalink
  flake's placeholder JAR derivations are replaced by real fetches/builds from the migrated repos.
- No persistent paid build server; builds go through a Nix binary cache + registry, with a documented
  build trigger (local-on-host / GH Actions / on-demand) chosen on cost grounds.
- Beta/staging/production run on one GPU host, isolated by endpoint (namespace/port/DNS), never by a
  separate instance; stage naming reconciled to beta/staging/production across the codebase.
- The GPU host stays cheap to maintain (single instance, scale-to-zero GPU, one AMI shared).
- Pipeline promotes beta→staging→production in fixed order with halt-on-failure once code lands in the
  account.
- **All upstream/base versions are the latest at spec time, verified against upstream — not memory.**

## 4. Design decisions the new chat MUST resolve (call these out explicitly)

- **Gradle-in-Nix:** `gradle2nix` vs a fixed-output `stdenv.mkDerivation` with a vendored dependency
  lock. JVM Gradle builds are network-heavy; pick a hermetic approach and document it per fork.
- **Plugin packaging boundary:** are `lavasrc-plugin` / `youtube-plugin-sabr` / the lavaplayer patch
  standalone flake *outputs* (jars) consumed by the Lavalink image, or built inline in the Lavalink
  flake? (They're separate repos, so separate flakes with the Lavalink flake taking them as inputs is
  the clean answer — confirm.)
- **Where builds run without a paid server** (the core cost question) — local-on-GPU-host vs GH
  Actions vs on-demand ephemeral — with the actual $ tradeoff, and how CDK Pipelines/CodeBuild in
  `pipeline-stack.ts` is kept or replaced so no build compute bills.
- **Nix binary cache** backend (S3 / attic / cachix) and how beta/staging/prod all pull the same
  prebuilt closures (build once, deploy thrice).
- **Single-host three-endpoint isolation mechanism** — EKS namespaces + Ingress hostnames vs
  per-stage NixOS service instances vs ports — and how each stage's GPU endpoint maps onto the one GPU
  host.
- **Stage rename** beta/gamma/prod → beta/staging/production and every file that encodes the order.
- **Upstream version pinning:** how flake inputs pin the *latest verified* upstream commit/tag, and
  the `nix flake update` sync workflow for future upstream merges.

## 5. Verification the spec should specify

- `nix flake check` + `nix build .#<image>` for every fork flake and every component flake evaluates
  and (where a builder is available) builds; jars are real (no placeholders).
- `python3 tools/gate_base_image.py` → all components PASS, zero SKIP, zero distro base.
- `nixos-generate -f amazon` (or the `infra/ami` flake) builds the GPU AMI.
- The composed CDK app still synthesizes (`npx cdk synth`) with stages renamed and single-host
  endpoints wired; existing jest suite green.
- A documented, reproducible path shows: push to `hellodj` account → build via Nix (no paid server) →
  cache/registry → pipeline promotes beta→staging→production on the one GPU host.
- Every pinned upstream input is the latest version verified against upstream at spec time.

## 6. Files the new chat should read first

- The four fork repos' build files: `Lavalink/{build.gradle.kts,settings.gradle.kts,Dockerfile.custom}`,
  `lavaplayer/build.gradle.kts`, `LavaSrc/build.gradle`, `youtube-source/build.gradle.kts`.
- `platform/components/lavalink/flake.nix` (placeholder JAR derivations to replace) + its README.
- `platform/components/potoken-server/flake.nix`, `platform/components/yt-cipher/flake.nix`,
  `platform/components/spotify-stream/flake.nix` — the Nix OCI image reference pattern.
- `platform/infra/ami/gpu-node.nix` + `platform/infra/ami/flake.nix` — native-Nix AMI build.
- `platform/infra/lib/pipeline-stack.ts` — the existing CDK Pipelines/CodeBuild path to reconcile
  with "no build server."
- `platform/infra/lib/eks-stack.ts`, `workloads-stack.ts`, `component-workloads.ts` — how workloads
  land on the cluster/host.
- `platform/components/hellodj_platform_logic/{dns_naming.py,promotion.py}` — stage order + DNS naming
  to reconcile to beta/staging/production.
- `platform/NIX-CONVERSION-CONTEXT.md` — the sibling Nix-image-packaging effort for the 7 Python
  components (prerequisite/parallel work).
- Steering: `.kiro/steering/hellodj-architecture.md` (fork provenance, plugin set, JVM 21) and the
  global NixOS-workflow steering (declarative, flake inputs via `github:owner/repo/branch`,
  `nix flake update` sync).

## 7. Explicit reminders for the author chat

- **Verify latest upstream versions live** (Lavalink, lavaplayer, LavaSrc, youtube-source releases;
  Temurin/JDK; nixpkgs; nixos-generators; Karpenter; EKS k8s version) — do not trust memory.
- **Kill the Alpine/Debian bases** — the current `Lavalink/Dockerfile.custom` (Alpine) and
  `web-ui/Dockerfile` (Debian) are the anti-pattern this migration removes.
- **Cost is the primary constraint** — every design choice needs a "does this bill when idle?" answer,
  and the answer should be "no" wherever possible.
