# Requirements Document

## Introduction

This feature migrates the HelloDJ codebase and its four modified upstream JVM forks into the
`hellodj` Git account and makes **every** build artifact — all OCI container images and the GPU AMI —
produced natively by the Nix build system, with **no distribution base image (Ubuntu, Debian, or
Alpine) anywhere**. It also migrates all Java/JVM dependencies to the latest Java LTS (Temurin 25),
establishes a build path that requires **no persistent paid build server**, and consolidates the
Beta / Staging / Production deployment stages onto a **single NixOS GPU host**, isolated by endpoint
rather than by separate instances.

The primary, non-negotiable constraint is **cost**: no resource may bill while idle if a serverless,
on-demand, or local build/deploy path can produce the same result. The secondary constraint is
**latest-verified upstream versions**: every pinned upstream and base version is the latest version
verified against upstream (releases, tags, nixpkgs) at spec authoring time, not assumed from memory.

This spec builds on two existing, in-flight efforts and must reconcile with them:

- `aws-saas-replatform` (fully implemented under `platform/`): CDK Pipelines Beta→Gamma→Prod
  (`infra/lib/pipeline-stack.ts`, uses CodeBuild), the native-Nix GPU AMI (`infra/ami/`), the Nix OCI
  image flakes for `lavalink`/`spotify-stream`/`yt-cipher`/`potoken-server`, the base-image gate
  (`tools/gate_base_image.py`), and the stage/DNS/promotion logic in `hellodj_platform_logic`.
- `nix-image-packaging` (companion, referenced by `platform/NIX-CONVERSION-CONTEXT.md`): the 7 Python
  components still lacking a Nix image flake. That work is a **prerequisite/parallel** dependency:
  the base-image gate must reach a state where it **PASSES** for all components rather than SKIPPING
  any of them.

This is a delivery-and-build spec. It does **not** change application runtime behavior; it changes
how artifacts are built, versioned, migrated, and promoted.

## Glossary

- **Nix_Build_System**: The Nix package manager and flake toolchain that produces all artifacts.
  Containers via `pkgs.dockerTools.buildLayeredImage`; AMIs via `nixos-generators -f amazon`; JVM
  jars via Nix-wrapped Gradle derivations.
- **Fork_Repo**: One of the four migrated upstream forks — `Lavalink`, `lavaplayer`, `LavaSrc`,
  `youtube-source` — each becoming an independent repository under the `hellodj` account.
- **Upstream_Remote**: A git remote named `upstream` on each Fork_Repo pointing at the original
  upstream project, preserved to enable future `nix flake update` synchronization.
- **Fork_Flake**: The `flake.nix` in a Fork_Repo that produces that fork's jar artifact(s) via a
  Nix-wrapped Gradle build, and — for `Lavalink` — a Nix OCI container image.
- **Component_Flake**: The `flake.nix` for a platform component under `platform/components/<name>/`
  that produces the component's OCI image.
- **Lavalink_Image**: The Nix OCI container image built from the `Lavalink` Fork_Flake, consuming the
  plugin jars produced by the `LavaSrc`, `youtube-source`, and `lavaplayer` Fork_Flakes.
- **Plugin_Jar**: A jar output consumed by the Lavalink_Image — `lavasrc-plugin`,
  `youtube-plugin-sabr`, and the lavaplayer fMP4 HLS patch consumed by the custom `Lavalink.jar`.
- **Temurin_25**: Eclipse Temurin OpenJDK 25, the latest Java Long-Term-Support release (released
  September 2025; per Eclipse Adoptium and the Red Hat build of OpenJDK, the LTS lines are 8u, 11u,
  17u, 21u, and 25u). Temurin 26 is a non-LTS feature release and is **not** the migration target.
- **GPU_AMI**: The native-Nix-built EBS-backed Amazon Machine Image for the NixOS GPU transcode host,
  produced from `infra/ami/gpu-node.nix` via `nixos-generators` `amazon-image`.
