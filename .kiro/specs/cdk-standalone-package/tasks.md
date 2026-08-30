# Implementation Plan: cdk-standalone-package

## Overview

This plan migrates the HelloDJ CDK app and everything `cdk synth` + the repo-wide gates depend on out of the `hellodj` monorepo (`platform/infra`, `platform/tools`, `platform/secrets`, the closure/pin manifests, `pyproject.toml`, and `hellodj_platform_logic`) into a new standalone package + CodeCommit repo `hellodj-cdk` at `/home/celes/sources/celesrenata/hellodj-cdk`. After the split, the pipeline synths from `hellodj-cdk` and builds the 12 components from `hellodj` as an additional input.

Languages are already fixed by the existing codebase: **TypeScript** for the CDK app (`infra/`) and **Python** for the gates and `hellodj_platform_logic` (`tools/`, `shared/`). No language selection is needed.

This is an infrastructure repo-split migration, so the tasks follow the design's hard bootstrap chain (Requirement 7): **(a)** prepare the working tree locally → **(b)** declare the repo from the current in-repo CDK → **(c)** seed history → **(d)** rewire the pipeline → **(e)** cut over → **(f)** verify green, THEN remove moved files from `hellodj` (destructive, last) → **(g)** sync steering docs. Most of the chain is sequential by nature; the few things that parallelize (writing template-assertion tests, drafting steering docs) are called out in the Task Dependency Graph.

> **Real AWS / infrastructure actions — surface for confirmation, do not run blind.**
> Several tasks perform real, hard-to-reverse actions against live AWS and the bot repo:
> - `cdk deploy hellodj-source` (creates the `hellodj-cdk` CodeCommit repo)
> - `migrate_repos.py` real run (`git push --mirror` seeds history)
> - `cdk deploy hellodj-pipeline` + starting a fresh pipeline execution (repoints the live pipeline)
> - removing the moved files from `hellodj` (destructive; the LAST step, gated on pipeline-green verification)
>
> The executor MUST surface each of these to the user for explicit confirmation before running, per the safety guardrails. Dry-run first where a dry-run exists (`migrate_repos.py --dry-run`).

## Tasks

- [x] 1. Prepare the `hellodj-cdk` working tree with the moved layout (bootstrap step a)
  - [x] 1.1 Create the `hellodj-cdk` package skeleton and copy the moved files
    - Create `/home/celes/sources/celesrenata/hellodj-cdk/` with `infra/` (from `platform/infra/`), `tools/` (from `platform/tools/`), `secrets/` (from `platform/secrets/`), and `shared/hellodj_platform_logic/` (from `platform/components/hellodj_platform_logic/`)
    - Copy `closures.toml`, `pins.toml`, `pins.upstream.toml`, `pyproject.toml` to the repo root
    - Add a `README.md` describing the standalone package
    - Do NOT delete anything from `hellodj` yet (copy, not move) — deletion is the final gated step
    - _Requirements: 5.1, 5.3_

  - [x] 1.2 Apply low-level path edits L4 for the synth commands in `infra/lib/pipeline-stack.ts`
    - Rewrite synth working dirs: `$CODEBUILD_SRC_DIR/platform/infra` → `$CODEBUILD_SRC_DIR/infra`; gate/tooling `cd $CODEBUILD_SRC_DIR/platform` → `cd $CODEBUILD_SRC_DIR`
    - Rewrite the sops decrypt path `platform/secrets/nix-cache-key.sec.enc` → `secrets/nix-cache-key.sec.enc` for the synth input
    - Ensure no synth command references `platform/infra` or `platform/tools`
    - _Requirements: 2.2, 5.1_

  - [x] 1.3 Adjust Python import/config paths in `tools/` and `pyproject.toml` for the new layout
    - Point `hellodj_platform_logic` imports in the gates (`gate_*.py`, `resolve_closure.py`, `migrate_repos.py`, `_migration_helpers.py`) at the relocated `shared/hellodj_platform_logic/`
    - Update `pyproject.toml` ruff/pytest paths (source roots, testpaths) to the `hellodj-cdk` layout
    - _Requirements: 5.1, 5.3, 6.1_

  - [x] 1.4 Validate the moved tree locally (TS + Python gates)
    - Run `cd infra && npx tsc --noEmit && npx jest` (the 226 CDK tests) against the new paths
    - Run ruff + pytest for the relocated Python tooling under the moved `pyproject.toml`
    - This is a local, non-AWS validation — fix path breakage until both suites pass
    - _Requirements: 4.1_
    - **Validates: Property 7 (Tests preserved)**

