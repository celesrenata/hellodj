---
name: hellodj-website-debug
description: Debug the HelloDJ web-ui and AWS platform (beta/staging/production at <stage>.us-east-1.hellodj.bot) without re-discovering the setup. Covers pipeline workflow rules, key AWS facts, known bugs already fixed, the multi-tenant guild-sources feature state, known gaps, debugging commands, gate commands, and local shell gotchas. Use when debugging the web-ui on EKS or the AWS SaaS platform.
---

# Website Debug Context — beta.us-east-1.hellodj.bot

## When to use

- Debugging the HelloDJ web-ui or AWS platform (CloudFront → ALB → EKS Flask/gunicorn pods)
- Continuing work on the multi-tenant guild-sources feature or known gaps
- Reasoning about the AWS SaaS pipeline, EKS stacks, or web-ui deployment state

## When NOT to use

- Designing new web pages (use `hellodj-modern-web-ui`)
- Playwright/visual auditing of the site (use `hellodj-audit-crawl` / `hellodj-visual-debug`)
- General pipeline/deployment rules only (use `hellodj-session-context`)

## The site

- URL `https://beta.us-east-1.hellodj.bot/`; stages `beta`/`staging`/`production` at `<stage>.us-east-1.hellodj.bot`
- Path: CloudFront → ALB → EKS web-ui (Flask/gunicorn) pods
- Login works: admin `admin` / `Wkh3llodjbeta!` (Cognito `admins` group)

## Email/SES state (fixed 2026-08-28)

- **Invite flow** sends a branded single-use link via Amazon SES (`invite_email.py` → SES `send_email`), sender `invites@<stage>.<region>.hellodj.bot` (`INVITE_SENDER` env). On failure web-ui catches `MessageRejected`, rolls back the pending invite, and shows a generic "could not send" (R7.4) — SES detail intentionally suppressed, so **pod logs stay clean on send failure**. Don't expect a stack trace.
- **Bug fixed:** CDK had provisioned the sender as an SES *email-address identity* (`ses.Identity.email(...)`), stuck `VerificationStatus: Pending` forever (needs a human click to `invites@...`, no mailbox). Every send rejected.
- **Fix (in `platform/infra/lib/workloads-stack.ts`):** switched to a **domain identity** for `<stage>.<region>.hellodj.bot` via `ses.Identity.domain(...)`, publishing the three Easy-DKIM CNAME tokens (`identity.dkimRecords`) into the delegated `hellodj.bot` Route 53 zone (`HostedZone.fromLookup`). SES self-verifies once CNAMEs resolve — no manual click. web-ui role `ses:SendEmail`/`SendRawEmail` scoped to the domain identity ARN with a `ses:FromAddress` condition pinning From to `invites@<domain>`. Deploy with `cdk deploy hellodj-eks`.
- **Still manual (no CFN resource):** account is in the SES **sandbox** (`ProductionAccessEnabled: false`) — only sends to *verified* recipients even after sender verified. For arbitrary invitees request production access out-of-band: `aws sesv2 put-account-details --production-access-enabled --mail-type TRANSACTIONAL ...`.
- Facts: SES region us-east-1, `SendingEnabled: true`, quota 200/day @ 1 msg/s (sandbox default). Verified identities: `aws ses list-identities`.
- **Cognito branded emails (fixed):** the plaintext "Your username is X and temporary password is Y" email was Cognito's DEFAULT invitation template (pool had no `AdminCreateUserConfig.InviteMessageTemplate`). Both Python user-creation paths (`invite_service.register`, `cognito_seeder`) pass `MessageAction=SUPPRESS`, so that email only fires for accounts created OUTSIDE those flows (e.g. console `AdminCreateUser`). Fix: `auth-stack.ts` sets branded HTML `userInvitation`/`userVerification` templates (shared `hellodjEmailShell`, dark-glass palette matching `invite_email.py`, <2000 chars with `{username}`/`{####}` placeholders). Deploy with `cdk deploy hellodj-auth`. NOTE: the primary invite email is still the SES one; Cognito templates are the branded fallback/verification path.

## CRITICAL workflow rules — read first

