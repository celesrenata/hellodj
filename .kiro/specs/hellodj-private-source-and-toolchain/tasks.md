# Implementation Plan: HelloDJ Private Source and Toolchain

## Overview

This plan converts the eight-requirement `hellodj-private-source-and-toolchain` design into an
incremental, test-driven sequence. This spec **amends existing code** from the two implemented
sibling specs (`aws-saas-replatform`, `hellodj-nix-native-delivery`) under `platform/`; it does not
build from scratch. It reuses `hellodj_platform_logic` (`pinning.verify_pin`/Property 13 unchanged,
`migration.migrate_forks`, `binary_cache.resolve_closure`, `types.py`), `platform/tools/gate_pins.py`,
`platform/pins.toml` + `platform/pins.upstream.toml`, the CDK stacks under `platform/infra/lib/`
(`observability-stack.ts`, `analytics-stack.ts`, `workloads-stack.ts`), and the GitHub Actions
workflow `.github/workflows/nix-build.yml`.

The sequencing respects the design's dependency structure:

1. **Pure decision logic + data models first** in `hellodj_platform_logic` — `classify_input`,
   `resolve_codecommit_input`, `migrate_repos` (extends `migrate_forks`), `tiered_cache_lookup`,
   `python_migration_ready`, `stale_pins`, and the `alarm_subject` functions. They are independently
   testable, carry this spec's 7 correctness properties, and unblock the gate/CDK/build wiring.
2. **Pin-gate + `pins.toml` amendment** so the new CodeCommit input form is accepted, `path:` stays
   rejected, and `verify_pin` still runs on every enumerated input.
3. **CDK `SourceStack` (CodeCommit repos) + the transactional migration procedure.**
4. **The `git+https` flake-input switch + credential-helper wiring on both builder classes** (GHA
   runner in `.github/workflows/nix-build.yml`; EKS/Karpenter builder).
5. **Local Nix cache tier wiring** in front of the S3 binary cache.
6. **Python 3.11 → 3.14 component migration + the no-deadsnakes scan.**
7. **Stale-pin bump tooling.**
8. **Optional `Subject_Rewriter` Lambda** in the observability stack.
9. **Regression-guard assertions (R8)** — already implemented this session; confirm/guard via tests.
10. **End-to-end verification / final checkpoint.**

**Language:** Python (pure logic, gate, tooling), Nix (flakes), TypeScript (CDK), plus GitHub Actions
YAML — fixed by the design; no language selection is required (the design uses concrete languages, no
pseudocode).

**Testing stack:** the existing platform PBT stack — **Hypothesis** for the Python pure-logic
properties (the repo already uses Hypothesis; `.hypothesis/` is present). Do not build property
testing from scratch. Each of the 7 correctness properties is one property test, **minimum 100
iterations**, tagged `Feature: hellodj-private-source-and-toolchain, Property {n}: {property_text}`.
Property 1 **reuses/extends** the existing Property 13 test
(`tests/test_pinning_property.py`); the migration property **extends** the existing
`tests/test_migrate_forks_property.py`.

## Tasks

- [x] 1. Add data models for this spec's decisions in `hellodj_platform_logic`
  - Add frozen dataclasses/enums to `platform/components/hellodj_platform_logic/types.py` (or a sibling
    module imported by it): `CodeCommitInput`, `InputForm` (GITHUB/CODECOMMIT/PATH/INVALID),
    `CodeCommitRepo`, `CacheTier` (LOCAL_HIT/S3_HIT/BUILD), `CacheTierResolution`, `DependencyCheck`,
    `PythonComponentMigration`, `StalePin`, `AlarmNotification`, `EmailDelivery`, exactly as the design
    Data Models specify
  - Reuse existing `FlakeInputPin`, `PinVerification`, `ForkMigration`, `ClosureRef`,
    `ClosureResolution` without modification
  - _Requirements: 1.1, 1.2, 1.3, 2.1, 3.1, 4.2, 4.3, 4.9, 5.3, 5.4, 6.1, 7.2, 7.3, 7.5_

  - [x]* 1.1 Write unit tests for the new dataclass/enum invariants
    - In `platform/components/hellodj_platform_logic/tests/test_types.py`, assert `InputForm` and
      `CacheTier` member sets, `CacheTierResolution` field defaults, and frozen/immutable construction
      of the new dataclasses
    - _Requirements: 3.2, 4.2, 7.2_

