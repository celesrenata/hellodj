# Website Debugging Context — beta.us-east-1.hellodj.bot (2026-08-28)

inclusion: manual

This captures the full state of the HelloDJ web-ui + AWS platform after a long
debugging/build session, so a new chat can continue debugging the whole website
without re-discovering everything.

## The Site

- **URL**: https://beta.us-east-1.hellodj.bot/  (beta stage)
- **Stages**: `beta`, `staging`, `production` — hostnames `<stage>.us-east-1.hellodj.bot`
- **Path**: CloudFront → ALB → EKS web-ui (Flask/gunicorn) pods
- **Login**: works. Admin account `admin` / `Wkh3llodjbeta!` (Cognito, `admins` group)

## Invitation email / Amazon SES (fixed 2026-08-28)

The admin-panel invite flow sends a branded single-use registration link via
Amazon SES (`invite_email.py` → SES `send_email`), sender
`invites@<stage>.<region>.hellodj.bot` (`INVITE_SENDER` env). On failure the
web-ui catches the `MessageRejected`, rolls back the pending invite, and shows a
generic "could not send" — the SES detail is intentionally suppressed (R7.4), so
**pod logs stay clean on send failure**. Don't expect a stack trace.

**The bug:** CDK provisioned the sender as an SES **email-address identity**
(`ses.Identity.email(...)`), which sits `VerificationStatus: Pending` forever
because it needs a human to click a link mailed to `invites@...` (no mailbox).
Every send was rejected.

**The fix (in `platform/infra/lib/workloads-stack.ts`):** switched to a
**domain identity** for the stage host `<stage>.<region>.hellodj.bot` via
`ses.Identity.domain(...)`, and publish the three Easy-DKIM CNAME tokens
(`identity.dkimRecords`) into the delegated `hellodj.bot` Route 53 zone
(`HostedZone.fromLookup`). SES self-verifies once the CNAMEs resolve — no manual
click. A domain identity authorizes any mailbox on the domain (so `invites@`
works). The web-ui role's `ses:SendEmail`/`SendRawEmail` grant is scoped to the
domain identity ARN (`identity/<stage>.<region>.hellodj.bot`) with an
`ses:FromAddress` condition pinning the From to `invites@<domain>`. Deploy with
`cdk deploy hellodj-eks` (the WorkloadsStack manifests attach to that stack).

**Still manual (no CloudFormation resource exists):** the account is in the SES
**sandbox** (`ProductionAccessEnabled: false`), so it can only send to *verified*
recipients even after the sender is verified. To email arbitrary invitees,
request production access once out-of-band:
`aws sesv2 put-account-details --production-access-enabled --mail-type TRANSACTIONAL ...`.

Facts: SES account region us-east-1, `SendingEnabled: true`, quota 200/day @
1 msg/s (sandbox default). Verified identities: `aws ses list-identities`.

### First-party auth forms (custom-auth-forms spec, 2026-08-28)

Admin login, self-registration, and account recovery are now HelloDJ-branded
**first-party Flask forms**, not the Cognito hosted UI. Cognito is still the
identity provider (the `auth_routing.py` invariant is unchanged — this is a
presentation change to the Cognito-routed purposes only). Key pieces in
`platform/components/web-ui/`:

### Invited user could not log in with their chosen name (fixed 2026-08-28)

Reported bug: a new (invited) user `celes` could not log in. Root cause (facts):
the invite flow (`invite_service.register` → `invite_registration.create_confirmed_account`)
created the Cognito account with an **opaque UUID `Username`** and stored the
name the invitee picked as the `preferred_username` attribute. The pool has
`AliasAttributes: ["email"]`, so `preferred_username` is NOT a sign-in alias —
the ONLY usable login identifier was the email. But the login form prompts
"Username or email", so the user typed `celes` (their chosen name) and it never
resolved. Verified via `initiate-auth`: signing in with the email returned
`NotAuthorizedException` (account reachable, wrong password) while the chosen
name could not resolve.

DEAD END (do not retry): making `preferred_username` a sign-in alias via
`auth-stack.ts` `signInAliases: { preferredUsername: true }`. `AliasAttributes`
is **IMMUTABLE** on an existing pool — `cdk deploy hellodj-auth` fails with
`Invalid request provided: Updates are not allowed for property -
AliasAttributes.` (the update rolls back cleanly). Changing it needs pool
REPLACEMENT, which destroys all users. Not acceptable.