- **Nix_Binary_Cache**: A Nix binary cache backend (evaluated among S3-backed, attic, or cachix) that
  stores prebuilt closures so an artifact is built once and reused across all three stages.
- **Container_Registry**: The OCI image registry (ECR) the pipeline deploys images from.
- **Base_Image_Gate**: The build-stage check (`platform/tools/gate_base_image.py`, backed by
  `hellodj_platform_logic.base_image_gate.check_base`) that rejects any non-Nix-produced image base.
- **Deployment_Stage**: One of the three ordered stages — Beta, Staging, Production — modeled by the
  `DeploymentStage` enum (currently `BETA`/`GAMMA`/`PROD`; `GAMMA` is reconciled to Staging).
- **GPU_Host**: The single NixOS GPU host on which all three Deployment_Stages run, isolated by
  endpoint (EKS namespace / port / DNS hostname), not by separate instances.
- **Stage_Endpoint**: The distinct endpoint (namespace, port, and DNS hostname) that identifies one
  Deployment_Stage on the shared GPU_Host.
- **Build_Trigger**: The mechanism that initiates a Nix build (local-on-GPU-host, GitHub Actions with
  Nix, or on-demand ephemeral builder), chosen on cost grounds.
- **Promotion_Controller**: The pure decision logic (`hellodj_platform_logic.promotion.promote`) that
  promotes stages in fixed order and halts on the first failure.
- **Distro_Base**: An Ubuntu, Debian, or Alpine container base image — the anti-pattern this feature
  eliminates from all artifacts.

## Requirements

### Requirement 1: Migrate forks into the `hellodj` account as independent repos

**User Story:** As the platform maintainer, I want each modified upstream fork moved into the
`hellodj` account as its own repository with its upstream remote preserved, so that the code pipeline
owns the source and future upstream syncs remain possible.

#### Acceptance Criteria

1. THE Nix_Build_System project SHALL provide, for each of the four Fork_Repos (`Lavalink`,
   `lavaplayer`, `LavaSrc`, `youtube-source`), a separate repository under the `hellodj` account,
   yielding exactly four independent repositories.
2. WHEN a Fork_Repo is migrated to the `hellodj` account, THE Fork_Repo SHALL contain a git remote
   named `upstream` whose fetch URL resolves to the original upstream project from which that fork
   was derived.
3. THE `Lavalink` Fork_Repo SHALL contain a branch named `dev`, and THE `Lavalink` Fork_Repo SHALL
   designate `dev` as the build branch consumed by the Lavalink_Image build.
4. WHEN the HelloDJ application code is present in the `hellodj` account, THE code pipeline SHALL
   build and promote artifacts sourced only from repositories under the `hellodj` account.
5. WHERE a Fork_Flake declares another Fork_Repo as a flake input, THE Fork_Flake SHALL reference
   that input in the form `github:hellodj/<repo>/<branch>`.
6. IF a Fork_Repo cannot be created under the `hellodj` account or its `upstream` remote cannot be
   established during migration, THEN THE migration SHALL report an error identifying the affected
   Fork_Repo and SHALL leave the already-migrated Fork_Repos unchanged.

### Requirement 2: Per-fork Nix-wrapped Gradle build recipes

**User Story:** As a build engineer, I want each fork to build its jar artifacts through a Nix-wrapped
Gradle derivation, so that jar builds are hermetic and reproducible without a distro toolchain.

#### Acceptance Criteria

1. THE `Lavalink` Fork_Flake SHALL produce the custom `Lavalink.jar` (including the lavaplayer fMP4
   HLS patch and Lavalink v4 server) via a Nix-wrapped Gradle build.
2. THE `lavaplayer` Fork_Flake SHALL produce the lavaplayer jar artifact consumed by the `Lavalink`
   build via a Nix-wrapped Gradle build.