1. **DO NOT build/push Docker images locally.** The pipeline builds all images (Nix OCI on ARM64 CodeBuild) and pushes to ECR. Fix source → commit → push to CodeCommit → pipeline rebuilds.
2. **Self-mutation is DISABLED.** Changes to `platform/infra/lib/pipeline-stack.ts` (install/build commands, nix.conf, cache config) DO NOT take effect on a CodeCommit push alone — buildspecs are frozen at `cdk deploy` time. For pipeline-stack.ts changes: commit + push, THEN `cd platform/infra && npx cdk deploy hellodj-pipeline`, THEN start a new pipeline execution.
3. **Infra manifest/IAM changes** (workloads-stack.ts, eks-stack.ts, auth-stack.ts, edge-stack.ts, foundation.ts, bin/hellodj.ts) deploy via `cd platform/infra && npx cdk deploy <stack>` — NOT by pushing. Workloads Kubernetes manifests live in the `hellodj-eks` stack (attached via `eks.cluster.addManifest`), so `cdk deploy hellodj-eks` applies web-ui env + IRSA changes.
4. **Component source changes** (web-ui `*.py`, templates, flake.nix, bot code) DO take effect on a plain CodeCommit push.
5. **`:latest` + imagePullPolicy: Always** — a running pod won't re-pull until it restarts. After a new image push: `KUBECONFIG=/tmp/hellodj-eks-kubeconfig kubectl rollout restart deploy/web-ui -n hellodj-beta`.
6. **Pipeline backlog:** rapid pushes queue executions on OLD revisions. Stop stale ones, start fresh on HEAD:
   `aws codepipeline stop-pipeline-execution --pipeline-name hellodj-pipeline --pipeline-execution-id <id> --abandon --region us-east-1`
   then `aws codepipeline start-pipeline-execution --name hellodj-pipeline --region us-east-1`.

## Key AWS facts

- Profile `hellodj` (account `874927898283`, region `us-east-1`); EKS cluster `hellodj` — `aws eks update-kubeconfig --name hellodj --region us-east-1 --kubeconfig /tmp/hellodj-eks-kubeconfig`
- Namespaces `hellodj-beta`, `hellodj-staging`, `hellodj-production`; ECR `874927898283.dkr.ecr.us-east-1.amazonaws.com/hellodj/<component>`
- Pipeline `hellodj-pipeline` (Source → Build/synth → ComponentBuilds → beta → staging → production)
- Nix cache `s3://hellodj-nix-cache?region=us-east-1`, `require-sigs = false` (working)
- Cognito user pool `us-east-1_C6xFPZt4x` (`hellodj-beta`), web-ui client `7e914pnbvn2c8lq8vkme22ds43`, hosted UI `hellodj-beta-874927898283.auth.us-east-1.amazoncognito.com`, `admins` group
- Node group `hellodj-app-ondemand` scaled to 5 (m7g.large ARM64). Karpenter runs with IRSA (SA `karpenter`, role `hellodj-eks-ClusterKarpenterSaRoleE6DD4AE8-Qtt7L61yDx8f`); SQS interrupt queue `hellodj-beta-karpenter-interruption` created manually.

## Bugs already fixed (don't re-chase)

1. **503** — `TODO-pipeline-injected-tag`; changed `PLACEHOLDER_IMAGE_TAG` to `latest` + `imagePullPolicy: Always` in workloads-stack.
2. **Karpenter crash-loop** (IMDS 401) — added IRSA SA in eks-stack `installKarpenter`.
3. **Nix cache ignored** (signature reject) — `require-sigs = false` + `?region=us-east-1`.
4. **Pipeline pushed wrong images to wrong repos** — `docker images | head -1` → `grep hellodj-<component>` in `getComponentBuildCommands`.
5. **web-ui ModuleNotFoundError** — flake didn't bundle modules; now `cp $src/*.py`. Same class hit `hellodj_platform_logic`, `admin_directory`, etc.
6. **Admin sign-in broken** — wired `COGNITO_DOMAIN`, `COGNITO_CLIENT_ID`, `HELLODJ_PUBLIC_BASE_URL` + callback URLs (were `https://example.com`).
7. **Login bounce** — 2 replicas each generated own `FLASK_SECRET_KEY`; now a shared key from AuthStack secret → k8s Secret `web-ui-flask-secret`.
8. **Static assets 403/404** — CloudFront `/static/*` to empty S3; changed to ALB origin + `ALL_VIEWER` origin request policy (forwards Host header).
9. **Nav nesting** — HTMX nav returned full shell; pages now `{% extends layout %}` (`base.html` full-load / `_partial.html` for `HX-Request`).
10. **Nav active state stuck on Dashboard** — now client-side via Alpine `isActive(href)` tracking `window.location.pathname`.
11. **Admin panel missing invite UI** — added invite form to `pages/admin.html`.

## Feature state: multi-tenant guild sources

Spec: `.kiro/specs/multi-tenant-guild-sources/{requirements,design}.md`.