- [x] 2. Implement input-form classification and CodeCommit input resolution (R3.1–R3.4)
  - [x] 2.1 Implement `classify_input` and `resolve_codecommit_input`
    - Add `classify_input(entry) -> InputForm` and `resolve_codecommit_input(region, repo, branch) -> str`
      to a new module `platform/components/hellodj_platform_logic/codecommit_input.py`
    - `classify_input` returns CODECOMMIT iff `type == "codecommit"` and region/repo/branch are all
      present and non-empty; PATH whenever any field declares a `path:` input or `path:`-style reference
      (contains `:` in a bare field, starts with `path`, or `type == "path"`); INVALID when a codecommit
      entry is missing region/repo/branch (recording the missing field); otherwise GITHUB for a
      well-formed legacy github entry
    - `resolve_codecommit_input` returns
      `git+https://git-codecommit.<region>.amazonaws.com/v1/repos/<repo>?ref=<branch>`
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

  - [x]* 2.2 Write the property test for Property 2 (input-form classification)
    - **Property 2: Input-form classification accepts CodeCommit, rejects path, flags missing fields**
    - **Validates: Requirements 3.2, 3.3, 3.4**
    - In `platform/components/hellodj_platform_logic/tests/test_classify_input_property.py`, Hypothesis
      over generated entries: well-formed codecommit → CODECOMMIT; a `path:` in any field → PATH; a
      codecommit entry with a dropped field → INVALID naming the field; a well-formed github entry →
      GITHUB; min 100 iterations; tag `Feature: hellodj-private-source-and-toolchain, Property 2: ...`

  - [x]* 2.3 Write unit test for `resolve_codecommit_input` shape
    - In `platform/components/hellodj_platform_logic/tests/test_classify_input_property.py`, assert the
      returned string equals `git+https://git-codecommit.<region>.amazonaws.com/v1/repos/<repo>?ref=<branch>`
      for representative inputs
    - _Requirements: 2.1, 3.1_

- [x] 3. Extend the transactional migration decision function to five repos (R1.4/R1.5)
  - [x] 3.1 Implement `migrate_repos` extending `migrate_forks`
    - Add `migrate_repos(repos, attempt) -> list[ForkMigration]` to
      `platform/components/hellodj_platform_logic/migration.py`, reusing the existing `migrate_forks`
      shape (process in order, halt at the first repo whose create/upstream/history-preservation step
      fails, name that repo, mark prior repos migrated-and-unchanged, process no repo after the
      failure); the side-effecting create/push/verify is injected via the `attempt` callback exactly as
      `migrate_forks` injects it, extended to the five `CodeCommitRepo`s and the history-preservation
      assertion
    - _Requirements: 1.4, 1.5_

  - [x]* 3.2 Extend the existing migration property test to five repos
    - **Property (migration): Repo migration halts on first failure and leaves prior repos unchanged**
    - **Validates: Requirements 1.4, 1.5**
    - Extend `platform/components/hellodj_platform_logic/tests/test_migrate_forks_property.py` to cover
      `migrate_repos` over generated five-repo lists + failure index; assert stop-at-first-failure,
      prior repos unchanged, no later repo processed; min 100 iterations; tag
      `Feature: hellodj-private-source-and-toolchain, Property (migration): ...`

