# Requirements Document

## Introduction

Today the HelloDJ CDK application lives inside the `hellodj` monorepo at `platform/infra/`, co-located with the repo-wide gates (`platform/tools/`), encrypted cache secrets (`platform/secrets/`), the closure/pin manifests (`platform/closures.toml`, `platform/pins*.toml`, `platform/pyproject.toml`), the pure-logic Python package (`platform/components/hellodj_platform_logic/`), and the 12 bot-adjacent workload component sources (`platform/components/*`). Because the deployment pipeline's synth source is the `hellodj` repo, any CDK-only change forces a commit against the bot repository.

This feature migrates the CDK application and everything that `cdk synth` and the repo-wide gates depend on into a new standalone CodeCommit repository and package, `hellodj-cdk` (workspace folder `/home/celes/sources/celesrenata/hellodj-cdk`). After the migration, the pipeline's primary synth source is `hellodj-cdk`, while the 12 per-component Nix image builds take `hellodj` as an additional source input (mirroring the established 4-fork additional-input pattern). The result: CDK-only changes are pushed to `hellodj-cdk` and trigger the pipeline without touching the bot repository, while bot and component source changes continue to flow from `hellodj`.

The requirements below are derived from the approved design and preserve requirement numbering so the design's Correctness Properties (which reference Requirements 1.1, 1.2, 2.1, 2.2, 2.3, 3.1, 4.1, 4.2) remain valid. They also cover the design areas that were not previously numbered: the what-moves-vs-stays boundary, the `hellodj_platform_logic` ownership decision (Option A), the bootstrap ordering, the "parsed/deployed as it synths" reconciliation, and the steering-doc synchronization.

## Glossary

- **hellodj-cdk repo**: The new standalone AWS CodeCommit repository (build branch `main`, no upstream) and its corresponding workspace package at `/home/celes/sources/celesrenata/hellodj-cdk`, which holds the CDK application, the repo-wide gates, the encrypted cache secrets, the closure/pin manifests, `pyproject.toml`, and the shared `hellodj_platform_logic` package.
- **hellodj repo**: The existing AWS CodeCommit repository (build branch `main`) that retains the bot sources, the Kubernetes manifests, and the 12 workload component sources under `platform/components/*`.
- **SourceStack**: The CDK stack that declaratively provisions the CodeCommit repositories from the `SOURCE_REPOS` array of `SourceRepoSpec` entries, grants pull access to build roles, and emits clone-URL and build-branch outputs.
- **PipelineStack**: The CDK stack that defines the `hellodj-pipeline` CDK Pipelines pipeline, including the synth step and the per-component build steps.
- **Primary synth source**: The single CodePipeline source repository whose checkout is the working root (`$CODEBUILD_SRC_DIR`) for the synth CodeBuildStep that runs `cdk synth` and the repo-wide gates.
- **Additional source input**: A secondary CodePipeline source mounted alongside the primary source in a CodeBuildStep, used by the per-component build steps to access component sources and vendored shared code, following the existing 4-fork `additionalInputs` pattern.
- **selfMutation**: The CDK Pipelines feature that redeploys the pipeline definition automatically on a source push. It is deliberately disabled (`selfMutation: false`) for `hellodj-pipeline` because stage stacks apply Kubernetes manifests via the EKS kubectl handler Lambda, and self-mutation triggers cross-stack custom-resource failures.
- **hellodj_platform_logic**: A pure-logic Python package (no side effects) imported by both the CDK jest tests and Python gates (CDK-side) and by the 12 per-component Nix builds that vendor it into each component tree (bot-side).
- **Build closure**: The pinned dependency set resolved by `resolve_closure.py` from `closures.toml` and the `pins*.toml` manifests, verified by the repo-wide gates during synth.
- **Repo-wide gates**: The Python gate programs (`gate_base_image.py`, `gate_style.py`, `gate_pins.py`, `gate_dependencies.py`, `resolve_closure.py`, `check_line_count.py`) that run in the synth step against the CDK repository.
- **migrate_repos.py**: The transactional migration tool that creates and seeds CodeCommit repositories from local working trees via `git push --mirror`, with halt-on-first-failure semantics and history verification.