Implemented (web-ui, deploys via pipeline): `invite_service.py` (admin invites by email), `user_profile.py` (Discord OAuth link, GSI1 reverse index), `guild_admin_service.py` (ownership + appoint admins by Discord id, `can_manage_guild()` gate), `guild_sources.py` (per-guild source OAuth in isolated Secrets Manager secrets `hellodj/<stage>/guild/<gid>/<provider>`), `guild_routes.py` (`/account`, `/guilds/<id>`, source connect/disconnect), `source_oauth.py`, `bootstrap.py`, `auth.py` (Discord link + per-guild source OAuth connect/callback). platform_logic added `CoreTable.query_pk_prefix`, `delete`; `delete_item` on ReadThroughTable/TableLike.

Infra (via `cdk deploy hellodj-eks`): web-ui SA gets Cognito `AdminCreateUser` + Secrets Manager RW on `hellodj/<stage>/guild/*`; bot-path SAs (discord-bot-core, tidal/spotify-stream, lavalink) READ on same prefix; web-ui env `DISCORD_CLIENT_SECRET` + `SPOTIFY_CLIENT_ID`/`TIDAL_CLIENT_ID`/`GOOGLE_CLIENT_ID`.

## Known gaps / not yet done (likely debugging targets)

1. **Source OAuth client ids/secrets are EMPTY env** — `SPOTIFY_CLIENT_ID`, `TIDAL_CLIENT_ID`, `GOOGLE_CLIENT_ID`, `DISCORD_CLIENT_SECRET` default to "". Per-guild "Connect" buttons no-op until populated from Secrets Manager into web-ui deployment env (workloads-stack `containerEnv`).
2. **Discord login** (`/auth/login`) — needs `DISCORD_CLIENT_ID` (+ secret for callback token exchange). Only Cognito admin login is fully wired.
3. **Bot-side per-guild credential resolution** — designed (bot reads `hellodj/<stage>/guild/*`, IAM granted) but bot `player.py`/lavasrc integration to LOAD per-guild tokens at play time may not be wired. Verify `bot/playback/guild_credentials.py` exists/is used.
4. **Source connect stores only the auth code** — code→token exchange delegated to streaming sidecars (they own client secrets). Verify sidecars complete the exchange against the guild secret.
5. **Guild ownership claim** — `can_manage_guild` uses OWNER/admin edges, but nothing CREATES the OWNER edge on first guild access. `GuildAdminService.claim_ownership` exists but isn't called from a route.
6. **Config page is still GLOBAL config** — per-guild config exists in `ConfigStore.get_guild/set_guild` but the UI config form writes global.
7. **Dashboard stats all 0** — placeholder `_dashboard_stats`/`_guild_list` return empty (not wired to live data).

## Debugging commands

```bash
AWS_PROFILE=hellodj aws eks update-kubeconfig --name hellodj --region us-east-1 --kubeconfig /tmp/hellodj-eks-kubeconfig
KUBECONFIG=/tmp/hellodj-eks-kubeconfig kubectl get pods -n hellodj-beta
KUBECONFIG=/tmp/hellodj-eks-kubeconfig kubectl logs <pod> -n hellodj-beta --tail=40
KUBECONFIG=/tmp/hellodj-eks-kubeconfig kubectl get deploy web-ui -n hellodj-beta -o json > /tmp/wd.json   # inspect env/image
curl -sS -D - -o /dev/null "https://beta.us-east-1.hellodj.bot/<path>"   # HTTP trace (origin gunicorn/awselb/AmazonS3, x-cache)
AWS_PROFILE=hellodj aws codepipeline get-pipeline-state --name hellodj-pipeline --region us-east-1 --query 'stageStates[*].{stage:stageName,status:latestExecution.status}' --output json
# web-ui build project: pipelinePipelineComponentBu-EUil8fIxbqV9 ; logs group /aws/codebuild/<project>
```

## Gate commands (must pass before push)

```bash
cd platform/infra && npx tsc --noEmit && npx jest          # 226 tests
cd platform/components/web-ui && ruff check --target-version py314 . && python3 -m pytest tests/ -q  # 24 tests
python3 platform/tools/check_line_count.py platform/components/web-ui   # 500-line ceiling
```

## Local shell gotcha

The user's shell has a starship/custom prompt that corrupts captured command output when interleaved. Write to a file and read it, or use plain `aws ... --output json | python3 -c ...` piped carefully. `kubectl exec` needs a Running pod; the Nix images have no `grep`/`bash` — use `python3 -c` inside them.

## Owner

Project owned by the Platform_Owner (see login page attribution — name stored crawler-resistant: reversed + entity-encoded, flipped via CSS).
