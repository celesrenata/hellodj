# Requirements Document

## Introduction

This feature moves the HelloDJ source of truth off public GitHub into a private, AWS-native git
host, adds a local build cache tier in front of the existing S3 binary cache so unchanged Nix
derivations are never rebuilt or refetched, bumps outdated dependencies (raising any component still
on Python 3.11 to Python 3.14 without relying on the deadsnakes PPA in production images), and adds
an optional alarm-email subject rewriter so operational emails literally begin with `HelloDJ:`.

It builds on two completed specs in the same repository and must reconcile with them:

- `aws-saas-replatform` (implemented under `platform/infra`): the CDK Pipelines infrastructure,
  observability stack (CloudWatch alarms, SNS notifications), analytics stack (Glue crawler), and
  the workloads stack.
- `hellodj-nix-native-delivery` (implemented under `platform/`): the four JVM fork flakes
  (`Lavalink`, `lavaplayer`, `LavaSrc`, `youtube-source`), the HelloDJ app repository, the
  declarative pin manifest `pins.toml`, the pin gate (`tools/gate_pins.py` +
  `hellodj_platform_logic.pinning.verify_pin`), the S3-backed Nix binary cache (its Requirement 7),
  and the GitHub Actions Nix build workflow (`.github/workflows/nix-build.yml`).

The central change to the existing design is the **private source relocation**. Research completed
this session established the facts this spec is built on, replacing assumptions:

- **AWS CodeCommit returned to general availability on 2025-11-24.** CodeCommit was closed to new
  customers on 2024-07-25, but that closure was reversed and new repositories can be created again.
  CodeCommit is therefore the chosen private, AWS-native git host — a managed service with no server
  to self-administer.
- **Nix can consume CodeCommit.** The generic `git+https://` flake-input fetcher shells out to the
  system `git`, which honours AWS's git credential helper (`git-remote-codecommit` / the AWS
  credential helper) using IAM authentication, so no static credentials are needed. Flake inputs
  therefore move from `github:hellodj/<repo>/<branch>` to
  `git+https://git-codecommit.<region>.amazonaws.com/v1/repos/<repo>?ref=<branch>`, authenticated by
  the IAM roles the builders already assume via OIDC.
- **This conflicts with `hellodj-nix-native-delivery` Requirement 11.3**, which mandated
  `github:owner/repo/branch` inputs only and had the pin gate reject non-github inputs. The pin gate
  (`gate_pins.py` / `verify_pin` / the `pins.toml` schema) must be amended to accept and resolve
  `git+https` CodeCommit inputs while still rejecting `path:` inputs and still verifying that the
  pinned identifier equals the upstream identifier at pin time.

This is a delivery-and-build and observability spec. It changes where source lives, how builds
cache, which language and dependency versions components target, and how alarm emails are formatted.
It does not change the runtime behaviour of the bot, Activity, or voice features.

Four items completed directly earlier this session are captured only as regression-guard context
(see the Regression-Guard Context requirement), not as new implementation work: the Glue crawler
schedule change (hourly to daily), the SNS email and SMS subscriptions, the `HelloDJ:` alarm-name
prefix, and the per-stage debug logging. All CDK tests currently pass (115 of 115), TypeScript
compilation is clean, and CDK synth is clean.

## Glossary

- **HelloDJ_Platform**: The complete HelloDJ system defined under `platform/`, comprising the JVM
  forks, the Python components, the CDK infrastructure, and the Nix build path.
- **Private_Source_Host**: Amazon CodeCommit, the private AWS-native managed git host that becomes
  the source of truth for the HelloDJ application repository and the four JVM fork repositories,
  returned to general availability on 2025-11-24.
- **Source_Repo**: One of the five repositories relocated into the Private_Source_Host — the HelloDJ
  application repository plus the four JVM fork repositories (`Lavalink`, `lavaplayer`, `LavaSrc`,
  `youtube-source`).
- **CodeCommit_Input**: A Nix flake input that references a Source_Repo in the form
  `git+https://git-codecommit.<region>.amazonaws.com/v1/repos/<repo>?ref=<branch>`, fetched by the
  generic `git+https` fetcher through the system `git` using IAM authentication.
- **Upstream_Remote**: A git remote named `upstream` on each JVM fork Source_Repo pointing at the
  original public upstream project, preserved so future upstream syncs remain possible.
