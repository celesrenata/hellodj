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

## Admin sign-in "temporarily unavailable" (fixed 2026-08-29)

Symptom: `/auth/admin` could not sign in — the login POST always rendered
"Sign-in is temporarily unavailable." Root cause (facts): the deployed web-ui
pod had NO `COGNITO_CLIENT_ID` env var, so `web-ui/cognito_auth.build_cognito_auth()`
returned `None` and `auth_forms.handle_login` short-circuited to the degraded
message (login was impossible, not just wrong-password). The env was empty
because `bin/hellodj.ts` reads the client id from context `hellodj:cognitoClientId`
(default `''`) and NOTHING supplied it: cdk.json didn't have it and the pipeline
synth never resolved it. Verified: live pool `us-east-1_C6xFPZt4x`, web-ui
client `7e914pnbvn2c8lq8vkme22ds43`, `ALLOW_USER_PASSWORD_AUTH` enabled, no
client secret (public client — matches `cognito_auth.py`).

Fix (self-healing, in `hellodj-cdk`, NOT a rotting cdk.json literal — that's the
ALB-DNS anti-pattern): the pipeline synth step now resolves EACH stage's live
Cognito pool + web-ui client id from the `hellodj-auth` foundation (mirroring the
OIDC-ARN resolution) and threads them as PER-STAGE JSON maps
`-c hellodj:cognitoClientIdByStage={...}` / `-c hellodj:cognitoUserPoolIdByStage={...}`.
`bin/hellodj.ts` parses the maps (`stageMapContext`) into
`foundation.cognitoClientIdByStage` / `...UserPoolIdByStage`; each per-stage
`HelloDjStage` selects ITS stage's id (falling back to the single-stage
`cognitoClientId` for a standalone deploy). Each stage has a DISTINCT
`hellodj-<stage>` pool, so this is per-stage correct — beta's id never leaks into
staging/production (regression-guarded by a test). Deploys via a plain
CodeCommit push (pipeline synth resolves + the per-stage WorkloadsStack rolls);
manual fallback `tools/deploy_workloads.sh --stage beta` threads the same context.

### Admin sign-in STILL unavailable — synth context word-split (fixed 2026-08-29, take 2)

The per-stage Cognito resolution above was correct AND the synth resolved the
right ids (build log: `resolved Cognito clients: {"beta": "7e914..."}`), but the
deployed web-ui STILL had NO `COGNITO_CLIENT_ID` — admin login stayed
"temporarily unavailable". Root cause (facts, from the synth CodeBuild log): the
resolved map was `{"beta": "<id>"}` (Python `json.dumps` default separator puts
a SPACE after the colon), and the synth step expanded it into
`npx cdk synth $CTX` **UNQUOTED**. The shell word-split the value on that inner
space, handing cdk a truncated `-c hellodj:cognitoClientIdByStage={"beta":`
(invalid JSON) as one arg and `"<id>"}` as another. `stageMapContext` in
`bin/hellodj.ts` caught the `JSON.parse` failure and returned `undefined` →
empty per-stage map → each `HelloDjStage` fell back to the empty single-stage
`cognitoClientId` → `workloads-stack.ts` skipped the `COGNITO_CLIENT_ID` env →
`build_cognito_auth()` returned None → "unavailable". So the map was resolved but
never reached the manifest.

Fix (source, `hellodj-cdk/infra/lib/pipeline-stack.ts` `getBuildCommands`): two
complementary changes so the value survives the shell verbatim — (1) emit the
resolved maps as COMPACT JSON (`json.dumps(d, separators=(',',':'))`, no spaces),
and (2) build the synth context as a bash ARRAY (`CTX=(); CTX+=(-c "k=$V")`) and
invoke `npx cdk synth "${CTX[@]}"` so each `-c key=value` is one argv element,
word-split-proof regardless of the value's contents. Runs on the
`amazonlinux-aarch64-standard:3.0` CodeBuild image (bash arrays are fine).
Regression-guarded in `pipeline-stack.test.ts` (asserts compact separators, the
quoted `-c "...=$COG_CLIENTS"` args, and `synth "${CTX[@]}"`; forbids the old
unquoted `synth $CTX`). Deploys via a plain CodeCommit push — this is a
`pipeline-stack.ts` change, so the pipeline's `UpdatePipeline`/SelfMutate stage
rewrites the synth commands, the next synth threads the quoted context, and the
beta WorkloadsStack redeploys web-ui WITH `COGNITO_CLIENT_ID`.

