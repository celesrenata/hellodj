# Session Context — Pipeline Debugging + Application Ready (2026-08-27)

inclusion: manual

## What was accomplished this session

### Pipeline fully operational (synth + gates passing)

The CDK pipeline (`hellodj-pipeline`) was broken on first run due to multiple
structural issues. All fixed iteratively across ~15 commits:

1. **CodeBuildStep** with `installCommands` (Nix + ruff), `primaryOutputDirectory`
2. **`$CODEBUILD_SRC_DIR` absolute paths** — CodeBuild `/bin/sh` doesn't persist `cd`
3. **Ubuntu 24.04 `standard:8.0`** — Node 22, Python 3.14 pre-installed
4. **MEDIUM compute** (4 vCPU, 7 GB) — fast synth (~1 min total)
5. **All 5 CodeCommit repos as source triggers** (additionalInputs)
6. **Self-mutation disabled** — kubectl cross-stack Lambda issue
7. **Bootstrap mode** for closures (placeholder hashes pass cleanly)
8. **ruff 0.16.4** targeting `py314`
9. **500-line refactoring** — types package, extracted helpers
10. **All missing files committed** — codecommit_input.py, source-stack.ts, etc.

### Pipeline current state

- **Synth**: PASSING ✓
- **All gates**: PASSING ✓ (base-image, style/ruff, line-count, pin verification, closures)
- **Self-mutation**: DISABLED (deploy manually with `cdk deploy hellodj-pipeline`)
- **Stage deploys** (beta → staging → production): In Progress / awaiting first run
- **Build image**: `aws/codebuild/standard:8.0` (Ubuntu 24.04)
- **Compute**: `BUILD_GENERAL1_MEDIUM` (4 vCPU, 7 GB)
- **Source triggers**: All 5 CodeCommit repos

### Carried forward issues

1. **Tidal Token Refresh** — `status=401`, needs credential refresh
2. **Track Transition Crash** — Lavalink OpusEncoder shared downstream filter close
3. **Component Nix packaging** — Most components lack `flake.nix` (GHA skips)
4. **Self-mutation** — Needs kubectl handler fix for cross-stack Lambda invocation

## Key facts for next session

- **Pipeline name**: `hellodj-pipeline`
- **AWS profile**: `hellodj` (account `874927898283`, region `us-east-1`)
- **Deploy command**: `cd platform/infra && npx cdk deploy hellodj-pipeline --profile hellodj --require-approval never`
- **Source of truth**: CodeCommit (5 repos), NOT GitHub
- **On-prem kube**: gremlin nodes (10.1.1.12–15), namespace `hellodj-service`
- **Bot image**: `registry.celestium.life/hellodj/bot:shader-presets-2026-08-24`
- **Lavalink image**: `registry.celestium.life/hellodj/lavalink:audio-pipe-2026-08-23`
- **Tests**: 226 CDK tests passing, all Python gate tools passing
- **Ruff**: 0.16.4, target `py314`
- **CodeBuild image**: `aws/codebuild/standard:8.0`