3. THE `LavaSrc` Fork_Flake SHALL produce the `lavasrc-plugin` jar via a Nix-wrapped Gradle build.
4. THE `youtube-source` Fork_Flake SHALL produce the `youtube-plugin-sabr` jar via a Nix-wrapped
   Gradle build.
5. WHILE the Nix build phase is executing, THE Fork_Flake SHALL resolve all Gradle dependencies from
   vendored/locked sources and SHALL make zero outbound network requests for dependency resolution.
6. WHEN `nix build .#<jar>` is run for a Fork_Flake, THE Fork_Flake SHALL produce a jar artifact
   whose manifest declares a Main-Class (or plugin entrypoint) and whose contents include compiled
   class files, rather than a zero-byte or placeholder marker file.
7. WHEN `nix flake check` is run for a Fork_Flake, THE Fork_Flake SHALL evaluate to completion with
   an exit status of 0 and emit no evaluation errors.
8. IF a Fork_Flake build encounters a compilation error or an unresolved dependency, THEN THE
   Fork_Flake build SHALL terminate with a non-zero exit status, produce no jar output in the result
   path, and emit an error indicating the compilation or dependency failure.

### Requirement 3: Migrate all JVM forks and the Lavalink image base to Temurin 25 (LTS)

**User Story:** As the platform maintainer, I want all Java/JVM dependencies on the latest Java LTS,
so that the forks and the Lavalink image run on a supported long-term-support runtime.

#### Acceptance Criteria

1. WHEN the `Lavalink` Fork_Flake is built, THE Fork_Flake SHALL use Temurin_25 (a Temurin
   distribution reporting Java feature version 25) as its Gradle build toolchain and SHALL complete
   the build without toolchain-resolution errors.
2. WHEN the `lavaplayer` Fork_Flake is built, THE Fork_Flake SHALL use Temurin_25 (a Temurin
   distribution reporting Java feature version 25) as its Gradle build toolchain and SHALL complete
   the build without toolchain-resolution errors.
3. WHEN the `LavaSrc` Fork_Flake is built, THE Fork_Flake SHALL use Temurin_25 (a Temurin
   distribution reporting Java feature version 25) as its Gradle build toolchain and SHALL complete
   the build without toolchain-resolution errors.
4. WHEN the `youtube-source` Fork_Flake is built, THE Fork_Flake SHALL use Temurin_25 (a Temurin
   distribution reporting Java feature version 25) as its Gradle build toolchain and SHALL complete
   the build without toolchain-resolution errors.
5. THE Lavalink_Image runtime base SHALL be a Nix-built Temurin_25 JRE that reports Java feature
   version 25 when queried at container startup, replacing the current Temurin 21 base.
6. WHERE a Fork_Repo declares a Gradle Java or Kotlin toolchain or language level (for example
   `jvmToolchain(21)`, `JvmTarget.JVM_21`, `sourceCompatibility`), THE Fork_Flake SHALL build to
   completion under Temurin_25 without language-level or toolchain-compatibility errors and SHALL
   record the confirmed declared level for that fork.
7. THE migration target for all JVM forks and the Lavalink image base SHALL be Temurin feature
   version 25 (the LTS release) and SHALL NOT be any Temurin feature release other than 25.
8. IF a Fork_Flake build fails under Temurin_25 due to a toolchain-resolution, language-level, or
   compilation error, THEN THE Fork_Flake SHALL fail the build with an error indicating the
   incompatible fork and level, and SHALL NOT produce a build artifact for that fork.

### Requirement 4: Wire real plugin jars into the Lavalink Nix image

**User Story:** As a build engineer, I want the Lavalink image to consume the real jars from the
sibling fork flakes, so that the placeholder JAR derivations are eliminated.

#### Acceptance Criteria

1. THE `Lavalink` Fork_Flake SHALL declare the `lavaplayer`, `LavaSrc`, and `youtube-source`
   Fork_Flakes as `github:hellodj/<repo>/<branch>` flake inputs.
