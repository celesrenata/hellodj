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
2. **Self-mutation is DISABLED.** Changes to `platform/infra/lib/pipeline-stack.ts`
   (install/build commands, nix.conf, cache config) DO NOT take effect on a
   CodeCommit push alone — the CodeBuild buildspecs are frozen at `cdk deploy`
   time. For pipeline-stack.ts changes: commit + push, THEN
   `cd platform/infra && npx cdk deploy hellodj-pipeline`, THEN start a new
   pipeline execution.
3. **Infra manifest/IAM changes** (workloads-stack.ts, eks-stack.ts, auth-stack.ts,
   edge-stack.ts, foundation.ts, bin/hellodj.ts) deploy via
   `cd platform/infra && npx cdk deploy <stack>` — NOT by pushing to CodeCommit.
   The workloads Kubernetes manifests live in the `hellodj-eks` stack (they're
   attached to `eks.cluster.addManifest`), so `cdk deploy hellodj-eks` applies
   web-ui env + IRSA changes.
4. **Component source changes** (web-ui `*.py`, templates, flake.nix, bot code)
   DO take effect on a plain CodeCommit push (pipeline rebuilds the image).
5. **`:latest` + imagePullPolicy: Always** — but a running pod won't re-pull a
   new `:latest` until it restarts. After the pipeline pushes a new image:
   `KUBECONFIG=/tmp/hellodj-eks-kubeconfig kubectl rollout restart deploy/web-ui -n hellodj-beta`
6. **Pipeline backlog**: rapid pushes queue multiple executions on OLD revisions.
   Stop stale ones and start fresh on HEAD:
   `aws codepipeline stop-pipeline-execution --pipeline-name hellodj-pipeline --pipeline-execution-id <id> --abandon --region us-east-1`
   then `aws codepipeline start-pipeline-execution --name hellodj-pipeline --region us-east-1`

## Key AWS Facts

- **Profile**: `hellodj` (account `874927898283`, region `us-east-1`)
- **EKS cluster**: `hellodj` — `aws eks update-kubeconfig --name hellodj --region us-east-1 --kubeconfig /tmp/hellodj-eks-kubeconfig`
- **Namespaces**: `hellodj-beta`, `hellodj-staging`, `hellodj-production`
- **ECR**: `874927898283.dkr.ecr.us-east-1.amazonaws.com/hellodj/<component>`
- **Pipeline**: `hellodj-pipeline` (Source → Build/synth → ComponentBuilds → beta → staging → production)
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

## KNOWN GAPS / NOT YET DONE (likely debugging targets)

1. **Source OAuth client ids/secrets are EMPTY env** — `SPOTIFY_CLIENT_ID`,
   `TIDAL_CLIENT_ID`, `GOOGLE_CLIENT_ID`, `DISCORD_CLIENT_SECRET` default to "".
   The per-guild "Connect" buttons no-op until these are populated from Secrets
   Manager into the web-ui deployment env (workloads-stack `containerEnv`).
2. **Discord login** (`/auth/login`) — needs `DISCORD_CLIENT_ID` (+ secret for
   the callback token exchange). Only Cognito admin login is fully wired.
3. **Bot-side per-guild credential resolution** — designed (bot reads
   `hellodj/<stage>/guild/*`), IAM granted, but the bot's `player.py`/lavasrc
   integration to actually LOAD per-guild tokens at play time may not be wired
   yet. Verify `bot/playback/guild_credentials.py` exists / is used.
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
cd platform/infra && npx tsc --noEmit && npx jest          # 226 tests
cd platform/components/web-ui && ruff check --target-version py314 . && python3 -m pytest tests/ -q  # 24 tests
python3 platform/tools/check_line_count.py platform/components/web-ui   # 500-line ceiling
```

## Local shell gotcha

The user's shell has a starship/custom prompt that corrupts captured command
output when interleaved. Write to a file and read it, or use plain
`aws ... --output json | python3 -c ...` piped carefully. `kubectl exec` needs a
Running pod; the Nix images have no `grep`/`bash` — use `python3 -c` inside them.

## Owner

Project owned by the Platform_Owner (see login page attribution — the name is
stored crawler-resistant: reversed + entity-encoded, flipped via CSS).