Fix (application-side, no infra change): `invite_registration.create_confirmed_account`
now uses the invitee's **chosen name as the Cognito `Username`** (the pool signs
in by `username`, so the name works for login), mirrors it into
`preferred_username` for display, and falls back to a UUID username only when no
name is chosen (Discord-only login). `register_policy` already forbids
email-shaped names, so the email-alias `Username` constraint is satisfied.
Cognito enforces `Username` uniqueness natively; a create-time
`UsernameExistsException` maps to `UsernameTakenError`, and
`InviteService.register` pre-checks availability BEFORE consuming the single-use
token (a taken name lets the invitee retry on the same link). The account-create
mechanics were extracted from `invite_service.py` into `invite_registration.py`
to stay under the 500-line ceiling. Deploys via the CI/CD pipeline (web-ui source
change → CodeCommit push → image rebuild), NOT `cdk deploy`.

EXISTING accounts created before this fix (e.g. `celes`, username
`76039644-...`) keep their UUID username — Cognito usernames are immutable — so
they can only log in with their **email**. To let such a user log in with their
chosen name, delete + re-invite so the new flow recreates the account with
`Username = <chosen name>`.

- `cognito_auth.py` — `CognitoAuth`: server-side `InitiateAuth`
  (`USER_PASSWORD_AUTH`), `RespondToAuthChallenge` (NEW_PASSWORD_REQUIRED /
  SOFTWARE_TOKEN_MFA), `SignUp`/`ConfirmSignUp`, `ForgotPassword`/
  `ConfirmForgotPassword`. Normalizes Cognito errors to non-enumerating copy;
  never logs secrets.
- `cognito_jwt.py` — verifies id/access token RS256 sig + `iss`/`aud`/
  `token_use`/`exp` against the pool JWKS (PyJWT + `PyJWKClient`) BEFORE trusting
  the `cognito:groups` claim that drives `is_admin`. (The old hosted-UI callback
  read groups without verifying — retired.)
- `auth_forms.py` — the login/challenge/register/recover flow controllers
  (keeps `auth.py` thin, under the 500-line ceiling).
- `auth_ratelimit.py` — best-effort per-pod fixed-window limiter on the auth
  POST routes (Cognito enforces the authoritative throttling).
- Templates: `pages/login.html` (now a real credential form),
  `auth_new_password.html`, `auth_mfa.html`, `auth_register.html`,
  `auth_recover.html`.
- CDK: `auth-stack.ts` app client enables `ALLOW_USER_PASSWORD_AUTH`
  (`authFlows.userPassword`). Deploy via `cdk deploy hellodj-auth`.
- Deps added to the web-ui image: `PyJWT` + `cryptography` (flake.nix,
  requirements.txt, pyproject.toml).

New deps mean a fresh image: web-ui Python change goes CodeCommit push →
pipeline rebuild → `rollout restart deploy/web-ui`. The `USER_PASSWORD_AUTH`
app-client change goes `cdk deploy hellodj-auth`.

### Cognito-managed emails branded (fixed 2026-08-28)

The reported plaintext "Your username is X and temporary password is Y" email
was Cognito's DEFAULT invitation template (the pool had no
`AdminCreateUserConfig.InviteMessageTemplate`, sender `COGNITO_DEFAULT`). Both
Python paths that create users (`invite_service.register` and the migration
`cognito_seeder`) already pass `MessageAction=SUPPRESS`, so that email only
fires for accounts created OUTSIDE those flows (e.g. a console
`AdminCreateUser`). Fix: `auth-stack.ts` now sets branded HTML `userInvitation`
and `userVerification` templates (shared `hellodjEmailShell`, dark-glass palette
matching `invite_email.py`, kept <2000 chars — the Cognito body limit — with the
required `{username}`/`{####}` placeholders intact). Deploy with
`cdk deploy hellodj-auth`. NOTE: the primary invite email is still the SES one
(`invite_email.py`); the Cognito templates are the branded fallback/verification
path, not the main invite.

## CRITICAL WORKFLOW RULES (read first)