2. THE Lavalink_Image SHALL be built with `pkgs.dockerTools.buildLayeredImage`.
3. THE Lavalink_Image SHALL include the custom `Lavalink.jar` produced by the `Lavalink` Fork_Flake
   at path `/opt/Lavalink/Lavalink.jar`.
4. WHEN the Lavalink_Image is assembled, THE Lavalink_Image SHALL include the `lavasrc-plugin`
   Plugin_Jar sourced from the `LavaSrc` Fork_Flake at path `/opt/Lavalink/plugins/` and the
   `youtube-plugin-sabr` Plugin_Jar sourced from the `youtube-source` Fork_Flake at path
   `/opt/Lavalink/plugins/`.
5. THE `platform/components/lavalink/flake.nix` derivations currently marked `TODO(artifact-source)`
   (the `mkPlaceholderJar` outputs for `Lavalink.jar`, `youtube-plugin-sabr.jar`, and
   `lavasrc-plugin-4.8.3.jar`) SHALL be replaced by derivations that fetch or build the migrated jars
   from the sibling Fork_Flakes.
6. THE `Lavalink.jar`, `lavasrc-plugin`, and `youtube-plugin-sabr` artifacts included in the
   Lavalink_Image SHALL each be a runnable jar and SHALL NOT contain any placeholder marker output
   (for example the `PLACEHOLDER ARTIFACT` text emitted by `mkPlaceholderJar`).
7. IF a Plugin_Jar or the custom `Lavalink.jar` cannot be resolved from its source Fork_Flake, THEN
   THE `Lavalink` Fork_Flake build SHALL fail fast, SHALL produce no Lavalink_Image, and SHALL emit
   an error identifying by name the missing artifact.
8. THE application configuration `application.yml` SHALL NOT be present in the Lavalink_Image
   filesystem and SHALL be readable by Lavalink only from the runtime-mounted path
   `/opt/Lavalink/application.yml` injected at container start.

### Requirement 5: Every artifact is Nix-produced with no distro base

**User Story:** As the platform maintainer, I want all container images and the GPU AMI produced by
Nix with no Ubuntu, Debian, or Alpine base, so that the platform is fully Nix-native.

#### Acceptance Criteria

1. THE Nix_Build_System SHALL produce every platform component OCI image via
   `pkgs.dockerTools.buildLayeredImage`.
2. THE Nix_Build_System SHALL produce the GPU_AMI via `nixos-generators` `amazon-image`.
3. THE `Lavalink` Fork_Repo SHALL replace the Alpine-based `Dockerfile.custom`
   (`FROM eclipse-temurin:21-jre-alpine`) with a Nix-produced Lavalink_Image.
4. THE `web-ui` component SHALL replace its Debian-based `Dockerfile` (`node:22-slim` +
   `python:3.11-slim`) with a Nix-produced image.
5. THE Nix_Build_System SHALL NOT reference a Distro_Base in a base-declaring position in any
   Fork_Flake or Component_Flake.
6. WHEN the Base_Image_Gate is run over the complete set of deployable components, THE
   Base_Image_Gate SHALL report PASS for every component, SKIP for zero components, and detect zero
   Distro_Base references.
7. THE Base_Image_Gate SHALL be executed as a pipeline build step, and compliance SHALL NOT be
   claimed unless that step has run and reported PASS for every component.
8. WHERE a component previously reported SKIP because it lacked a Component_Flake, THE component
   SHALL have a Component_Flake so the Base_Image_Gate enforces rather than skips it.
9. IF the Base_Image_Gate detects a Distro_Base reference or a component that does not report PASS,
   THEN THE pipeline build step SHALL fail, SHALL block the compliance claim, and SHALL identify the
   offending component.

### Requirement 6: No persistent paid build server

