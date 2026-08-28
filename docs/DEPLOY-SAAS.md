# HelloDJ SaaS Platform — Deployment Guide

## Prerequisites

Before deploying, ensure you have:

- Access to the K3s cluster (gremlin-1..4) via `kubectl`
- Push access to `registry.celestium.life` (Harbor)
- The existing CNPG PostgreSQL cluster running at `postgresql-rw.postgresql-service.svc.cluster.local:5432`
- Docker/Podman for building images

## Deployment Order

The SaaS platform adds several new components. Deploy in this order:

```
1. Redis                    (new namespace: redis-service)
2. Kubernetes Secrets       (new secrets for PG URI in hellodj-service)
3. PostgreSQL Schema        (run migration script against CNPG)
4. Data Migration           (SQLite → PostgreSQL)
5. Shared Lavalink Pool     (new StatefulSet in hellodj-service)
6. Bot Image                (rebuild with new dependencies)
7. Web UI Image             (rebuild with new blueprints/services)
8. Apply Kustomization      (deploys everything)
```

---

## Step 1: Deploy Redis

```bash
kubectl apply -k kube/redis/
```

Verify:
```bash
kubectl -n redis-service get pods
kubectl -n redis-service exec -it redis-0 -- redis-cli ping
# Expected: PONG
```

---

## Step 2: Create Kubernetes Secrets

The SaaS platform needs two new secrets in `hellodj-service`:

```bash
# Secret: hellodj-pg-uri (PostgreSQL connection string for the hellodj database)
kubectl -n hellodj-service create secret generic hellodj-pg-uri \
  --from-literal=HELLODJ_PG_URI="postgresql://hellodj:<PASSWORD>@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj" \
  --from-literal=uri="postgresql://hellodj:<PASSWORD>@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj"

# Secret: hellodj-db-key (already exists — same HELLODJ_DB_KEY used by the bot)
# Verify it has the 'HELLODJ_DB_KEY' key:
kubectl -n hellodj-service get secret hellodj-db-key -o jsonpath='{.data}' | base64 -d
```

If `hellodj-db-key` doesn't have a `key` sub-key (needed by pod_spec_builder):
```bash
EXISTING_KEY=$(kubectl -n hellodj-service get secret hellodj-db-key -o jsonpath='{.data.HELLODJ_DB_KEY}' | base64 -d)
kubectl -n hellodj-service patch secret hellodj-db-key \
  --type='json' \
  -p="[{\"op\":\"add\",\"path\":\"/data/key\",\"value\":\"$(echo -n $EXISTING_KEY | base64)\"}]"
```

---

## Step 3: Run PostgreSQL Schema Migration

This creates the `hellodj` database, user, and all tables:

```bash
# From the repo root, using a pod with network access to the CNPG cluster:
kubectl -n hellodj-service run migrate-schema --rm -it \
  --image=registry.celestium.life/hellodj/bot:saas-platform-2026-08-24 \
  --env="HELLODJ_PG_URI=postgresql://postgres@postgresql-rw.postgresql-service.svc.cluster.local:5432/postgres" \
  --command -- python /app/scripts/migrate_schema.py

# Or locally if you have port-forwarding:
# kubectl -n postgresql-service port-forward svc/postgresql-rw 5432:5432
# HELLODJ_PG_URI="postgresql://postgres@localhost:5432/postgres" python scripts/migrate_schema.py
```

---

## Step 4: Data Migration (SQLite → PostgreSQL)

Migrate existing data from the running bot's PVC:

```bash
# Option A: Run migration pods (recommended)
# These exec into the existing bot pod which has the data volume mounted

# Get the bot pod name
BOT_POD=$(kubectl -n hellodj-service get pods -l app.kubernetes.io/name=hellodj -o jsonpath='{.items[0].metadata.name}')

# Migrate credentials (byte-for-byte preservation of Fernet blobs)
kubectl -n hellodj-service exec $BOT_POD -c bot -- \
  python /app/scripts/migrate_credentials.py

# Migrate sessions
kubectl -n hellodj-service exec $BOT_POD -c bot -- \
  python /app/scripts/migrate_sessions.py --tenant-id "00000000-0000-0000-0000-000000000000"

# Migrate playlists
kubectl -n hellodj-service exec $BOT_POD -c bot -- \
  python /app/scripts/migrate_playlists.py --tenant-id "00000000-0000-0000-0000-000000000000"
```

---

## Step 5: Deploy Shared Lavalink Pool

Already included in the main kustomization (via `lavalink-pool/` resource reference).
It deploys automatically in Step 8. To deploy independently:

```bash
kubectl apply -k kube/lavalink-pool/
kubectl -n hellodj-service rollout status statefulset/lavalink-pool
```

---

## Step 6: Build & Push Bot Image