1. **DO NOT build/push Docker images locally.** The CI/CD pipeline builds all
   images (Nix OCI on ARM64 CodeBuild) and pushes to ECR. Fix source → commit →
   push to CodeCommit → pipeline rebuilds.
2. **Self-mutation is ENABLED (updated 2026-08-29).** `pipeline-stack.ts` lives
   in `hellodj-cdk/infra/lib/pipeline-stack.ts` with `selfMutation: true`. The
   pipeline has an `UpdatePipeline` (SelfMutate) stage, so a CDK git push
   auto-applies `pipeline-stack.ts` changes (install/build commands, nix.conf,
   cache) AND foundation-stack changes (e.g. `hellodj-eks` GPU NodePool / idle
   window / env / IAM) — NO manual `cdk deploy hellodj-pipeline` /
   `cdk deploy hellodj-eks` needed. The old cross-stack kubectl-handler blocker
   is gone (manifests on per-stage WorkloadsStacks with their own kubectl
   layer; the SelfMutate step redeploys only the pipeline stack, which has no
   kubectl custom resource). Fallback if the self-mutating pipeline breaks: the
   one-time `cd infra && npx cdk deploy hellodj-pipeline` reinstalls it.
3. **Infra manifest/IAM changes** (workloads-stack.ts, eks-stack.ts, auth-stack.ts,
   edge-stack.ts, foundation.ts, bin/hellodj.ts — these stacks now also live in
   `hellodj-cdk/infra`) deploy via `cd infra && npx cdk deploy <stack>` (from the
   `hellodj-cdk` package) — NOT by pushing to CodeCommit.
   The workloads Kubernetes manifests live in the `hellodj-eks` stack (they're
   attached to `eks.cluster.addManifest`), so `cdk deploy hellodj-eks` applies
   web-ui env + IRSA changes.
4. **Component source changes** (web-ui `*.py`, templates, flake.nix, bot code)
   DO take effect on a plain CodeCommit push (pipeline rebuilds the image).