## Requirements

### Requirement 1: Source-repo topology

**User Story:** As a platform owner, I want the CDK application to have its own CodeCommit repository declared as infrastructure, so that the repository set is provisioned declaratively and the CDK app is a first-class source alongside the bot and JVM forks.

#### Acceptance Criteria

1. THE SourceStack SHALL declare a CodeCommit repository named `hellodj-cdk` in its synthesized template.
2. THE SourceStack SHALL declare exactly six CodeCommit repositories, WHERE the `hellodj-cdk` entry has a build branch of `main` and no upstream URL, and the other five entries (`hellodj`, `Lavalink`, `lavaplayer`, `LavaSrc`, `youtube-source`) retain their existing build branches.
3. WHEN the SourceStack provisions the `hellodj-cdk` repository, THE SourceStack SHALL grant pull access to the build roles and emit clone-URL and build-branch outputs for `hellodj-cdk`.

### Requirement 2: Pipeline rewiring

**User Story:** As a platform owner, I want the pipeline to synth from `hellodj-cdk` and build components from `hellodj`, so that CDK synth is decoupled from the bot source of truth while component builds continue to source the bot repository.

#### Acceptance Criteria

1. WHERE no `repoString` override is supplied, THE PipelineStack SHALL resolve the synth step's primary source input to the `hellodj-cdk` repository.
2. THE PipelineStack SHALL define every synth command working directory under the `hellodj-cdk` layout, WHERE CDK commands run in `infra/` and gate/tooling commands run at the repository root, and no synth command SHALL reference a `platform/infra` or `platform/tools` path.
3. THE PipelineStack SHALL include the `hellodj-cdk` CodeCommit repository ARN in the `codecommit:GitPull` policy resource list, and SHALL retain the ARNs for the other five repositories.
4. THE PipelineStack SHALL provide the `hellodj` repository as an additional source input to the per-component build steps, so that the 12 component builds source their component trees from `hellodj`.
5. THE PipelineStack SHALL set the synth step `primaryOutputDirectory` to `infra/cdk.out`.

### Requirement 3: CDK-only change isolation (user goal)

**User Story:** As a platform owner, I want CDK-only changes to be confined to the `hellodj-cdk` repository, so that infrastructure edits trigger the pipeline without requiring a commit to the bot repository.

#### Acceptance Criteria

1. WHERE a change set is confined to the CDK application, THE hellodj-cdk repo SHALL contain all changed paths, and the set of changed paths intersecting `platform/components/*` in the hellodj repo SHALL be empty.
2. WHEN a change is pushed to the `hellodj-cdk` repository, THE hellodj-pipeline SHALL trigger a synth execution without requiring a commit to the `hellodj` repository.

### Requirement 4: Migration correctness

**User Story:** As a platform owner, I want the migrated package to keep all existing tests green and to be seeded transactionally, so that the repository split introduces no regression and the new repository is created safely.

#### Acceptance Criteria

1. WHEN the moved package is verified after migration, THE hellodj-cdk package SHALL keep the 226 CDK jest tests passing and SHALL keep the relocated Python tooling passing ruff and pytest under the moved `pyproject.toml`.
2. WHEN migrate_repos.py runs with the `hellodj-cdk` entry added to its `REPOS` list, THE migrate_repos.py tool SHALL preserve halt-on-first-failure transactional semantics, and WHEN re-run against an already-created `hellodj-cdk` repository, THE migrate_repos.py tool SHALL report the repository as created without error.

### Requirement 5: What-moves-vs-what-stays boundary

**User Story:** As a platform owner, I want a precise boundary between what moves to `hellodj-cdk` and what stays in `hellodj`, so that synth/gate dependencies relocate together while bot-adjacent sources remain with the bot.

#### Acceptance Criteria