- **Git_Credential_Helper**: The AWS git credential helper (`git-remote-codecommit` or the AWS
  CodeCommit credential helper) configured on each builder so `git` authenticates to CodeCommit
  using the builder's IAM role, with no static credentials stored.
- **Builder**: A machine that runs Nix builds — a GitHub Actions runner or an EKS/Karpenter-hosted
  builder — each of which already assumes an IAM role via OIDC.
- **Pin_Gate**: The pin-time verification workflow (`platform/tools/gate_pins.py`) and its pure
  decision function (`hellodj_platform_logic.pinning.verify_pin`) that verify every flake input pin
  in `pins.toml` equals its upstream identifier, reject `path:` inputs, and retain the prior pinned
  revision on rejection or failure.
- **Pin_Manifest**: The declarative `platform/pins.toml` file that enumerates every pinned input the
  Pin_Gate verifies.
- **S3_Binary_Cache**: The existing signed S3-backed Nix binary cache from
  `hellodj-nix-native-delivery` Requirement 7, holding prebuilt closures shared across the Beta,
  Staging, and Production stages.
- **Local_Nix_Cache**: A cache tier local to a Builder — a persistent local `/nix/store` or a local
  substituter that fronts the S3_Binary_Cache — from which the Builder reuses locally present
  closures by store-path hash before contacting the S3_Binary_Cache.
- **Store_Path_Hash**: The Nix store path hash that uniquely identifies a built closure and is the
  reuse key across the Local_Nix_Cache and the S3_Binary_Cache.
- **Python_Component**: A HelloDJ_Platform component whose runtime is CPython, built by a Nix flake
  (for example `web-ui`, `activity-backend`, `voice-pipeline`, `migration`).
- **Python_314**: CPython feature version 3.14, released October 2025, the migration target for any
  Python_Component currently targeting Python 3.11.
- **Deadsnakes**: The `deadsnakes` Ubuntu PPA providing alternative CPython builds, which production
  images MUST NOT depend on because Python_Components are Nix-built.
- **Dependency_Bump**: The action of updating a pinned dependency to a newer verified version through
  the existing `pins.toml` plus `nix flake update` workflow.
- **Observability_Stack**: The CDK observability stack (`platform/infra/lib/observability-stack.ts`)
  defining CloudWatch alarms and SNS notification subscriptions.
- **Alarm_Notification**: A notification sent when a CloudWatch alarm changes state, delivered to the
  configured SNS email and SMS subscriptions.
- **Subject_Rewriter**: An optional AWS Lambda function subscribed between the alarm SNS topic and
  the email delivery so the delivered email subject literally begins with `HelloDJ:`.
- **Alarm_Name_Prefix**: The `HelloDJ:` text already prepended to CloudWatch alarm names in the
  Observability_Stack.

## Requirements

### Requirement 1: Relocate the HelloDJ source of truth into a private AWS-native git host

**User Story:** As the platform maintainer, I want HelloDJ and its four custom forks moved off public
GitHub into private CodeCommit repositories with their upstream remotes preserved, so that the source
of truth is private and AWS-native while future upstream syncs remain possible.

#### Acceptance Criteria

1. THE HelloDJ_Platform SHALL use Amazon CodeCommit as the Private_Source_Host for the source of
   truth of the HelloDJ application repository and the four JVM fork repositories (`Lavalink`,
   `lavaplayer`, `LavaSrc`, `youtube-source`), yielding exactly five private Source_Repos.
2. WHEN a JVM fork Source_Repo is migrated into the Private_Source_Host, THE Source_Repo SHALL retain
   a git remote named `upstream` whose fetch URL equals the public upstream project URL from which
   that fork was derived and from which a `git fetch upstream` succeeds.
3. THE `Lavalink` Source_Repo SHALL retain the branch named `dev` as its designated build branch.
4. WHEN the migration of a Source_Repo into the Private_Source_Host completes, THE migration SHALL
   preserve the full commit history of that Source_Repo such that the tip commit SHA of each migrated
   branch, the complete set of ancestor commit SHAs reachable from each branch tip, and the set of
   branch and tag names all equal those of the pre-migration source.
5. IF a Source_Repo cannot be created in the Private_Source_Host or its `upstream` remote cannot be
   established during migration, THEN THE migration SHALL report an error identifying the affected
   Source_Repo, SHALL leave the already-migrated Source_Repos unchanged, and SHALL NOT leave the
   affected Source_Repo in a partially migrated state on the Private_Source_Host.