## CloudFront 502 "couldn't resolve the origin domain name" (fixed 2026-08-29)

Symptom: the whole site returned a CloudFront 502 error page —
"CloudFront wasn't able to resolve the origin domain name." Root cause (facts):
the `hellodj-edge` CloudFront distribution (`ED3GFEKRKFBX4`) had its default/
`static/*` origin **hardcoded** in `hellodj-cdk/infra/bin/hellodj.ts` as the
literal ALB DNS name `k8s-hellodj-15947bf6df-1852676627.us-east-1.elb.amazonaws.com`
(with a comment falsely claiming the name is "stable for the group 'hellodj'").
The AWS Load Balancer Controller had deleted+recreated the shared Ingress ALB,
which changed the AWS-assigned trailing id — the live ALB became
`k8s-hellodj-15947bf6df-**2128421402**...`. The hardcoded old name was
`NXDOMAIN`, so CloudFront could not resolve its origin → 502. (The `k8s-hellodj-
<groupHash>` prefix is stable for the group; the trailing `<elbId>` is NOT — it
changes on every ALB recreation.)

Fix (source, not an imperative CloudFront patch): `bin/hellodj.ts` no longer
bakes the ALB DNS literal. Like the other post-deploy-resolved values in that
file (`oidcProviderArn`, `daxEndpoint`, `cognitoClientId`, …), the ALB DNS is
threaded via context `hellodj:albDnsName` (absent → static-only S3 fallback,
never a rotting literal). Restored the site by deploying the foundation edge
stack with the current name:

```bash
ALB=$(aws elbv2 describe-load-balancers --region us-east-1 \
  --query "LoadBalancers[?starts_with(LoadBalancerName,'k8s-hellodj-')].DNSName | [0]" \
  --output text --profile hellodj)
cd infra && npx cdk deploy hellodj-edge -c hellodj:albDnsName=$ALB \
  --profile hellodj --require-approval never
```

`hellodj-edge` is a FOUNDATION stack (top-level in `bin/hellodj.ts`), NOT a
pipeline stage — a plain git push does NOT redeploy it; it needs the explicit
`cdk deploy hellodj-edge`. Verified after deploy: origin =
`k8s-hellodj-15947bf6df-2128421402...`, and `GET https://beta.us-east-1.hellodj.bot/healthz`
+ `/login` return HTTP 200 (`server: gunicorn`, `via: ...cloudfront.net`).

REMAINING FRAGILITY (follow-up, NOT yet done): the origin is still the RAW ELB
DNS name, so the NEXT ALB recreation will break CloudFront again and require
another `cdk deploy hellodj-edge -c hellodj:albDnsName=<new>`. The durable fix
is a stable Route53 indirection — an origin-only record (e.g. `origin.<envName>`)
kept pointed at the current ALB by external-dns, with CloudFront's origin set to
that stable name — so a future recreation never breaks the edge. Not yet
implemented.

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
   cache, stages) — NO manual `cdk deploy hellodj-pipeline` needed. The old
   cross-stack kubectl-handler blocker is gone (manifests on per-stage
   WorkloadsStacks with their own kubectl layer; the SelfMutate step redeploys
   only the pipeline stack, which has no kubectl custom resource). Fallback if
   the self-mutating pipeline breaks: the one-time
   `cd infra && npx cdk deploy hellodj-pipeline` reinstalls it.
   **CORRECTION (2026-08-29): self-mutation does NOT deploy the FOUNDATION
   stacks** (`hellodj-eks`, `hellodj-data`, `hellodj-auth`, `hellodj-network`).
   Those are top-level `app` stacks deployed ONCE outside the pipeline, NOT
   pipeline stages — self-mutation only rewrites the pipeline stack's own
   template. A change to a foundation stack (e.g. the new `hellodj-kubectl`
   role in `hellodj-eks`, or its GPU NodePool / idle window / env / IAM)
   requires an explicit `cd infra && npx cdk deploy hellodj-eks`. Only
   `pipeline-stack.ts` changes and per-stage WorkloadsStack manifests ride a
   plain push.