- [x] 2. Rewire `SourceStack` and `PipelineStack` in the `hellodj-cdk` package
  - [x] 2.1 Extend `SOURCE_REPOS` with the `hellodj-cdk` entry (edit L1)
    - In `infra/lib/source-stack.ts` add `{ name: 'hellodj-cdk', buildBranch: 'main' }` (no `upstreamUrl`) so `SOURCE_REPOS` has exactly six entries; keep the other five branches unchanged
    - The existing loop provisions the repo, grants `repo.grantPull(role)`, and emits clone-URL + build-branch outputs
    - _Requirements: 1.1, 1.2, 1.3_

  - [x] 2.2 Write template-assertion test for the six-repo SourceStack
    - **Property 1: hellodj-cdk is always declared** — assert `hellodj-cdk` ∈ declared CodeCommit repo names
    - **Property 2: Six repos, correct branches** — assert exactly 6 repos; `hellodj-cdk` branch `main`, no upstream; other five branches retained
    - Add assertions that pull grants and clone-URL/build-branch outputs exist for `hellodj-cdk`
    - **Validates: Requirements 1.1, 1.2, 1.3**

  - [x] 2.3 Repoint the synth primary source to `hellodj-cdk` (edits L2, L5)
    - In `infra/lib/pipeline-stack.ts` default `props.repoString ?? 'hellodj-cdk'` for the synth `SourceRepo`
    - Ensure `bin/hellodj.ts` passes/defaults `repoString: 'hellodj-cdk'` for the pipeline stack
    - Set the synth step `primaryOutputDirectory` to `infra/cdk.out`
    - _Requirements: 2.1, 2.5_

  - [x] 2.4 Write template-assertion tests for synth source, output dir, and synth paths
    - **Property 3: Synth source invariant** — with no `repoString`, the synth primary input resolves to `hellodj-cdk`
    - **Property 5: Synth path invariants** — every synth command cwd resolves under `infra/` (CDK) or repo root (tools); no command references `platform/infra` or `platform/tools`
    - Assert `primaryOutputDirectory === 'infra/cdk.out'`
    - **Validates: Requirements 2.1, 2.2, 2.5**

  - [x] 2.5 Add `hellodj` (+ `hellodj-cdk`) as additional inputs to the per-component builds and add the GitPull ARN (edits L3, L6, L7)
    - Add `hellodj` as an additional source input to the 12 per-component build steps; add `hellodj-cdk` as an additional input for `shared/hellodj_platform_logic`
    - In `getComponentBuildCommands`, rewrite the component `cd` target to the `hellodj` input mount (`platform/components/<component>`) and the vendor copy source to the `hellodj-cdk` input `shared/hellodj_platform_logic`
    - Add `arn:aws:codecommit:${region}:${account}:hellodj-cdk` to the `codecommit:GitPull` policy resource list; retain the other five ARNs
    - _Requirements: 2.3, 2.4, 6.2_

  - [x] 2.6 Write template-assertion tests for GitPull ARN coverage and component inputs
    - **Property 6: GitPull ARN coverage** — assert the policy resource list includes the `hellodj-cdk` ARN and retains the other five
    - Assert `hellodj` is wired as an additional input to the per-component build steps (Req 2.4) and the vendor path sources from the `hellodj-cdk` `shared/` mount (Req 6.2)
    - **Validates: Requirements 2.3, 2.4, 6.2**

  - [x] 2.7 Write template-assertion test that selfMutation stays disabled
    - Assert the synthesized pipeline keeps `selfMutation: false`
    - _Requirements: 8.2_