**User Story:** As the budget owner, I want builds to run without a persistent paid build server, so
that no build compute bills while idle.

#### Acceptance Criteria

1. THE build path SHALL produce all container images and the GPU_AMI without provisioning a build
   server that continues to incur compute charges during the interval between builds (zero
   build-compute cost while no build is running).
2. THE build path SHALL publish built closures to a Nix_Binary_Cache and built images to the
   Container_Registry.
3. WHERE the pipeline orchestrates deployment, THE pipeline SHALL deploy prebuilt closures pulled
   from the Nix_Binary_Cache and Container_Registry rather than compiling artifacts on paid build
   compute.
4. THE build design SHALL reconcile with the existing CDK Pipelines/CodeBuild path in
   `platform/infra/lib/pipeline-stack.ts` such that no CodeBuild compute is billed for building
   images or the AMI.
5. THE build design SHALL select exactly one Build_Trigger from the set {local-on-GPU-host, GitHub
   Actions with Nix, on-demand ephemeral builder} and SHALL record a written cost justification that
   compares the per-build and idle cost of the selected Build_Trigger against the rejected
   alternatives.
6. WHEN a build that provisioned ephemeral build compute completes (successfully or with failure),
   THE build compute SHALL be torn down within 300 seconds of build completion so that it does not
   continue to incur compute charges.
7. WHERE ephemeral build compute is provisioned, THE build compute SHALL be assigned a maximum
   lifetime not exceeding 3 hours (10800 seconds) after which it is forcibly terminated even if
   teardown fails or the build process crashes.
8. IF the forced termination at maximum lifetime does not confirm the ephemeral build compute has
   stopped, THEN THE build path SHALL emit an alert identifying the still-running build compute so
   the ongoing cost is surfaced for manual intervention.
9. WHEN a build completes on ephemeral build compute, THE build path SHALL record confirmation that
   no build compute remains running, retaining the ephemeral resource identifier and teardown
   timestamp.

### Requirement 7: Nix binary cache — build once, deploy thrice

**User Story:** As the platform maintainer, I want all three stages to pull the same prebuilt
closures, so that artifacts are built once and reused across Beta, Staging, and Production.

#### Acceptance Criteria

1. THE Nix_Build_System SHALL record a selection of exactly one Nix_Binary_Cache backend from the
   set {S3-backed, attic, cachix}, together with a cost evaluation that states, for each of the three
   candidate backends, its estimated idle monthly cost and its estimated per-artifact
   storage-and-transfer cost, such that a reviewer can confirm the selected backend has the lowest
   recorded idle cost or a recorded justification for not selecting the lowest.
2. WHEN an artifact has been built and pushed to the Nix_Binary_Cache, THE Beta, Staging, and
   Production stages SHALL each pull, for that artifact, a closure whose Nix store path hash is
   identical to the pushed artifact's store path hash.
3. IF an identical closure (matching Nix store path hash) for an artifact already exists in the
   Nix_Binary_Cache, THEN THE build path SHALL reuse the cached closure and SHALL NOT rebuild that
   artifact for any stage.
4. IF a required closure is absent from the Nix_Binary_Cache at the time a stage deploys, THEN THE
   deployment SHALL halt for that stage, SHALL surface an error identifying the missing closure by
   its store path, and SHALL NOT substitute an artifact obtained from any source other than the
   Nix_Binary_Cache.
5. WHERE an explicit rebuild is requested, THE build path SHALL be permitted to rebuild the artifact
   and re-push the resulting closure to the Nix_Binary_Cache.
6. IF the Nix_Binary_Cache does not respond within 30 seconds or fails after 3 consecutive retry
   attempts during a build, THEN THE build path SHALL be permitted to rebuild the artifact locally
   and SHALL record that the rebuild occurred due to cache unreachability.