```bash
cd /home/celes/sources/celesrenata/hellodj

# Build (includes new files: credential_store_pg.py, feature_gate.py, heartbeat.py,
# cogs/remote.py, render_lavalink_config.py updates, drift engine, scripts/)
docker build -t registry.celestium.life/hellodj/bot:saas-platform-2026-08-24 -f bot/Dockerfile .
docker push registry.celestium.life/hellodj/bot:saas-platform-2026-08-24
```

---

## Step 7: Build & Push Web UI Image

```bash
cd /home/celes/sources/celesrenata/hellodj

# Build (includes new blueprints, services, templates, static assets)
docker build -t registry.celestium.life/hellodj/web-ui:saas-platform-2026-08-24 -f web-ui/Dockerfile .
docker push registry.celestium.life/hellodj/web-ui:saas-platform-2026-08-24
```

---

## Step 8: Update Image Tags & Apply

Edit `kube/kustomization.yaml` to use the new image tags:

```yaml
images:
  - name: registry.celestium.life/hellodj/bot
    newTag: saas-platform-2026-08-24
  - name: registry.celestium.life/hellodj/web-ui
    newTag: saas-platform-2026-08-24
  - name: registry.celestium.life/hellodj/tidal-stream
    newTag: latest
  - name: registry.celestium.life/hellodj/spotify-stream
    newTag: latest
```

Apply:
```bash
kubectl apply -k kube/
kubectl -n hellodj-service rollout status deployment/hellodj
kubectl -n hellodj-service rollout status deployment/hellodj-web-ui
```

---

## Step 9: Verify Deployment

```bash
# Redis
kubectl -n redis-service get pods -o wide

# Lavalink pool
kubectl -n hellodj-service get pods -l app.kubernetes.io/name=hellodj-lavalink

# Bot pod (init container should now read from PG)
kubectl -n hellodj-service logs -l app.kubernetes.io/name=hellodj -c render-lavalink-config

# Web UI (should show new blueprints loaded)
kubectl -n hellodj-service logs -l app.kubernetes.io/name=hellodj-web-ui | head -20

# Test OAuth2 flow
curl -I https://hellodj.celestium.life/auth/login
# Should redirect to Discord OAuth2

# Test feature flags API (internal)
kubectl -n hellodj-service exec $BOT_POD -c bot -- \
  curl -s http://hellodj-web-ui.hellodj-service.svc.cluster.local:8080/api/v1/features/00000000-0000-0000-0000-000000000000
```

---

## Environment Variables (New)

### Bot Container (added to deployment.yaml)

| Variable | Source | Purpose |
|----------|--------|---------|
| `HELLODJ_PG_URI` | Secret `hellodj-pg-uri` | PostgreSQL connection for credential store |
| `HELLODJ_REDIS_URL` | Value | Redis connection for heartbeat + feature gate |
| `BOT_INSTANCE_ID` | Value (per tenant pod) | Instance ID for heartbeat publisher |

Add to the bot container env in `kube/deployment.yaml`:
```yaml
- name: HELLODJ_PG_URI
  valueFrom:
    secretKeyRef:
      name: hellodj-pg-uri
      key: HELLODJ_PG_URI
- name: HELLODJ_REDIS_URL
  value: "redis://redis.redis-service.svc.cluster.local:6379/0"
```

### Web UI Container (added to web-ui-deployment.yaml)

| Variable | Source | Purpose |
|----------|--------|---------|
| `HELLODJ_PG_URI` | Secret `hellodj-pg-uri` | PostgreSQL for all services |
| `REDIS_URL` | Value | Redis for sessions, rate limiting, pub/sub |
| `DISCORD_CLIENT_ID` | Secret/ConfigMap | Discord OAuth2 app ID |
| `DISCORD_CLIENT_SECRET` | Secret | Discord OAuth2 app secret |
| `OPERATOR_DISCORD_ID` | ConfigMap | Your Discord user ID (admin access) |
| `PAYPAL_MODE` | ConfigMap | `sandbox` or `live` |
| `PAYPAL_BUSINESS_EMAIL` | ConfigMap | `celes@frameshift.net` |

Add to the web-ui container env:
```yaml
- name: HELLODJ_PG_URI
  valueFrom:
    secretKeyRef:
      name: hellodj-pg-uri
      key: HELLODJ_PG_URI
- name: REDIS_URL
  value: "redis://redis.redis-service.svc.cluster.local:6379/0"
- name: OPERATOR_DISCORD_ID
  value: "<YOUR_DISCORD_USER_ID>"
- name: DISCORD_CLIENT_ID
  valueFrom:
    secretKeyRef:
      name: hellodj-discord-oauth
      key: client_id
- name: DISCORD_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: hellodj-discord-oauth
      key: client_secret
- name: PAYPAL_MODE
  value: "sandbox"
- name: PAYPAL_BUSINESS_EMAIL
  value: "celes@frameshift.net"
```