- [x] 3. CDK-only change isolation test
  - [x] 3.1 Write the CDK-only-change path-partition test
    - **Property 4: CDK-only change isolation** — for a change set confined to the CDK app, all changed paths live under `hellodj-cdk` and the intersection with `hellodj`'s `platform/components/*` is empty
    - Implement as a path-partition unit test over the two repos' file-set boundaries
    - **Validates: Requirements 3.1**

- [x] 4. Harden `migrate_repos.py` for the `hellodj-cdk` entry
  - [x] 4.1 Add the `hellodj-cdk` entry to `REPOS` and resolve its local path (edit L8)
    - In `tools/migrate_repos.py` add `CodeCommitRepo(name="hellodj-cdk", build_branch="main", upstream_url=None)`
    - Ensure `locate_local_repo("hellodj-cdk")` resolves to `/home/celes/sources/celesrenata/hellodj-cdk`
    - Preserve the transactional halt-on-first-failure semantics and history verification
    - _Requirements: 4.2, 7.2_

  - [x] 4.2 Write Hypothesis property test for migrate_repos transactionality
    - **Property 8: Migration idempotence/transactionality** — over arbitrary repo orderings/states, adding `hellodj-cdk` preserves halt-on-first-failure; a re-run against an already-created repo reports `created` without error
    - Use Hypothesis (already in the repo per `.hypothesis/`)
    - **Validates: Requirements 4.2**

- [x] 5. Checkpoint — local gates green before touching AWS
  - Ensure `cd infra && npx tsc --noEmit && npx jest` (226 tests + new assertions) pass, and ruff + pytest (incl. the Hypothesis test) pass. Ask the user if questions arise.
  - This checkpoint is the gate before any real AWS action begins.

- [x] 6. Declare the `hellodj-cdk` CodeCommit repo from the current in-repo CDK (bootstrap step b — REAL AWS)
  - Deploy `hellodj-source` from the CURRENT in-repo location (`cd platform/infra && npx cdk deploy hellodj-source`) with the L1 `SOURCE_REPOS` edit applied there, creating the empty `hellodj-cdk` CodeCommit repo
  - **REAL AWS action — surface for user confirmation before running.** Do not proceed to seeding until the repo exists.
  - _Requirements: 7.1_

- [x] 7. Seed `hellodj-cdk` history (bootstrap step c — REAL AWS, dry-run first)
  - Run `python tools/migrate_repos.py --dry-run` and confirm the `hellodj-cdk` entry appears with the correct target URL and no upstream
  - Then run the real `migrate_repos.py` to `git push --mirror` the new package into `hellodj-cdk` and verify history preservation
  - **REAL AWS action (`git push --mirror`) — surface for user confirmation after the dry-run looks correct.**
  - _Requirements: 7.2_

- [x] 8. Cut the pipeline over to `hellodj-cdk` (bootstrap step e — REAL AWS)
  - From the `hellodj-cdk` package, run `cd infra && npx cdk deploy hellodj-pipeline` (required because `selfMutation` is disabled — the buildspec is frozen at deploy time), then start a fresh pipeline execution
  - **REAL AWS action — surface for user confirmation.** This repoints the live pipeline; do not remove any `hellodj` files yet.
  - _Requirements: 7.3, 8.1, 8.2, 8.3_

- [x] 9. Verify green from `hellodj-cdk` (bootstrap step f — verification, must pass before cleanup)
  - Push a no-op CDK change to `hellodj-cdk` and confirm the pipeline synths WITHOUT any `hellodj` commit (Req 3.2)
  - Push a component change to `hellodj` and confirm the affected component rebuilds (Req 6.3)
  - Do NOT proceed to removal until both confirmations are green
  - _Requirements: 3.2, 6.3, 8.1_