- [x] 4. Implement the tiered cache-lookup decision function (R4.2–R4.5, R4.9)
  - [x] 4.1 Implement `tiered_cache_lookup`
    - Add `tiered_cache_lookup(local_present: bool, local_integrity_ok: bool, s3_present: bool) -> CacheTierResolution`
      to `platform/components/hellodj_platform_logic/binary_cache.py` (in front of the existing
      `resolve_closure`), returning LOCAL_HIT when local present and integrity OK (no rebuild, no S3
      fetch); S3_HIT (populate local) when not usable locally but present in S3; BUILD (populate local,
      push S3) when usable at neither tier; a corrupt local closure (integrity fail) is treated as
      absent per the design truth table
    - _Requirements: 4.2, 4.3, 4.4, 4.5, 4.6, 4.9_

  - [x]* 4.2 Write the property test for Property 3 (tiered lookup ordering)
    - **Property 3: Tiered cache lookup reuses locally, else fetches S3, else builds and populates both**
    - **Validates: Requirements 4.2, 4.3, 4.4, 4.6, 4.9**
    - In `platform/components/hellodj_platform_logic/tests/test_tiered_cache_lookup_property.py`,
      Hypothesis over the (local_present, local_integrity_ok, s3_present) truth table; assert LOCAL_HIT
      ⇒ no rebuild/no S3 fetch; S3_HIT ⇒ populated_local; BUILD ⇒ populated_local + pushed_s3; never
      rebuilds a reusable closure; min 100 iterations; tag
      `Feature: hellodj-private-source-and-toolchain, Property 3: ...`

  - [x]* 4.3 Write the property test for Property 4 (integrity fallthrough)
    - **Property 4: A local closure that fails integrity is treated as absent**
    - **Validates: Requirements 4.5**
    - In `platform/components/hellodj_platform_logic/tests/test_tiered_cache_lookup_property.py`,
      Hypothesis: local_present + integrity FAIL never yields LOCAL_HIT; yields S3_HIT when s3 present
      else BUILD; min 100 iterations; tag `Feature: hellodj-private-source-and-toolchain, Property 4: ...`

- [x] 5. Implement the Python migration-readiness decision function (R5.3/R5.4)
  - [x] 5.1 Implement `python_migration_ready`
    - Add `python_migration_ready(checks, test_suite_passed) -> (ready: bool, blocking_dependency: str | None)`
      to a new module `platform/components/hellodj_platform_logic/python_migration.py`; ready iff every
      `DependencyCheck.imports_ok` is true AND `test_suite_passed`; otherwise not ready, naming the first
      failing dependency (or the failed test suite)
    - _Requirements: 5.3, 5.4_

  - [x]* 5.2 Write the property test for Property 5 (migration readiness)
    - **Property 5: A Python component is migration-ready iff every dependency imports and its tests pass**
    - **Validates: Requirements 5.3, 5.4**
    - In `platform/components/hellodj_platform_logic/tests/test_python_migration_property.py`, Hypothesis
      over generated dependency-check maps + test outcome; assert ready iff all import and tests pass;
      else names a blocking dependency and component not marked migrated; min 100 iterations; tag
      `Feature: hellodj-private-source-and-toolchain, Property 5: ...`

- [x] 6. Implement the stale-pin report decision function (R6.1)
  - [x] 6.1 Implement `stale_pins`
    - Add `stale_pins(pins: dict[str, FlakeInputPin], upstream: dict[str, str | None]) -> list[StalePin]`
      to a new module `platform/components/hellodj_platform_logic/stale_pins.py`; report an entry iff its
      resolved upstream identifier is present and differs from the pinned identifier (exactly the set
      `verify_pin` would reject), listing both identifiers; unresolved upstream is excluded (surfaced
      separately as a resolution failure)
    - _Requirements: 6.1_

  - [x]* 6.2 Write the property test for Property 6 (stale-pin report)
    - **Property 6: The stale-pin report lists exactly the pins whose pinned identifier differs from upstream**
    - **Validates: Requirements 6.1**
    - In `platform/components/hellodj_platform_logic/tests/test_stale_pins_property.py`, Hypothesis over
      pins + upstream maps; assert reported iff resolved upstream differs from pinned; both identifiers
      listed; unresolved excluded; min 100 iterations; tag
      `Feature: hellodj-private-source-and-toolchain, Property 6: ...`