7. WHEN an artifact build completes on the build path, THE build path SHALL push the resulting
   closure to the Nix_Binary_Cache and SHALL confirm the pushed closure is retrievable from the
   Nix_Binary_Cache before the artifact is marked available for stage deployment.

### Requirement 8: Single GPU host, three stages isolated by endpoint

**User Story:** As the budget owner, I want Beta, Staging, and Production to run on one GPU host
isolated by endpoint, so that I do not pay for separate per-stage instances.

#### Acceptance Criteria

1. THE Beta, Staging, and Production stages SHALL run on the single GPU_Host.
2. THE deployment SHALL isolate each Deployment_Stage by a distinct Stage_Endpoint (EKS namespace,
   port, and DNS hostname) on the shared GPU_Host.
3. THE deployment SHALL NOT provision a separate GPU instance per Deployment_Stage.
4. THE GPU_Host SHALL use a single shared GPU_AMI across all three Deployment_Stages.
5. WHILE the GPU has had no active transcode workload for a continuous idle window (default 300
   seconds, configurable within the range 60–900 seconds), THE GPU_Host SHALL scale the GPU to zero
   so the GPU bills only under load.
6. WHEN a workload requiring the GPU arrives while the GPU is scaled to zero, THE GPU_Host SHALL
   scale the GPU back up to serve the workload.
7. IF a request targets one Stage_Endpoint, THEN THE deployment SHALL route that request only to the
   workload of the corresponding Deployment_Stage and SHALL NOT route it to another stage's workload.

### Requirement 9: Reconcile stage naming to Beta / Staging / Production

**User Story:** As the platform maintainer, I want the stage naming reconciled to
Beta/Staging/Production across the codebase, so that naming is consistent everywhere.

#### Acceptance Criteria

1. THE `DeploymentStage` enum SHALL define exactly three members naming the stages Beta, Staging, and
   Production, replacing the prior `GAMMA` member with the Staging member and retaining the Beta and
   Production members.
2. THE `dns_naming.py`, `promotion.py`, `pipeline-stack.ts`, and Route 53 DNS records SHALL contain
   zero occurrences of the prior `GAMMA`/`Gamma`/`gamma` stage identifier and SHALL reference the
   reconciled Beta, Staging, and Production identifiers in every location that previously referenced
   a stage identifier.
3. WHEN a Deployment_Stage and a region are both provided, THE `dns_naming` logic SHALL return a DNS
   name that is a subdomain of the `hellodj.bot` zone and that includes both the reconciled stage
   name and the region.
4. IF the `dns_naming` logic is invoked with a stage but no region, THEN THE `dns_naming` logic SHALL
   NOT return a DNS name and SHALL raise an error indicating that both a stage and a region are
   required.
5. IF the `dns_naming` logic is invoked with a region but no stage, THEN THE `dns_naming` logic SHALL
   NOT return a DNS name and SHALL raise an error indicating that both a stage and a region are
   required.
6. THE reconciled naming SHALL preserve the fixed promotion order Beta → Staging → Production.

### Requirement 10: Pipeline promotes in fixed order and halts on failure

**User Story:** As the release manager, I want the pipeline to promote Beta → Staging → Production in
fixed order and halt on the first failure, so that a broken build never reaches Production.

#### Acceptance Criteria

1. WHEN a new commit is pushed to the tracked branch of the HelloDJ application repository in the
   `hellodj` account, THE pipeline SHALL build all required artifacts and, only after the build
   succeeds, begin promotion of the Deployment_Stages in the fixed order Beta → Staging → Production.
2. IF the build fails before any Deployment_Stage is deployed, THEN THE pipeline SHALL halt, deploy
   no Deployment_Stage, and record a build-failure result identifying the failed build step.
3. WHILE promoting, THE Promotion_Controller SHALL deploy a given Deployment_Stage only after every
   earlier Deployment_Stage in the fixed order has reached the succeeded result (its deployment
   completed and its post-deployment verification passed).