6. WHEN the migration of all five Source_Repos completes, THE HelloDJ_Platform build path SHALL
   source every build input for those five repositories only from Source_Repos hosted on the
   Private_Source_Host and SHALL retain no `github:hellodj/<repo>/<branch>` input referencing a
   migrated Source_Repo.
7. WHEN a Source_Repo is verified after migration, THE Private_Source_Host SHALL expose that
   Source_Repo as a private repository that is not readable without an authenticated, authorized IAM
   principal.

### Requirement 2: Consume CodeCommit repositories as Nix flake inputs over IAM authentication

**User Story:** As a build engineer, I want every flake input to reference the private CodeCommit
repositories using IAM authentication, so that builds fetch private source with no static
credentials.

#### Acceptance Criteria

1. THE HelloDJ_Platform flake inputs SHALL reference each Source_Repo as a CodeCommit_Input in the
   form `git+https://git-codecommit.<region>.amazonaws.com/v1/repos/<repo>?ref=<branch>`, replacing
   the prior `github:hellodj/<repo>/<branch>` inputs.
2. THE HelloDJ_Platform SHALL configure the Git_Credential_Helper on every Builder so that `git`
   authenticates to the Private_Source_Host using the Builder's IAM role.
3. WHEN a Builder fetches a CodeCommit_Input during a build, THE Builder SHALL authenticate to the
   Private_Source_Host using the IAM role it assumes via OIDC and SHALL NOT read or transmit any
   static long-lived credential.
4. WHEN a Builder resolves a CodeCommit_Input for the first time, THE Builder SHALL fetch the source
   at the commit that is the tip of the input's `ref` branch on the Private_Source_Host at
   resolution time.
5. IF a Builder cannot authenticate to the Private_Source_Host when fetching a CodeCommit_Input,
   THEN THE build SHALL fail with an error identifying the CodeCommit_Input that could not be
   fetched and indicating that the failure was an authentication failure, and THE build SHALL NOT
   proceed using partial or stale source for that input.
6. IF a CodeCommit_Input references a repository or `ref` branch that does not exist on the
   Private_Source_Host, THEN THE build SHALL fail with an error identifying the CodeCommit_Input and
   distinguishing the missing repository or branch from an authentication failure.

### Requirement 3: Amend the pin gate to accept CodeCommit inputs while still rejecting path inputs

**User Story:** As the platform maintainer, I want the pin gate to accept the new CodeCommit inputs,
still reject path inputs, and still verify pinned versions against upstream, so that the source
relocation does not weaken pin-time verification.

#### Acceptance Criteria

1. THE Pin_Manifest schema SHALL represent a CodeCommit_Input, including its region, repository name,
   and branch, such that the Pin_Gate can resolve it to the form
   `git+https://git-codecommit.<region>.amazonaws.com/v1/repos/<repo>?ref=<branch>`.
2. WHEN the Pin_Gate reads a Pin_Manifest entry that declares a CodeCommit_Input whose region,
   repository name, and branch are all present, THE Pin_Gate SHALL accept that entry as a valid
   input form rather than rejecting it as a non-github input.
3. IF a Pin_Manifest entry declares a `path:` input or a `path:`-style reference in any field, THEN
   THE Pin_Gate SHALL reject that entry, SHALL fail the pin-gate run with a non-success result, and
   SHALL emit a message identifying the offending entry by its Pin_Manifest key.
4. IF a Pin_Manifest entry that declares a CodeCommit_Input is missing its region, its repository
   name, or its branch, THEN THE Pin_Gate SHALL reject that entry, SHALL fail the pin-gate run with
   a non-success result, and SHALL emit a message identifying the offending entry by its
   Pin_Manifest key and the missing field (this missing-field validation applies to CodeCommit_Input
   entries specifically).
5. WHEN the Pin_Gate verifies a CodeCommit_Input whose pinned commit revision equals the commit
   revision resolved from that input's upstream source at pin time, THE Pin_Gate SHALL accept the
   pin.
6. IF the pinned commit revision of a CodeCommit_Input does not equal the commit revision resolved
   from its upstream source at pin time, THEN THE Pin_Gate SHALL reject the pin, SHALL fail the
   pin-gate run with a non-success result, SHALL emit a message identifying the affected input by
   its Pin_Manifest key, and SHALL leave that input's prior pinned revision unchanged.