3. **FOUNDATION infra/IAM changes** (`eks-stack.ts`, `auth-stack.ts`,
   `data-stack.ts`, `edge-stack.ts`, `network-stack.ts`, and the FOUNDATION
   props in `bin/hellodj.ts` — all in `hellodj-cdk/infra`) deploy via
   `cd infra && npx cdk deploy <stack>` (from the `hellodj-cdk` package) — NOT
   by pushing to CodeCommit. NOTE: `hellodj-eks` now owns ONLY the shared
   cluster + GPU NodePool + NVIDIA device plugin. It NO LONGER owns the workload
   manifests (web-ui env, IRSA SAs, Deployments) — those moved to the per-stage
   `WorkloadsStack` (push-triggered-rolling-deploy spec). So `cdk deploy
   hellodj-eks` applies GPU/cluster changes; web-ui env + IRSA changes ride the
   pipeline (see #5).
4. **Component source changes** (web-ui `*.py`, templates, flake.nix, bot code)
   DO take effect on a plain CodeCommit push (pipeline rebuilds the image).
5. **Rolling pods after an image rebuild (READ THIS — UPDATED).** The workloads'
   Kubernetes manifests live in the per-stage **`WorkloadsStack`**
   (`hellodj-pipeline/hellodj-<stage>-stage/hellodj-workloads-<stage>`), which
   the **pipeline deploys**, NOT in the `hellodj-eks` foundation stack. This is
   the current model after the `push-triggered-rolling-deploy` spec:
   `selfMutation` is **ENABLED**; each per-stage WorkloadsStack imports the
   shared cluster with its OWN kubectl layer (so there is no cross-stack
   kubectl-handler failure), and the pipeline deploys the WorkloadsStacks as
   ordered stages. Consequences:
   - A `git push` rebuilds the component **images** (pipeline → ECR) AND the
     pipeline deploys the per-stage WorkloadsStacks — so a plain push **does**
     roll the pods. This is the normal, correct path.
   - The pipeline synth resolves post-deploy foundation outputs from LIVE AWS
     and threads them via `-c` (mirroring how the OIDC ARN is resolved):
     - the immutable image tag (`hellodj:imageTag=<hellodj_Source SHA>`), so
       each run changes the pod spec and K8s rolls automatically;
     - the real cluster OIDC provider ARN (`hellodj:oidcProviderArn`) for IRSA
       trust;
     - EACH stage's Cognito pool + web-ui client id as PER-STAGE JSON maps
       (`hellodj:cognitoClientIdByStage` / `hellodj:cognitoUserPoolIdByStage`),
       so the web-ui gets `COGNITO_CLIENT_ID` and admin sign-in at `/auth/admin`
       works. Each stage has its OWN `hellodj-<stage>` pool, so the maps are
       resolved per stage — never a single shared id leaking beta's pool into
       staging/production. Stages whose pool doesn't exist yet are omitted.
   - MANUAL fallback (pipeline wedged, or force one stage to re-pull without a
     full promotion): `tools/deploy_workloads.sh [--stage <stage>]` (in the
     `hellodj-cdk` repo). It deploys the per-stage WorkloadsStack directly
     (`cdk deploy hellodj-pipeline/hellodj-<stage>-stage/hellodj-workloads-<stage>`),
     pins the immutable HEAD tag, verifies the ECR image exists, and threads the
     SAME OIDC + per-stage Cognito context the pipeline synth resolves (so a
     manual roll never regresses IRSA or admin login). It targets the
     WorkloadsStack — NOT `cdk deploy hellodj-eks` (that stack no longer holds
     the workloads).
   - `kubectl rollout restart` still works as a last-resort manual re-pull of
     `:latest`, but the pipeline (or the wrapper) is preferred (immutable tag +
     auto-roll).
   - TAG RESOLUTION is hardened in `bin/hellodj.ts`: explicit `-c
     hellodj:imageTag` ALWAYS wins; `CODEBUILD_RESOLVED_SOURCE_VERSION` is only
     trusted when it matches `^[0-9a-f]{40}$` (a real commit SHA), so a stray
     shell export can't poison the manifest tag.
6. **Pipeline backlog**: rapid pushes queue multiple executions on OLD revisions.
   Stop stale ones and start fresh on HEAD:
   `AWS_PROFILE=hellodj aws codepipeline stop-pipeline-execution --pipeline-name hellodj-pipeline --pipeline-execution-id <id> --abandon --region us-east-1`
   then `AWS_PROFILE=hellodj aws codepipeline start-pipeline-execution --name hellodj-pipeline --region us-east-1`

7. **Pushing to CodeCommit needs the profile.** Every `git push` to the
   `codecommit` remote MUST be prefixed `AWS_PROFILE=hellodj git push codecommit
   <branch>`. The repo's git `credential.helper` is the bare
   `!aws codecommit credential-helper $@` (no `--profile`), so a plain
   `git push codecommit main` fails with a MISLEADING
   `fatal: repository '.../v1/repos/hellodj/' not found` — that is an AUTH
   failure, not a missing repo (the repo exists; `aws codecommit
   get-repository --repository-name hellodj` succeeds under the profile).
   Do NOT chase the URL/trailing-slash; just set `AWS_PROFILE=hellodj`.

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

### Account-level delegated admins (co-admins by Discord id, Option B)

Distinct from PLATFORM admins (Cognito `admins` group) and GUILD admins
(`guild_admin_service`): a user can appoint co-admins of THEIR OWN account by
Discord user id, on the `/account` page. Option B — an appointed Discord id that
Discord-OAuths in logs STRAIGHT INTO the owner's account (shared access; session
identity = owner's Cognito `sub`) and lands on the dashboard.

- `account_admin_service.py` — `AccountAdminService` over `hellodj-core`:
  edge `PK=USER#<owner_sub>` / `SK=ACCTADMIN#<discordId>`, entity `AccountAdmin`,
  GSI1 reverse (`DISCORD#<id>` / `ACCTADMIN#<owner_sub>`). `appoint_admin`
  (idempotent) / `remove_admin` / `list_admins` / `owner_for_discord`
  (login-path resolver; lexically-first owner when multi-appointed).
- `guild_routes.py` — `POST /account/admins` (appoint, numeric-id-gated) +
  `POST /account/admins/<discord_id>/remove`, keyed by the caller's own session
  `sub` (a user only manages their OWN account's admins). HTMX partial
  `partials/account_admin_list.html`; new "Account administrators" section in
  `pages/account.html`.
- `discord_session.py` — `establish_discord_session` (EXTRACTED from `auth.py`
  to stay under the 500-line ceiling; auth.py imports it). Resolves the login
  target: own linked account first (`user_for_discord`), else the appointed
  owner via `AccountAdminService.owner_for_discord`. Records
  `acting_as_account_admin` + `admin_actor_discord_id` on the session for audit;
  the session's `discord_id` stays the OWNER's linked id so owner-scoped
  authorization keeps resolving to the owner. Neither-linked-nor-appointed still
  bounces to login with `not_linked`.
- `bootstrap.py` / `app.py` — `account_admin` service built + registered as an
  app extension (None in degraded mode).
- Deploys via the pipeline (web-ui source change). No infra/IAM change — reuses
  the existing `hellodj-core` table + web-ui IRSA.

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

## Guild activation key + `/activate` gate (AWS port, 2026-08-29)

Port of the on-prem activation gate (a guild does NOTHING until an admin runs
`/activate <key>` with the key from the web dashboard — stops arbitrary people
adding the bot and using it). AWS had only `GuildPolicy` (admin-portal approve),
NOT the key/`/activate` UX; this adds it, backed by `hellodj-core`.

- **web-ui** `guild_activation_service.py` — `GuildActivationService` over
  `PK=GUILD#<gid>` / `SK=ACTIVATION` (entityType `GuildActivation`,
  `data={key, activated}`). `get_or_create_key` mints `secrets.token_urlsafe(16)`
  on first panel view (idempotent); `regenerate_key` mints a new key AND clears
  `activated` (on-prem deactivate parity). Shown on the guild-detail
  **Activation** tab (`partials/guild_activation.html`) with a regenerate
  control; `POST /guilds/<gid>/activation/regenerate` (ownership-gated).
- **discord-bot-core** `policy/activation.py` (`GuildActivation` +
  `CoreTableActivationStore` reading the SAME item) and
  `commands/activation_cog.py` (`/activate <key>` command + a GLOBAL command
  check that blocks every command in an unactivated guild EXCEPT `activate`;
  DMs allowed). Wired in `main.py` after `gateway.build()`; when no core table
  is configured the store is None and the gate treats every guild as LOCKED
  (secure default). `command_allowed` is a pure, unit-tested decision.
- **Entitlement gate on "add a server"** (R12.3): `discord_guilds_routes.
  add_guild_claim` now refuses claiming a NEW guild once the user's owned-guild
  count reaches their effective `max_guilds` (`entitlements_core.quota_reached`,
  via `EntitlementService.get_effective`) → `?add=guild_quota`. Re-claiming an
  already-owned guild is exempt. Per-guild bot count stays gated by
  `max_bots_per_guild` on the Bots tab (`bot_app_pool.assign_next`).
- **Bot joining a server** is the Bots-tab flow (unchanged): assign a pool app
  → click its Discord invite URL. A bot CANNOT self-join (Discord requires the
  user to authorize), so "add a server" claims ownership + lands on the detail
  page; the invite click is the join. Full chain: add server → Bots: add a bot
  → click invite → Activation: `/activate <key>` in Discord → bot works.
- Both components deploy via the pipeline (source push → image rebuild → roll).
  No infra/IAM change — reuses `hellodj-core`; discord-bot-core already had the
  core-table read grant + `HELLODJ_CORE_TABLE` env for identity.

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
5. ~~**Guild ownership claim**~~ *(FIXED 2026-08-29, "add a server" flow)* —
   there is now an entry point that creates the OWNER edge. `/guilds` has an
   **Add a server** button → `/auth/discord/guilds/connect` starts a Discord
   OAuth with the `identify guilds` scope → `/auth/discord/guilds/callback`
   fetches `/users/@me/guilds`, keeps ONLY guilds the user OWNS or has
   `MANAGE_GUILD` on (`auth_oauth.discord_manageable_guilds_from_code`), stashes
   the candidate id→name map in the session, and renders `pages/guild_select.html`
   → `POST /auth/discord/guilds/claim` claims ownership **only for a guild in the
   session candidate set** (so a user can't claim a server they don't manage),
   then redirects to `/guilds/<id>` (bot invite + sources). Routes live in
   `discord_guilds_routes.py` (registered on the auth blueprint, off the
   500-line-limited `auth.py`). `GuildAdminService.claim_ownership` now stores
   the guild `name` and an `OWNER#<sub>`/`GUILD#<gid>` GSI1 reverse index;
   `guilds_owned_by(sub)` + `guild_name(gid)` back the now-live `/guilds` list
   (`guild_common.user_guild_list`, owned ∪ administered). REQUIRES the fixed
   redirect `/auth/discord/guilds/callback` be registered in the Discord app for
   each stage host (out-of-band, like the source callbacks). Deploys via the
   pipeline (web-ui source change).
6. **Config page** is still GLOBAL config — per-guild config exists in
   `ConfigStore.get_guild/set_guild` but the UI config form writes global.
7. **Member dashboard stats** are still 0 (placeholder `_dashboard_stats` /
   `_guild_list` return empty — not wired to live data). NOTE (2026-08-29):
   the **admin** landing page is now a DISTINCT, role-scoped KPI dashboard —
   admins land on `pages/admin_dashboard.html` (route `/` branches on
   `_is_admin()`) with REAL counts from `admin_dashboard.admin_dashboard_stats`
   (Total Users / Administrators / Disabled Accounts from `AdminDirectory`,
   Pending Invites from `InviteService`, Guilds + Connected Sources via
   `CoreTable.scan_entity('GuildOwner'|'SourceCredential')`; each metric
   degrades to 0 independently). Admin nav is also role-scoped
   (`ADMIN_NAV_ITEMS` = Dashboard/Admin/Entitlements) and intentionally DROPS
   the member-only Config/Guilds/Account entries (`USER_NAV_ITEMS`); the routes
   still exist but aren't surfaced to admins. The member dashboard/nav is
   unchanged.

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