- [x] 7. Implement the alarm-subject rewriter pure functions (R7.2/R7.3/R7.5)
  - [x] 7.1 Implement `rewrite_subject`, `rewrite_body`, and `rewriter_outcome`
    - Add a new module `platform/components/hellodj_platform_logic/alarm_subject.py` with
      `rewrite_subject(original_subject) -> str` (result begins with `HelloDJ:`, idempotent — no
      double-prefix), `rewrite_body(alarm_name, previous_state, new_state, original_body) -> str`
      (output contains each input field verbatim), and
      `rewriter_outcome(process_succeeded, notification) -> EmailDelivery` (on success returns the
      rewritten delivery; on failure returns a fail-open delivery of the original notification, never
      dropping)
    - _Requirements: 7.2, 7.3, 7.5_

  - [x]* 7.2 Write the property test for Property 7 (subject prefix, body preservation, fail-open)
    - **Property 7: An enabled subject rewriter prefixes the subject, preserves the body, and never drops on failure**
    - **Validates: Requirements 7.2, 7.3, 7.5**
    - In `platform/components/hellodj_platform_logic/tests/test_alarm_subject_property.py`, Hypothesis
      over arbitrary subjects/alarm names/states + success/failure: subject begins `HelloDJ:` and is
      idempotent; body preserves alarm name + both states verbatim on success; original preserved and
      never dropped on failure; min 100 iterations; tag
      `Feature: hellodj-private-source-and-toolchain, Property 7: ...`

- [x] 8. Extend the existing Property 13 pin-verification test to CodeCommit inputs (R3.5–R3.7, R6.4/R6.5)
  - [x]* 8.1 Extend the Property 13 test to generate CodeCommit inputs
    - **Property 1: Pin verification accepts equal identifiers and otherwise retains the prior pin**
    - **Validates: Requirements 3.5, 3.6, 3.7, 6.4, 6.5**
    - Extend `platform/components/hellodj_platform_logic/tests/test_pinning_property.py` so its
      Hypothesis strategy also generates `CodeCommitInput`-derived pins alongside github inputs, keeping
      `verify_pin` unchanged; assert accept-iff-equal, reject+name on mismatch, fail+name on unresolved,
      prior pin retained in both failure paths; min 100 iterations; tag
      `Feature: hellodj-private-source-and-toolchain, Property 1: ...`

- [x] 9. Checkpoint — pure logic and data models complete
  - Ensure all `hellodj_platform_logic` unit and property tests pass, ask the user if questions arise.

- [ ] 10. Amend the pin gate and `pins.toml` to accept CodeCommit inputs (R3)
  - [x] 10.1 Extend the `pins.toml` schema for the CodeCommit input form
    - In `platform/pins.toml`, add the `type = "codecommit"` discriminator with `region`, `repo`, and
      `branch` fields for the four JVM-fork inputs (and the `hellodj` app input) so each resolves to
      `git+https://git-codecommit.<region>.amazonaws.com/v1/repos/<repo>?ref=<branch>`; keep
      `pinned_identifier` and update `platform/pins.upstream.toml` entries so upstream resolution still
      matches per input
    - _Requirements: 3.1, 3.2_

  - [x] 10.2 Wire `classify_input` + `resolve_codecommit_input` into `gate_pins.py`
    - In `platform/tools/gate_pins.py`, load each entry through `classify_input`: accept CODECOMMIT
      entries as a valid input form (remove the old "reject non-github" behaviour for codecommit
      entries only); reject PATH entries (fail the run, emit a message naming the offending entry by its
      `pins.toml` key); reject INVALID codecommit entries (fail the run, emit a message naming the entry
      and the missing field); resolve codecommit entries via `resolve_codecommit_input`; keep legacy
      github owner/repo/branch validation; then run every entry through the unchanged `verify_pin` for
      upstream verification
    - Keep `REQUIRED_INPUTS` and the Temurin `feature_version == 25` assertion enforced across all
      enumerated inputs (four forks, Temurin, nixpkgs, nixos-generators, Karpenter, EKS version)
    - _Requirements: 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8_

  - [x]* 10.3 Extend the gate unit/example tests for CodeCommit + path + enumeration
    - In `platform/components/hellodj_platform_logic/tests/test_gate_pins.py`, assert: a valid codecommit
      entry is accepted; a `path:` entry is rejected naming the key; a codecommit entry missing a field
      is rejected naming the missing field; `REQUIRED_INPUTS` still enforced; Temurin `feature_version
      == 25` asserted; a bump moving Temurin off 25 rejected by the loader
    - _Requirements: 3.3, 3.4, 3.8, 6.6_