4. IF a Deployment_Stage does not reach the succeeded result (its deployment or post-deployment
   verification does not pass), THEN THE Promotion_Controller SHALL halt promotion, deploy no later
   Deployment_Stage, and explicitly record every later Deployment_Stage with a skipped (not-deployed)
   result.
5. THE Promotion_Controller SHALL always attempt to deploy the first Deployment_Stage (Beta) once the
   build has succeeded, because Beta has no predecessor Deployment_Stage.

### Requirement 11: Pin the latest verified upstream and base versions

**User Story:** As the platform maintainer, I want every pinned upstream and base version to be the
latest verified against upstream at pin time, so that the build starts from current sources rather
than stale memory values.

#### Acceptance Criteria

1. WHERE a flake input pins an upstream version (Lavalink, lavaplayer, LavaSrc, youtube-source,
   Temurin/JDK, nixpkgs, nixos-generators, Karpenter, EKS Kubernetes version), THE flake input SHALL
   pin a version whose identifier equals the version identifier resolved from that input's upstream
   source at pin time.
2. THE Temurin/JDK pin SHALL equal Temurin_25, and its version identifier SHALL equal the latest
   Long-Term-Support release published by Eclipse Adoptium at pin time.
3. THE flake inputs SHALL reference upstream via `github:owner/repo/branch` inputs so `nix flake
   update <input>` synchronizes future upstream merges.
4. WHEN the maintainer runs `nix flake update <input>` followed by a rebuild, THE update workflow
   SHALL update that input's pinned revision to the current upstream revision of the referenced
   branch and rebuild from the updated pin, consistent with the declarative NixOS workflow.
5. IF, at pin time, a pinned version identifier does not equal the version identifier resolved from
   its upstream source, THEN THE pin SHALL be rejected and an indication SHALL identify the input
   whose pinned version does not match upstream, and the prior pinned revision SHALL be retained
   unchanged.
6. IF, at pin time, the upstream source for an input cannot be resolved, THEN THE pin operation for
   that input SHALL fail with an indication identifying the unresolved input, and the prior pinned
   revision SHALL be retained unchanged.

### Requirement 12: Verifiable, reproducible build-and-deploy path

**User Story:** As a reviewer, I want a documented set of verification commands, so that I can confirm
the entire build-and-deploy path works end to end.

#### Acceptance Criteria

1. WHEN `nix flake check` is run for every Fork_Flake and every Component_Flake, THE flakes SHALL
   evaluate to completion with an exit status of 0 and emit no evaluation errors.
2. WHERE a Nix builder is available for the target system, WHEN `nix build .#<image>` is run for
   every Fork_Flake and Component_Flake, THE build SHALL exit with status 0 and produce a real OCI
   image (and, for jar outputs, a real jar containing compiled class files) with no placeholder
   marker artifacts.
3. WHEN `python3 tools/gate_base_image.py` is run, THE Base_Image_Gate SHALL report PASS for every
   component, SKIP for zero components, and zero Distro_Base references.
4. WHEN `nixos-generate -f amazon` (or the `infra/ami` flake build) is run, THE build SHALL exit with
   status 0 and produce the GPU_AMI image artifact.
5. WHEN `npx cdk synth` is run, THE CDK app SHALL synthesize successfully with the reconciled stage
   names (Beta, Staging, Production) and the single-host Stage_Endpoints wired.
6. WHEN the existing jest suite is run, THE jest suite SHALL pass with zero failing tests.
7. IF any of the verification commands in criteria 1–6 exits non-zero or reports a failure, THEN THE
   verification SHALL be treated as failed and SHALL identify the failing command and artifact.
8. THE documentation SHALL provide an enumerated, copy-runnable command set describing a reproducible
   path: push to the `hellodj` account → Nix build with no paid build server → publish to
   Nix_Binary_Cache and Container_Registry → promote Beta → Staging → Production on the single
   GPU_Host.