7. IF the upstream source of a CodeCommit_Input cannot be resolved at pin time, THEN THE Pin_Gate
   SHALL fail the pin for that input, SHALL fail the pin-gate run with a non-success result, SHALL
   emit a message identifying the unresolved input by its Pin_Manifest key, and SHALL leave that
   input's prior pinned revision unchanged.
8. THE Pin_Gate SHALL continue to verify every input enumerated by the Pin_Manifest, including the
   four JVM forks, Temurin (pinned to feature version 25), nixpkgs, nixos-generators, Karpenter, and
   the EKS Kubernetes version.

### Requirement 4: Local Nix build cache tier in front of the S3 binary cache

**User Story:** As the budget owner, I want each Builder to reuse locally present closures before
fetching from S3, so that unchanged derivations are neither rebuilt nor refetched and build time and
transfer cost are reduced.

#### Acceptance Criteria

1. THE HelloDJ_Platform SHALL provide a Local_Nix_Cache on every Builder, implemented as either a
   persistent local `/nix/store` or a local substituter that fronts the S3_Binary_Cache.
2. WHEN a Builder needs a closure whose Store_Path_Hash is already present in its Local_Nix_Cache,
   THE Builder SHALL reuse the local closure and SHALL NOT rebuild it and SHALL NOT fetch it from the
   S3_Binary_Cache.
3. WHEN a Builder needs a closure whose Store_Path_Hash is absent from its Local_Nix_Cache but
   present in the S3_Binary_Cache, THE Builder SHALL fetch the closure from the S3_Binary_Cache and
   SHALL populate its Local_Nix_Cache with the fetched closure.
4. WHEN the inputs of a derivation are unchanged from a prior build, THE Builder SHALL resolve the
   derivation to the same Store_Path_Hash as the prior build and SHALL reuse the cached closure
   without rebuilding it.
5. IF a closure whose Store_Path_Hash is present in the Local_Nix_Cache fails Nix store-path
   integrity verification, THEN THE Builder SHALL treat that closure as absent from the
   Local_Nix_Cache and SHALL resolve the closure from the S3_Binary_Cache tier or rebuild it.
6. THE Local_Nix_Cache SHALL operate as a tier in front of the S3_Binary_Cache and SHALL NOT replace
   the S3_Binary_Cache as the shared build-once source for the Beta, Staging, and Production stages.
7. WHERE a Builder retains its Local_Nix_Cache storage across jobs, WHEN a subsequent job on that
   Builder needs a closure whose Store_Path_Hash is already present in the Local_Nix_Cache, THE
   Builder SHALL reuse the local closure without fetching it from the S3_Binary_Cache.
8. WHERE a Builder does not retain its Local_Nix_Cache storage across jobs, THE HelloDJ_Platform
   SHALL provide a local substituter that fronts the S3_Binary_Cache so that a closure already
   present in the Local_Nix_Cache is reused without fetching it from the S3_Binary_Cache.
9. IF a closure is absent from both the Local_Nix_Cache and the S3_Binary_Cache, THEN THE Builder
   SHALL build the closure, SHALL populate the Local_Nix_Cache, and SHALL push the resulting closure
   to the S3_Binary_Cache consistent with the existing binary-cache publish path.

### Requirement 5: Migrate Python components to Python 3.14 without deadsnakes

**User Story:** As the platform maintainer, I want every Python component that still targets Python
3.11 raised to Python 3.14 with no dependency on the deadsnakes PPA, so that components run on a
current interpreter built by Nix.

#### Acceptance Criteria

1. THE HelloDJ_Platform SHALL produce an enumerated list naming every Python_Component whose current
   runtime targets Python 3.11.
2. WHEN a Python_Component named in the enumerated list is migrated, THE Python_Component's Nix flake
   SHALL build that component against Python_314.
3. BEFORE a Python_Component is marked migrated to Python_314, THE HelloDJ_Platform SHALL verify, for
   every runtime dependency of that component including cryptography, onnxruntime, torch, discord.py,
   wavelink, and flask where present, that the dependency imports without error under Python_314 and
   that the component's existing test suite passes under Python_314.
4. IF a runtime dependency of a Python_Component fails to import under Python_314 or the component's
   test suite does not pass under Python_314, THEN THE HelloDJ_Platform SHALL record the name of the
   specific dependency that blocks the migration and SHALL NOT mark that component migrated.
5. THE HelloDJ_Platform production images SHALL NOT declare, install, or reference the Deadsnakes PPA
   as a source for any Python runtime at any point during the migration, including intermediate
   states, not only in the final migrated state.