### Init Container (render-lavalink-config)

The init container now uses `HELLODJ_PG_URI` to read from PostgreSQL:
```yaml
- name: HELLODJ_PG_URI
  valueFrom:
    secretKeyRef:
      name: hellodj-pg-uri
      key: HELLODJ_PG_URI
```

---

## New Secrets to Create

```bash
# Discord OAuth2 credentials (for the web portal login)
kubectl -n hellodj-service create secret generic hellodj-discord-oauth \
  --from-literal=client_id="<YOUR_DISCORD_APP_CLIENT_ID>" \
  --from-literal=client_secret="<YOUR_DISCORD_APP_CLIENT_SECRET>"
```

---

## Rollback Procedure

If something goes wrong, the data migration is reversible within 24 hours:

```bash
# Export PG data back to SQLite + JSON format
kubectl -n hellodj-service exec $BOT_POD -c bot -- \
  python /app/scripts/rollback_export.py --output-dir /app/data/

# Revert bot image to pre-SaaS tag
kubectl -n hellodj-service set image deployment/hellodj \
  bot=registry.celestium.life/hellodj/bot:visualizer-menu-2026-08-25

# The init container falls back to SQLite when HELLODJ_PG_URI is not set
# Remove the env var from the deployment to revert:
kubectl -n hellodj-service set env deployment/hellodj -c render-lavalink-config HELLODJ_PG_URI-
```

---

## Post-Deploy: First-Time Setup

1. **Register as operator**: Navigate to `https://hellodj.celestium.life/auth/login`, complete Discord OAuth2. Your tenant is auto-created.

2. **Set your Discord user ID as operator**: The `OPERATOR_DISCORD_ID` env var should match YOUR Discord user ID (visible in `GET /auth/me` response).

3. **Access admin panel**: Navigate to `https://hellodj.celestium.life/api/v1/admin/metrics` — should return JSON metrics if you're the operator.

4. **Test trial flow**: From a different Discord account, login → Apply for Trial → Approve from admin panel.

---

## Post-Deploy: Migrate stale pending invites (one-time)

The invite flow was amended to a single-use tokenized link + branded SES email,
replacing the old Cognito temp-password invitation. Invites created under the
old flow were recorded **without a `token_hash`**, so their links can no longer
be resolved by the new `/invite/<token>` route. Run this one-time migration once
the new web-ui image is deployed to clear them out.

The script lives at `platform/components/web-ui/migrate_invites.py`. It builds
services via `bootstrap.build_services()` (needs `HELLODJ_CORE_TABLE`,
`HELLODJ_COGNITO_USER_POOL_ID`, `AWS_REGION`, and — for `resend` — `INVITE_SENDER`
+ `HELLODJ_PUBLIC_BASE_URL` in the environment / IRSA role), enumerates every
invite, and acts only on **old-flow, still-pending** invites (no `token_hash`,
status `invited`). New-flow invites and non-pending (`accepted`/`revoked`)
invites are left untouched.

Two modes:

- **`expire` (default, no email sent):** marks each old-flow pending invite
  `expired` so the admin panel stops surfacing it as actionable. This is the
  safe default — it sends no email and touches only DynamoDB.
- **`resend`:** mints a fresh single-use token and sends the branded invitation
  email under the new flow, giving each invitee a working link.

```bash
# From within the web-ui component dir (or a pod running the web-ui image),
# with the web-ui env/IRSA in place:
cd platform/components/web-ui

# Default: expire stale old-flow pending invites (no emails sent)
python3 migrate_invites.py

# Or re-send them under the new flow (sends branded SES emails)
python3 migrate_invites.py --mode resend
```

The script prints a JSON summary (`scanned`, `resent`, `expired`, `skipped`,
`migrated`, `errors`) for logging. It is idempotent: once old-flow invites are
expired (or re-sent, which gives them a `token_hash`), a second run reports them
as `skipped`.

> SES note: `resend` sends real email. In SES sandbox mode only verified
> recipients receive mail — see the "SES starts in sandbox mode" note in the
> spec tasks before re-sending to external invitees.

---

## Architecture Changes Summary

| Before | After |
|--------|-------|
| Single bot Pod (4 containers) | Single bot Pod + shared Lavalink pool (2 replicas) |
| SQLite credential store | PostgreSQL (CNPG) + SQLite fallback |
| No auth (operator-only web UI) | Discord OAuth2 + session tokens in Redis |
| No subscriptions/payments | Full subscription + PayPal IPN lifecycle |
| No feature gating | Per-tenant feature flags with Redis cache |
| Single Lavalink sidecar | Shared Lavalink StatefulSet (headless service) |
| No Redis | Redis 7.x in `redis-service` namespace |
| Admin-only web UI | Public portal + tenant dashboards + admin panel + web player |