1. THE hellodj-cdk repo SHALL contain the relocated CDK application at `infra/`, the repo-wide gates at `tools/`, the encrypted cache secrets at `secrets/`, and the `closures.toml`, `pins.toml`, `pins.upstream.toml`, and `pyproject.toml` manifests at the repository root.
2. THE hellodj repo SHALL retain the 12 workload component sources under `platform/components/*`, the `bot/` sources, and the `kube/` manifests.
3. THE hellodj-cdk repo SHALL contain the `hellodj_platform_logic` package under `shared/hellodj_platform_logic/`.

### Requirement 6: hellodj_platform_logic ownership (single source of truth)

**User Story:** As a platform owner, I want `hellodj_platform_logic` to have a single source of truth in `hellodj-cdk` that components vendor from an additional input, so that the shared logic is not duplicated and the CDK owns the logic its tests assert.

#### Acceptance Criteria

1. THE hellodj-cdk repo SHALL be the single source of truth for the `hellodj_platform_logic` package, located at `shared/hellodj_platform_logic/`.
2. WHEN a per-component build vendors the shared logic, THE per-component build step SHALL copy `hellodj_platform_logic` from the `hellodj-cdk` additional-input `shared/` path rather than from a `hellodj/platform/components/` path.
3. WHEN the `hellodj_platform_logic` package changes, THE hellodj-pipeline SHALL rebuild the per-component images that embed that package.

### Requirement 7: Bootstrap ordering

**User Story:** As a platform owner, I want a defined bootstrap order for creating and cutting over to `hellodj-cdk`, so that the repository exists and is seeded before the pipeline is repointed at it.

#### Acceptance Criteria

1. WHEN the migration is performed, THE bootstrap procedure SHALL declare the `hellodj-cdk` repository from the current in-repo CDK location before seeding it.
2. WHEN the `hellodj-cdk` repository has been declared, THE bootstrap procedure SHALL seed the repository history via migrate_repos.py before cutting the pipeline over to it.
3. WHEN the repository has been seeded, THE bootstrap procedure SHALL cut the pipeline over to `hellodj-cdk` and, because selfMutation is disabled, SHALL apply the pipeline definition via a manual `cdk deploy hellodj-pipeline` from the `hellodj-cdk` package before removing the moved files from the `hellodj` repository.
4. IF a pipeline cutover is attempted before the `hellodj-cdk` repository exists or is seeded, THEN THE bootstrap procedure SHALL treat the ordering as violated and SHALL require declaring and seeding the repository before proceeding.

### Requirement 8: Synth-as-parse and pipeline-definition deployment reconciliation

**User Story:** As a platform owner, I want the pipeline to auto-synth on a `hellodj-cdk` push while pipeline-definition changes remain manual, so that the "parsed as it synths" behavior works without re-enabling the risky self-mutation path.

#### Acceptance Criteria

1. WHEN a change is pushed to the `hellodj-cdk` repository, THE hellodj-pipeline SHALL automatically run `cdk synth`, producing the cloud assembly at `infra/cdk.out` and running the repo-wide gates.
2. THE hellodj-pipeline SHALL keep `selfMutation` disabled.
3. WHILE `selfMutation` is disabled, THE hellodj-pipeline SHALL require a manual `cdk deploy hellodj-pipeline` from the `infra/` directory of the `hellodj-cdk` package for changes to the pipeline definition to take effect.

### Requirement 9: Steering documentation synchronization

**User Story:** As a platform owner, I want the steering documents updated in the same change as the repository layout and deploy-workflow change, so that future sessions have accurate repository layout and deploy paths.

#### Acceptance Criteria

1. WHEN the repository layout and deploy-workflow change is made, THE change SHALL update `session-context.md` to reflect the `hellodj-cdk` primary synth source, the `cd infra` deploy path, and the six-repository source count.
2. WHEN the repository layout and deploy-workflow change is made, THE change SHALL update `hellodj-architecture.md` to add a `hellodj-cdk` repository row and to record that the CDK, gates, and `hellodj_platform_logic` now live in `hellodj-cdk` while `platform/components/*` stay in `hellodj`.
3. WHEN the repository layout and deploy-workflow change is made, THE change SHALL update `website-debug-context.md` to reflect the new repository, deploy paths, and pipeline source description.