6. WHEN a migrated Python_Component image is started and its Python runtime feature version is
   queried at container startup, THE Python_Component SHALL report the feature version 3.14 within 30
   seconds of container startup.

### Requirement 6: Bump outdated dependencies through the existing pin workflow

**User Story:** As the platform maintainer, I want a mechanism to detect and bump stale pinned
dependencies, so that the project stays current without manual version tracking.

#### Acceptance Criteria

1. THE HelloDJ_Platform SHALL provide a mechanism that produces a report enumerating every
   Pin_Manifest entry whose pinned identifier does not equal the current upstream identifier resolved
   for that entry, listing for each such entry its pinned identifier and its current upstream
   identifier.
2. WHEN a Dependency_Bump is applied to a Pin_Manifest entry, THE HelloDJ_Platform SHALL update that
   entry's pinned revision through the existing `pins.toml` plus `nix flake update` workflow.
3. IF the Pin_Manifest update for a Dependency_Bump fails or is interrupted, THEN THE HelloDJ_Platform
   SHALL reject the entire Dependency_Bump and SHALL leave the Pin_Manifest unchanged so it stays
   consistent.
4. WHEN a Dependency_Bump is applied, THE Pin_Gate SHALL verify the bumped pinned identifier equals
   the entry's upstream identifier resolved at pin time before the bump is adopted.
5. IF a Dependency_Bump would set a pinned identifier that does not equal its upstream identifier
   resolved at pin time, THEN THE Pin_Gate SHALL reject the bump, identify the rejected Pin_Manifest
   entry, and retain the prior pinned revision unchanged.
6. THE Temurin pin SHALL remain at feature version 25 after any Dependency_Bump, consistent with the
   Temurin 25 LTS target from `hellodj-nix-native-delivery`.

### Requirement 7: Optional alarm-email subject prefixed with HelloDJ

**User Story:** As the platform owner, I want alarm emails to literally begin with `HelloDJ:` so that
I can filter them into a folder, so that operational email is organized even though CloudWatch
prepends its own text to the subject.

#### Acceptance Criteria

1. WHERE the Subject_Rewriter is enabled, THE Observability_Stack SHALL route each Alarm_Notification
   through a Subject_Rewriter before the notification is delivered to the configured email
   subscription.
2. WHERE the Subject_Rewriter is enabled, WHEN an Alarm_Notification is delivered to the email
   subscription, THE delivered email subject SHALL literally begin with the text `HelloDJ:` as its
   first characters.
3. WHERE the Subject_Rewriter is enabled, THE Subject_Rewriter SHALL include, in the delivered email
   body, the original alarm name and both the previous and the new alarm state from the original
   Alarm_Notification, each reproduced verbatim.
4. WHERE the Subject_Rewriter is disabled, THE Observability_Stack SHALL deliver Alarm_Notifications
   through the existing SNS-to-email path without routing them through the Subject_Rewriter.
5. IF the Subject_Rewriter fails to process an Alarm_Notification, THEN THE Observability_Stack SHALL
   still deliver that Alarm_Notification to the configured email subscription with the original
   notification body preserved, so that no alarm is silently dropped.

### Requirement 8: Regression-Guard Context for changes completed this session

**User Story:** As the platform maintainer, I want the changes already completed this session guarded
against regression, so that later work in this spec does not undo them.

#### Acceptance Criteria

1. THE analytics stack SHALL schedule the Glue crawler to run daily using the schedule expression
   `cron(5 0 * * ? *)` rather than hourly.
2. THE Observability_Stack SHALL retain an SNS email subscription for `celes+hellodj@celestium.life`
   and an SNS SMS subscription for `+14257853431`.
3. THE Observability_Stack SHALL prefix every CloudWatch alarm name with the Alarm_Name_Prefix
   `HelloDJ:`.
4. WHILE a workload runs in the Beta or Staging stage, THE workloads stack SHALL configure that
   workload with `LOG_LEVEL=DEBUG` and `HELLODJ_DEBUG=true`.
5. WHILE a workload runs in the Production stage, THE workloads stack SHALL configure that workload
   with `LOG_LEVEL=INFO` and `HELLODJ_DEBUG=false`.
6. WHEN the CDK test suite is run, THE CDK test suite SHALL pass with zero failing tests, and THE CDK
   application SHALL synthesize successfully.