- [ ] 11. Provision CodeCommit repositories in CDK and implement the migration procedure (R1)
  - [x] 11.1 Add the CDK `SourceStack` with five CodeCommit repositories
    - Create `platform/infra/lib/source-stack.ts` declaring five `aws-cdk-lib/aws-codecommit`
      `Repository` constructs named `hellodj`, `Lavalink`, `lavaplayer`, `LavaSrc`, `youtube-source`,
      with a resource policy granting access only to the build IAM roles (GHA-runner role,
      EKS/Karpenter builder role) and no public/anonymous access; designate `dev` as the Lavalink build
      branch; wire the stack into `platform/infra/bin/` app composition
    - _Requirements: 1.1, 1.3, 1.7_

  - [x] 11.2 Implement the transactional migration procedure driven by `migrate_repos`
    - Add `platform/tools/migrate_repos.py` that drives `hellodj_platform_logic.migration.migrate_repos`
      with a real `attempt` callback per repo: create/confirm the CodeCommit repo, add `origin →
      codecommit` and (for the four forks) `upstream → <public upstream>` verifying `git fetch upstream`
      succeeds, `git push --mirror` the full history, and verify post-push that each branch tip SHA, its
      ancestor SHA set, and the branch/tag name set equal the pre-migration source; halt on the first
      repo failure leaving already-migrated repos unchanged and no partial ref set on CodeCommit
    - Upstream URLs: Lavalink → `https://github.com/lavalink-devs/Lavalink` (branch `dev`); lavaplayer
      → `https://github.com/lavalink-devs/lavaplayer` (`main`); LavaSrc → `https://github.com/topi314/LavaSrc`
      (`tidal-v2-api`); youtube-source → `https://github.com/lavalink-devs/youtube-source` (`main`);
      `hellodj` app repo has no upstream (`main`)
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.6_

  - [x]* 11.3 Write CDK-assertion tests for `SourceStack`
    - In `platform/infra/test/`, assert exactly five `AWS::CodeCommit::Repository` resources with the
      expected names; the resource policy grants only the build IAM roles with no public/anonymous
      access; Lavalink designates `dev` as its build branch
    - _Requirements: 1.1, 1.3, 1.7_

  - [x]* 11.4 Write integration tests for upstream remote and history preservation
    - Add integration tests (1–3 examples) asserting each fork's `upstream` remote fetch URL equals the
      public upstream and `git fetch upstream` succeeds; and after the mirror push, each migrated
      branch's tip SHA, ancestor SHA set, and branch/tag name set equal the pre-migration source
    - _Requirements: 1.2, 1.4_

- [ ] 12. Switch flake inputs to CodeCommit and wire the git credential helper on builders (R2)
  - [x] 12.1 Replace `github:hellodj/<repo>/<branch>` inputs with CodeCommit `git+https` inputs
    - In the HelloDJ platform flake(s) and the Lavalink fork flake, replace each migrated repo's
      `github:hellodj/<repo>/<branch>` input with
      `git+https://git-codecommit.<region>.amazonaws.com/v1/repos/<repo>?ref=<branch>`; in the Lavalink
      flake the three sibling forks (`lavaplayer`, `LavaSrc`, `youtube-source`) likewise become
      `git+https` CodeCommit inputs; retain no `github:hellodj/<repo>/<branch>` input for a migrated repo
    - _Requirements: 2.1, 1.6_

  - [x] 12.2 Configure the git credential helper on the GHA runner
    - In `.github/workflows/nix-build.yml`, after the existing OIDC
      `aws-actions/configure-aws-credentials@v4` step (which assumes `AWS_BUILD_ROLE_ARN`), configure
      the AWS git credential helper (`git-remote-codecommit` or `git config --global credential.helper
      '!aws codecommit credential-helper $@'` + `credential.UseHttpPath true`) so `git` authenticates to
      CodeCommit using the assumed IAM role with no static long-lived credential read or transmitted
    - _Requirements: 2.2, 2.3, 2.4_

  - [x] 12.3 Configure the git credential helper on the EKS/Karpenter builder
    - In the EKS/Karpenter builder node/pod image (referenced from `platform/infra/lib/eks-stack.ts` /
      the builder image definition), configure the AWS git credential helper and use an
      IRSA/pod-identity role so `git` authenticates to CodeCommit from the assumed role with no static
      credential; map git/credential-helper exit signatures to the two error classes — HTTP 403 /
      credential denial → authentication failure (naming the input, not proceeding on partial/stale
      source); HTTP 404 / "repository does not exist" / unknown ref → missing repo/branch (naming the
      input, distinguished from auth failure)
    - _Requirements: 2.2, 2.3, 2.5, 2.6_

  - [x]* 12.4 Write the source-ownership static scan
    - In `platform/components/hellodj_platform_logic/tests/test_static_scan_compliance.py`, assert zero
      `github:hellodj/<repo>/<branch>` inputs referencing a migrated repo remain, and all five migrated
      inputs reference the CodeCommit `git+https` form
    - _Requirements: 1.6, 2.1_

  - [ ]* 12.5 Write integration tests for first-fetch and the two error classes
    - Add integration tests (1–3 examples): a first-time `CodeCommit_Input` fetch resolves to the branch
      tip on CodeCommit at resolution time; an induced credential denial fails the build naming the
      input as an authentication failure without proceeding on partial source; a nonexistent repo/branch
      fails naming the input and distinguished from an auth failure
    - _Requirements: 2.4, 2.5, 2.6_