- [x] 10. Remove the moved files from `hellodj` (bootstrap step f final — DESTRUCTIVE, LAST, gated)
  - Delete `platform/infra/`, `platform/tools/`, `platform/secrets/`, `platform/closures.toml`, `platform/pins.toml`, `platform/pins.upstream.toml`, `platform/pyproject.toml`, and `platform/components/hellodj_platform_logic/` from `hellodj` now that they live in `hellodj-cdk`
  - Confirm `hellodj` retains `platform/components/*` (the 12 workloads), `bot/`, and `kube/`
  - **DESTRUCTIVE and depends on task 9 being green — surface for user confirmation. This is the final, hardest-to-reverse step.**
  - _Requirements: 5.2, 6.1, 7.3_

- [x] 11. Synchronize steering documentation (bootstrap step g — same change)
  - [x] 11.1 Update `session-context.md`
    - Reflect the `hellodj-cdk` primary synth source, the `cd infra` deploy path (was `cd platform/infra`), and the six-repository source count in the Pipeline Architecture, Key Commands, and selfMutation gotcha sections
    - _Requirements: 9.1_

  - [x] 11.2 Update `hellodj-architecture.md`
    - Add a `hellodj-cdk` repository row (`codecommit::us-east-1://hellodj-cdk`, branch `main`) and record that the CDK, gates, and `hellodj_platform_logic` now live in `hellodj-cdk` while `platform/components/*` stay in `hellodj`
    - _Requirements: 9.2_

  - [x] 11.3 Update `website-debug-context.md`
    - Update the CRITICAL WORKFLOW RULES (self-mutation, `cd platform/infra` → `cd infra` deploy commands) and the pipeline source description to the new repo/paths
    - _Requirements: 9.3_

- [x] 12. Final checkpoint — ensure everything is consistent
  - Confirm the `hellodj-cdk` suites are green, the pipeline is verified from `hellodj-cdk`, the `hellodj` cleanup is complete, and all three steering docs reflect reality. Ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster path, but they are the automated coverage for the 8 Correctness Properties.
- The strict a→g bootstrap chain is a hard dependency: local prep (1) → rewiring (2–4) → local checkpoint (5) → declare repo (6) → seed (7) → cut over (8) → verify (9) → **destructive cleanup (10, last)** → doc sync (11).
- Property → test mapping: P1/P2 → 2.2, P3/P5 → 2.4, P4 → 3.1, P6 → 2.6, P7 → 1.4, P8 → 4.2.
- Real AWS / destructive actions (tasks 6, 7, 8, 10) must be surfaced to the user for confirmation before execution; use dry-runs where available.
- Component sources and the JVM forks stay in `hellodj`; only synth/gate dependencies and the shared logic move to `hellodj-cdk`.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3"] },
    { "id": 2, "tasks": ["1.4", "2.1", "4.1"] },
    { "id": 3, "tasks": ["2.3", "2.5", "2.2", "3.1", "4.2"] },
    { "id": 4, "tasks": ["2.4", "2.6", "2.7"] },
    { "id": 5, "tasks": ["11.1", "11.2", "11.3"] }
  ]
}
```

> Note on the graph: waves 0–4 cover the code-and-test work that a coding agent can parallelize where files don't conflict — e.g. the template-assertion tests (2.2/2.4/2.6/2.7), the path-partition test (3.1), and the Hypothesis test (4.2) can be written alongside the source edits and the local working-tree prep. The steering-doc updates (11.x) are independent files and parallelize in the final wave. The real-AWS bootstrap actions (tasks 6–10) are intentionally **not** in the parallel graph: they are a strictly sequential, human-gated chain (declare → seed → cut over → verify → destructive cleanup) and each requires user confirmation, so they are executed in order outside the parallel scheduler.