5. **Rolling pods after an image rebuild (READ THIS).** The workloads'
   Kubernetes manifests live in the **`hellodj-eks`** foundation stack (via
   `cluster.addManifest`), NOT in the per-stage `WorkloadsStack` the pipeline
   deploys. This is DELIBERATE: `selfMutation` is OFF because applying manifests
   through the EKS kubectl handler Lambda inside a self-mutating pipeline
   triggers cross-stack custom-resource failures (see
   `.kiro/specs/cdk-standalone-package/design.md`). Consequences:
   - A `git push` rebuilds the component **images** (pipeline → ECR) but does
     NOT roll the pods — the pipeline never deploys `hellodj-eks`.
   - `bin/hellodj.ts` bakes an **immutable commit-hash image tag** into the
     `hellodj-eks` manifests (from `CODEBUILD_RESOLVED_SOURCE_VERSION` at synth,
     or `-c hellodj:imageTag=<sha>`), so re-applying the manifests changes the
     pod spec and K8s rolls automatically — no `kubectl rollout restart` needed.
   - To roll pods after a push, re-apply the manifests with the
     `tools/deploy_workloads.sh` wrapper (now in the `hellodj-cdk` repo — `tools/`
     is at the hellodj-cdk repo root; from HEAD; verifies the ECR image
     exists, pins the tag via context, clean env + private cdk.out — avoids the
     stale-`CODEBUILD_RESOLVED_SOURCE_VERSION` footgun that once shipped a
     non-existent `web-ui:<garbage>` tag and ImagePullBackOff'd the pods).
     Under the hood this is `cdk deploy hellodj-eks -c hellodj:imageTag=<HEAD>`.
   - `kubectl rollout restart` still works as a last-resort manual re-pull of
     `:latest`, but the wrapper is preferred (immutable tag + auto-roll).
   - TAG RESOLUTION is hardened in `bin/hellodj.ts`: explicit `-c
     hellodj:imageTag` ALWAYS wins; `CODEBUILD_RESOLVED_SOURCE_VERSION` is only
     trusted when it matches `^[0-9a-f]{40}$` (a real commit SHA), so a stray
     shell export can't poison the manifest tag.
6. **Pipeline backlog**: rapid pushes queue multiple executions on OLD revisions.
   Stop stale ones and start fresh on HEAD:
   `aws codepipeline stop-pipeline-execution --pipeline-name hellodj-pipeline --pipeline-execution-id <id> --abandon --region us-east-1`
   then `aws codepipeline start-pipeline-execution --name hellodj-pipeline --region us-east-1`

## Key AWS Facts

- **Profile**: `hellodj` (account `874927898283`, region `us-east-1`)
- **EKS cluster**: `hellodj` — `aws eks update-kubeconfig --name hellodj --region us-east-1 --kubeconfig /tmp/hellodj-eks-kubeconfig`
- **Namespaces**: `hellodj-beta`, `hellodj-staging`, `hellodj-production`
- **ECR**: `874927898283.dkr.ecr.us-east-1.amazonaws.com/hellodj/<component>`
- **Pipeline**: `hellodj-pipeline` (Source → Build/synth → ComponentBuilds → beta → staging → production).
  The primary synth source is now **`hellodj-cdk`** (the CDK app + gates + shared
  logic; was `hellodj`); the 12 per-component Nix builds take **`hellodj`** as an
  additional source input (the 12 workloads stay in `hellodj`). Source repo count
  is now **six** (`hellodj-cdk`, `hellodj`, `Lavalink`, `lavaplayer`, `LavaSrc`,
  `youtube-source`).
- **Nix cache**: `s3://hellodj-nix-cache?region=us-east-1`, `require-sigs = false` (working)
- **Cognito**: user pool `us-east-1_C6xFPZt4x` (`hellodj-beta`), web-ui client
  `7e914pnbvn2c8lq8vkme22ds43`, hosted UI domain
  `hellodj-beta-874927898283.auth.us-east-1.amazoncognito.com`, `admins` group
- **Node group**: `hellodj-app-ondemand` scaled to 5 (m7g.large ARM64). Karpenter
  runs with IRSA (SA `karpenter` annotated with role
  `hellodj-eks-ClusterKarpenterSaRoleE6DD4AE8-Qtt7L61yDx8f`); SQS interrupt queue
  `hellodj-beta-karpenter-interruption` created manually.

## Bugs fixed this session (so you don't re-chase them)

1. **503** — deployments used `TODO-pipeline-injected-tag`; changed
   `PLACEHOLDER_IMAGE_TAG` to `latest` + `imagePullPolicy: Always` in workloads-stack.
2. **Karpenter crash-loop** (IMDS 401) — added IRSA SA in eks-stack `installKarpenter`.
3. **Nix cache ignored** (signature reject) — `require-sigs = false` + `?region=us-east-1`.
4. **Pipeline pushed wrong images to wrong repos** — `docker images | head -1` →
   `grep hellodj-<component>` in `getComponentBuildCommands`.
5. **web-ui ModuleNotFoundError** — flake didn't bundle modules; now `cp $src/*.py`.
   Same class of bug hit `hellodj_platform_logic`, `admin_directory`, etc.
6. **Admin sign-in broken** — web-ui had no Cognito env; wired `COGNITO_DOMAIN`,
   `COGNITO_CLIENT_ID`, `HELLODJ_PUBLIC_BASE_URL` + Cognito callback URLs (were
   `https://example.com`).
7. **Login bounce** — 2 replicas each generated own `FLASK_SECRET_KEY`; now a
   shared key from AuthStack secret → k8s Secret `web-ui-flask-secret`.
8. **Static assets 403/404** — CloudFront `/static/*` routed to empty S3; changed
   to ALB origin + `ALL_VIEWER` origin request policy (forwards Host header).
9. **Nav nesting** — HTMX nav returned full shell; pages now `{% extends layout %}`
   (`base.html` full-load / `_partial.html` for `HX-Request`).
10. **Nav active state stuck on Dashboard** — was server-rendered once; now
    client-side via Alpine `isActive(href)` tracking `window.location.pathname`.
11. **Admin panel missing invite UI** — added invite form to `pages/admin.html`.

## Current Feature State: Multi-Tenant Guild Sources

Spec: `.kiro/specs/multi-tenant-guild-sources/{requirements,design}.md`.

Implemented (web-ui, deploys via pipeline):
- `invite_service.py` — admin invites by email (Cognito `admin_create_user`)
- `user_profile.py` — Discord OAuth account linking (GSI1 reverse index)
- `guild_admin_service.py` — guild ownership + appoint admins by Discord id;
  pure `can_manage_guild()` gate
- `guild_sources.py` — per-guild source OAuth in ISOLATED Secrets Manager
  secrets `hellodj/<stage>/guild/<gid>/<provider>`
- `guild_routes.py` — `/account`, `/guilds/<id>`, source connect/disconnect
- `source_oauth.py` — provider authorize-URL builders
- `bootstrap.py` — builds CoreTable + services from env
- `auth.py` — Discord link flow + per-guild source OAuth connect/callback

platform_logic: added `CoreTable.query_pk_prefix`, `delete`; `delete_item` on
ReadThroughTable/TableLike.

Infra (deployed via `cdk deploy hellodj-eks`):
- web-ui SA: Cognito `AdminCreateUser` + Secrets Manager RW on `hellodj/<stage>/guild/*`
- bot-path SAs (discord-bot-core, tidal/spotify-stream, lavalink): READ on same prefix
- web-ui env: `DISCORD_CLIENT_SECRET` + `SPOTIFY_CLIENT_ID`/`TIDAL_CLIENT_ID`/`GOOGLE_CLIENT_ID`

## Unified source-credential store + durable watchdog (unified-oauth-and-token-watchdog spec)

This spec replaced the per-guild Secrets Manager secret model with a **unified
per-user DynamoDB credential store** (envelope-encrypted) and a **durable
token-refresh watchdog**. It also closes the "silent broken authorize URL" and
OAuth env-wiring gaps that used to sit in KNOWN GAPS below.

- **Storage:** one `hellodj-core` item per user+provider —
  `PK=USER#<sub>`, `SK=SOURCECRED#<provider>`, `entityType=SourceCredential`.
  Plaintext status (`connected`, `expires_at`, `last_refresh_at`,
  `refresh_status`, …) + an envelope-encrypted token blob (`enc_blob` +
  `enc_key` + `kms_key_id`). Never a plaintext token; tokens are never logged.
- **Encryption:** shared `hellodj_platform_logic.token_crypto`
  (`encrypt_blob`/`decrypt_blob`, AES-GCM under a KMS data key). Double at rest:
  table KMS + app-layer envelope.
- **New CMK:** `alias/hellodj-source-creds-<stage>` (rotation on), created in
  `data-stack.ts`, id wired as `HELLODJ_SOURCE_CREDS_KMS_KEY_ID`. Deploy the CMK
  with `cdk deploy hellodj-data`.
- **Least privilege** (`SOURCE_CREDENTIAL_KMS_COMPONENTS` in
  `workloads-stack.ts`): `web-ui` (writer: GenerateDataKey+Encrypt+Decrypt) &
  `playback-orchestrator` (watchdog: same) get RW on the table + full CMK;
  `discord-bot-core`/`tidal-stream`/`spotify-stream` (readers) get table read +
  CMK **Decrypt only**. Nothing else gets a CMK grant.
- **Watchdog:** durable token-refresh loop in the standing
  `playback-orchestrator` container (survives a bot bounce), started on a daemon
  thread next to its `/healthz` server. Enumerates near-expiry
  `SourceCredential` items (`CoreTable.scan_entity`, key-projected, never pulls
  `enc_blob`), refreshes via provider `RefreshClient`s, writes back with an
  optimistic lock (multi-replica safe). Per-item failure isolation; degraded
  no-op when no store/KMS/clients. Tidal still routes through the existing
  first-party `tidal_refresh` unchanged.
- **New env vars:**
  - web-ui + playback-orchestrator + readers: `HELLODJ_SOURCE_CREDS_KMS_KEY_ID`.
  - web-ui + playback-orchestrator: `HELLODJ_GOOGLE_OAUTH_SECRET_ARN`,
    `HELLODJ_DISCORD_OAUTH_SECRET_ARN`, `DISCORD_CLIENT_ID`, provider
    client id/secret envs.
  - web-ui: `POTOKEN_SERVER_URL` (defaults to in-namespace `potoken-server`).
  - playback-orchestrator: `TOKEN_WATCHDOG_INTERVAL` (default 300s),
    `TOKEN_WATCHDOG_THRESHOLD` (default 600s).
- **Readers:** `bot/playback/guild_credentials.py` resolves the DynamoDB item +
  decrypts via `token_crypto`, falling back to the legacy `hellodj/<stage>/guild/*`
  secret. Preserves the YouTube `POST /youtube` all-fields-together swap + TTL
  cache.
- **Migration backfill:** one-shot Job in `platform/components/migration`
  (`python -m migration_job.backfill_main`, needs
  `HELLODJ_SOURCE_CREDS_KMS_KEY_ID` + stage/region) reads existing
  `hellodj/<stage>/guild/*` secrets and writes encrypted `SourceCredential`
  items; idempotent; logs counts only. Run AFTER the CMK + IAM are deployed.
- **Default source is `youtube`** (unset resolves to YouTube in the config layer
  and the bot source map; the config form preselects it).

## KNOWN GAPS / NOT YET DONE (likely debugging targets)

1. ~~**Source OAuth client ids/secrets are EMPTY env**~~ *(addressed by
   unified-oauth-and-token-watchdog, then finished 2026-08-29)* —
   `workloads-stack.ts` wires the provider client id/secret envs (+
   `HELLODJ_GOOGLE_OAUTH_SECRET_ARN`, `HELLODJ_DISCORD_OAUTH_SECRET_ARN`,
   `POTOKEN_SERVER_URL`); when a provider client id is absent the UI renders a
   disabled "Needs setup" control instead of a link that no-ops (R1.2).
   **Fixed 2026-08-29 (source-oauth-account-page):**
   - The Spotify/Tidal client ids are now threaded via `cdk.json` context
     (`hellodj:spotifyClientId` / `hellodj:tidalClientId`) → `bin/hellodj.ts`
     foundation props → `SPOTIFY_CLIENT_ID` / `TIDAL_CLIENT_ID` env (they were
     never wired, so those envs were empty regardless of the secret). The
     `hellodj/<stage>/spotify` secret was populated with the real
     `{client_id, client_secret}`. Tidal is single-app-id (PKCE) — no client
     secret is consumed anywhere.
   - **YouTube / YouTube Music no longer need a Google Cloud OAuth app.** There
     is none (the on-prem cred DB has only a refresh token, no client id/secret
     — the youtube-source plugin uses a PUBLIC "TV / limited-input device"
     client, `861556708454-…apps.googleusercontent.com`, baked into the jar).
     The web-ui Account page now drives that SAME device-code flow
     (`youtube_device_oauth.py`): Connect → shows a user code + verification URL
     → HTMX-polls `/auth/oauth/youtube/device/poll` → on authorize, pairs the
     offline refresh token with a fresh PoToken and stores an encrypted
     `SourceCredential`. `source_provider_configured('youtube'/'youtube_music')`
     is therefore always True (no `GOOGLE_CLIENT_ID` needed). The durable
     watchdog refreshes device-issued tokens with the SAME public client at
     `youtube.com/o/oauth2/token` via
     `source_refresh.youtube_device_refresh_client` (used by
     `watchdog_bootstrap.build_clients_by_provider` when no operator
     `GOOGLE_CLIENT_ID`/secret is set). Verified the device endpoint still
     issues codes (HTTP 200) 2026-08-29.
   Deploy: web-ui + orchestrator source changes go via the pipeline (push →
   image rebuild → `cdk deploy hellodj-eks -c hellodj:imageTag=<HEAD>` to roll);
   the client-id env is picked up by `cdk deploy hellodj-eks` (context now in
   `cdk.json`).
2. **Discord login** (`/auth/login`) — needs `DISCORD_CLIENT_ID` (+ secret for
   the callback token exchange). The `DISCORD_CLIENT_ID` +
   `HELLODJ_DISCORD_OAUTH_SECRET_ARN` env wiring is now present; the login
   *route* wiring may still need finishing. Only Cognito admin login is fully
   wired.
3. ~~**Bot-side per-guild credential resolution**~~ *(addressed by
   unified-oauth-and-token-watchdog)* — `bot/playback/guild_credentials.py` now
   has a DynamoDB-backed `DynamoCredentialResolver` (built via
   `build_dynamo_credential_resolver()`, consulted FIRST) with the legacy
   per-guild secret as fallback, decrypting via `token_crypto`. `bot.py` wires it
   into `GuildCredentialResolver` + `YouTubeCredentialInjector`.
4. **Source connect stores only the auth code** — the code→token exchange is
   delegated to the streaming sidecars (they own client secrets). Verify the
   sidecars actually complete the exchange against the guild secret.
5. **Guild ownership claim** — `can_manage_guild` uses OWNER/admin edges, but
   nothing yet CREATES the OWNER edge on first guild access. Decide the claim
   path (currently `GuildAdminService.claim_ownership` exists but isn't called
   from a route).
6. **Config page** is still GLOBAL config — per-guild config exists in
   `ConfigStore.get_guild/set_guild` but the UI config form writes global.
7. **Dashboard stats** are all 0 (placeholder `_dashboard_stats` / `_guild_list`
   return empty — not wired to live data).

## Debugging Commands

```bash
# kubeconfig
AWS_PROFILE=hellodj aws eks update-kubeconfig --name hellodj --region us-east-1 --kubeconfig /tmp/hellodj-eks-kubeconfig

# pod status / logs
KUBECONFIG=/tmp/hellodj-eks-kubeconfig kubectl get pods -n hellodj-beta
KUBECONFIG=/tmp/hellodj-eks-kubeconfig kubectl logs <pod> -n hellodj-beta --tail=40

# what image/env is deployed
KUBECONFIG=/tmp/hellodj-eks-kubeconfig kubectl get deploy web-ui -n hellodj-beta -o json > /tmp/wd.json  # then inspect env/image

# HTTP trace (headers show origin: gunicorn vs awselb vs AmazonS3, x-cache)
curl -sS -D - -o /dev/null "https://beta.us-east-1.hellodj.bot/<path>"

# pipeline state
AWS_PROFILE=hellodj aws codepipeline get-pipeline-state --name hellodj-pipeline --region us-east-1 --query 'stageStates[*].{stage:stageName,status:latestExecution.status}' --output json

# component build project ids (generic names): the web-ui build project is
# pipelinePipelineComponentBu-EUil8fIxbqV9 ; logs group /aws/codebuild/<project>
```

## Gate Commands (must pass before push)

```bash
# CDK app + gates now live in the hellodj-cdk repo (run from that package):
cd infra && npx tsc --noEmit && npx jest          # 282 tests (23 suites)
# Component sources stay in hellodj (run from that repo):
cd platform/components/web-ui && ruff check --target-version py314 . && python3 -m pytest tests/ -q  # 384 tests
cd platform/components/playback-orchestrator && ruff check --target-version py314 . && python3 -m pytest tests/ -q  # 55 tests (watchdog)
cd platform/components/migration && ruff check --target-version py314 . && python3 -m pytest tests/ -q  # 21 tests (backfill)
# check_line_count.py moved to hellodj-cdk/tools/; it still targets the hellodj component trees:
python3 tools/check_line_count.py <hellodj>/platform/components/web-ui <hellodj>/platform/components/playback-orchestrator <hellodj>/platform/components/migration   # 500-line ceiling
```

> Note: `hellodj_platform_logic` now lives in `hellodj-cdk` at
> `shared/hellodj_platform_logic/` (it is no longer under
> `platform/components/`). Its tests have a few that
> import `boto3` (`test_data_access_property.py`) or the pinning/verify tooling
> (`test_apply_bump.py`, `test_gate_pins.py`, `test_verify_all.py`,
> `test_verify_integration_cache_synth_jest.py`) which fail to COLLECT in a bare
> local venv without `boto3` / the tools on `sys.path`. These are pre-existing
> environment gaps, NOT test failures — the CI image has those deps. The spec's
> own package tests (`test_token_crypto.py`, `test_source_refresh_property.py`,
> `test_data_access.py`, `test_tidal_refresh_property.py`) pass.

## Local shell gotcha

The user's shell has a starship/custom prompt that corrupts captured command
output when interleaved. Write to a file and read it, or use plain
`aws ... --output json | python3 -c ...` piped carefully. `kubectl exec` needs a
Running pod; the Nix images have no `grep`/`bash` — use `python3 -c` inside them.

## Owner

Project owned by the Platform_Owner (see login page attribution — the name is
stored crawler-resistant: reversed + entity-encoded, flipped via CSS).