- [x] 13. Checkpoint — source relocated, flake inputs switched, credential helper wired
  - Ensure the pin gate passes over the amended manifest, the flake inputs reference CodeCommit, and the
    migration/credential-helper wiring tests are green, ask the user if questions arise.

- [ ] 14. Wire the local Nix cache tier in front of the S3 binary cache (R4.1, R4.7, R4.8)
  - [x] 14.1 Wire the GHA-runner local cache tier
    - In `.github/workflows/nix-build.yml`, add a persistent-across-jobs local `/nix/store` via
      `actions/cache` keyed by a hash of `flake.lock` + target system (restore at job start, save at job
      end) and/or a local substituter fronting the S3 cache (`--substituters "file:///nix-local-cache
      s3://…"` ordering) so a closure present in the local tier is reused without an S3 fetch; on a
      BUILD, still push to S3 consistent with the existing publish path (`tools/record_closure.py`)
    - _Requirements: 4.1, 4.6, 4.7, 4.8, 4.9_

  - [x] 14.2 Wire the EKS/Karpenter builder local cache tier
    - In `platform/infra/lib/eks-stack.ts` (builder node/pod), provide a node-local persistent `/nix`
      store retained across builder pods on the same node and/or a local pull-through substituter in
      front of S3, ordered so the local tier is consulted before S3; keep S3 as the shared build-once
      source for Beta/Staging/Production (the local tier never becomes the cross-stage source)
    - _Requirements: 4.1, 4.6, 4.7, 4.8_

  - [ ]* 14.3 Write the tiered-cache integration test
    - Add an integration test: on a builder with a populated local store, an unchanged closure is reused
      without an S3 fetch; clearing the local store then fetches from S3 and repopulates locally; the
      BUILD path still pushes to S3
    - _Requirements: 4.2, 4.3, 4.6, 4.7, 4.9_

- [ ] 15. Migrate Python components from 3.11 to 3.14 without deadsnakes (R5)
  - [x] 15.1 Enumerate the Python 3.11 components and switch each flake to `python314`
    - Record the enumerated list of the seven Python components currently on 3.11 (`discord-bot-core`,
      `playback-orchestrator`, `config-renderer`, `activity-backend`, `voice-pipeline`, `web-ui`,
      `migration`) in `platform/NIX-CONVERSION-CONTEXT.md`; change each component flake under
      `platform/components/<component>/` from `pkgs.python311` to `pkgs.python314` and re-resolve
      dependencies; log `sys.version_info[:2]` at each component's entrypoint at startup
    - _Requirements: 5.1, 5.2, 5.6_

  - [x] 15.2 Gate each component migration on `python_migration_ready`
    - For each component, wire a migration-readiness check that feeds per-dependency 3.14 import results
      (cryptography, onnxruntime, torch, discord.py, wavelink, flask, boto3, aiohttp, numpy, gunicorn
      where present) and the component's test-suite outcome into `python_migration_ready`; mark the
      component migrated only when ready, otherwise record the named blocking dependency and leave it
      unmigrated
    - _Requirements: 5.3, 5.4_

  - [x]* 15.3 Write the no-deadsnakes static scan and the 3.11-component-list assertion
    - In `platform/components/hellodj_platform_logic/tests/test_static_scan_compliance.py`, assert zero
      `deadsnakes` references across component flakes and any Dockerfiles (asserted for each migration
      step including intermediate states), and assert the enumerated Python-3.11 component list equals
      the seven named components
    - _Requirements: 5.1, 5.5_

  - [ ]* 15.4 Write the 3.14 build + startup-version integration/smoke test
    - Add an integration test asserting each migrated component flake builds against `python314` and the
      started image reports Python feature version `(3, 14)` within 30 s of startup
    - _Requirements: 5.2, 5.6_

- [ ] 16. Implement the stale-pin report + dependency-bump tooling (R6)
  - [x] 16.1 Add the `report_stale_pins.py` wrapper over `stale_pins`
    - Create `platform/tools/report_stale_pins.py` that resolves current upstream identifiers (reusing
      the same `pins.upstream.toml` resolution the pin gate uses) and calls
      `hellodj_platform_logic.stale_pins.stale_pins`, printing each stale entry's pinned identifier and
      current upstream identifier
    - _Requirements: 6.1_

  - [x] 16.2 Implement the atomic dependency-bump apply path
    - Add a bump-apply path (in `platform/tools/report_stale_pins.py` or a sibling
      `platform/tools/apply_bump.py`) that updates a `pins.toml` entry's pinned revision through the
      existing `pins.toml` + `nix flake update <input>` workflow with an atomic write-to-temp-then-rename
      so an interrupted/failed update leaves `pins.toml` unchanged; then re-run the pin gate so the
      bumped identifier is verified against upstream (via `verify_pin`) before adoption, rejecting the
      bump and retaining the prior revision on mismatch; hold Temurin at feature version 25
    - _Requirements: 6.2, 6.3, 6.4, 6.5, 6.6_

  - [x]* 16.3 Write unit tests for the bump workflow and atomic write
    - Assert applying a bump rewrites the entry via the `pins.toml` + `nix flake update` path; an
      interrupted write (atomic temp+rename) leaves `pins.toml` unchanged; a bump to a non-upstream
      identifier is rejected retaining the prior revision; a bump moving Temurin off 25 is rejected
    - _Requirements: 6.2, 6.3, 6.5, 6.6_

- [ ] 17. Add the optional `Subject_Rewriter` Lambda to the observability stack (R7)
  - [x] 17.1 Implement the Lambda handler as a thin wrapper over the pure `alarm_subject` functions
    - Add a Lambda handler (Python) under `platform/components/` (or `platform/infra/assets/`) that
      parses the SNS alarm message into an `AlarmNotification`, calls `rewrite_subject` / `rewrite_body`
      / `rewriter_outcome`, and delivers the email via SES `SendEmail` (whose subject is fully
      controllable); catch all processing errors and fall back to delivering the original notification
      (fail-open, never drop)
    - _Requirements: 7.2, 7.3, 7.5_

  - [x] 17.2 Wire the optional Lambda into `observability-stack.ts` behind a toggle
    - In `platform/infra/lib/observability-stack.ts`, add a `subjectRewriterEnabled` stack prop; when
      enabled, subscribe the Lambda on the email side of the existing alarm SNS topic and gate/replace
      the direct email subscription so alarm emails route through the Lambda; when disabled, deliver via
      the existing SNS-to-email subscription with no Lambda in the path; leave the SMS subscription and
      the `HelloDJ:` alarm-name prefix untouched in both modes; retain a direct-email failure sink so a
      Lambda invocation failure still reaches email
    - _Requirements: 7.1, 7.4, 7.5_

  - [x]* 17.3 Write CDK-assertion tests for the rewriter wiring (enabled/disabled)
    - In `platform/infra/test/`, assert that when enabled the Lambda is the email-side subscriber and the
      direct email subscription is replaced/gated; when disabled the existing email subscription delivers
      directly with no Lambda in the path; the SMS subscription is present in both modes
    - _Requirements: 7.1, 7.4_

  - [ ]* 17.4 Write the fail-open integration test
    - Add an integration test: with the rewriter enabled but forced to error, the alarm email is still
      delivered with the original body preserved
    - _Requirements: 7.5_

- [ ] 18. Confirm and guard the regression items completed this session (R8)
  - [x]* 18.1 Confirm/guard the daily Glue crawler schedule
    - In `platform/infra/test/`, assert `analytics-stack.ts` schedules the crawler with
      `scheduleExpression == 'cron(5 0 * * ? *)'` (already implemented — this task adds/confirms the
      guard only)
    - _Requirements: 8.1_

  - [x]* 18.2 Confirm/guard the SNS email + SMS subscriptions
    - In `platform/infra/test/`, assert `observability-stack.ts` retains an email subscription for
      `celes+hellodj@celestium.life` and an SMS subscription for `+14257853431` (already implemented —
      guard, and confirm the §17 rewriter work preserves both)
    - _Requirements: 8.2_

  - [x]* 18.3 Confirm/guard the `HelloDJ:` alarm-name prefix
    - In `platform/infra/test/`, assert every CloudWatch alarm name in `observability-stack.ts` begins
      with the `HelloDJ:` prefix (already implemented — guard only)
    - _Requirements: 8.3_

  - [x]* 18.4 Confirm/guard the per-stage debug logging
    - In `platform/infra/test/`, assert `workloads-stack.ts` sets `LOG_LEVEL=DEBUG` + `HELLODJ_DEBUG=true`
      for Beta/Staging workloads and `LOG_LEVEL=INFO` + `HELLODJ_DEBUG=false` for Production (already
      implemented — guard only)
    - _Requirements: 8.4, 8.5_

- [ ] 19. Final checkpoint — end-to-end verification
  - [x]* 19.1 Run the full gate + suite verification
    - Confirm `python3 tools/gate_pins.py` passes over the amended manifest (github + CodeCommit inputs)
      and `python3 tools/report_stale_pins.py` produces a stale-pin report; confirm the CDK jest suite
      passes with zero failing tests and `cdk synth` completes cleanly (including the new `SourceStack`
      and rewriter wiring and the R8 regression guards)
    - _Requirements: 3.8, 6.1, 8.6_
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- This spec **amends** existing sibling-spec code; reused artifacts (`verify_pin`/Property 13,
  `migrate_forks`, `resolve_closure`, `types.py`, `gate_pins.py`, `pins.toml`, the CDK stacks, and
  `.github/workflows/nix-build.yml`) are extended, not rewritten.
- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP; core
  implementation tasks are never optional.
- The 7 correctness properties map to dedicated Hypothesis property tests: Property 1 (reuse/extend the
  existing Property 13 test), Property 2 (`classify_input`), Property 3 + Property 4
  (`tiered_cache_lookup`), Property 5 (`python_migration_ready`), Property 6 (`stale_pins`), Property 7
  (`alarm_subject`) — each ≥100 iterations, tagged
  `Feature: hellodj-private-source-and-toolchain, Property {n}: {property_text}`.
- All pure decision logic lives once in `hellodj_platform_logic/` and is imported by the gate, CDK
  layer, tooling, and the Lambda, giving a single source of truth.
- CodeCommit hosting, credential-helper wiring, Nix builds, CDK stack config, and IO-layer error
  mapping are covered by CDK-assertion, integration, and smoke tests, not PBT (no meaningful for-all
  statement).
- Regression items (R8) were implemented earlier this session; their tasks confirm/guard via test
  rather than implement.
- Checkpoints (tasks 9, 13, 19) provide incremental validation points.
- Every task is coding-only: no manual AWS console steps, no user acceptance testing, no production
  deployment actions.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1"] },
    { "id": 1, "tasks": ["1.1", "2.1", "3.1", "4.1", "5.1", "6.1", "7.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.2", "4.2", "4.3", "5.2", "6.2", "7.2", "8.1"] },
    { "id": 3, "tasks": ["10.1", "11.1", "15.1", "16.1", "17.1"] },
    { "id": 4, "tasks": ["10.2", "11.2", "14.1", "14.2", "15.2", "16.2", "17.2"] },
    { "id": 5, "tasks": ["10.3", "11.3", "11.4", "12.1", "12.2", "12.3"] },
    { "id": 6, "tasks": ["12.4", "12.5", "14.3", "15.3", "15.4", "16.3", "17.3", "17.4"] },
    { "id": 7, "tasks": ["18.1", "18.2", "18.3", "18.4"] },
    { "id": 8, "tasks": ["19.1"] }
  ]
}
```
