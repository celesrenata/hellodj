# HelloDJ Kubernetes Deployment — v2026-08-17

Date: 2026-08-17 (UTC 22:03 → 22:51)

## Deployed

| Service | Image (Harbor) | Tag | Digest |
|---------|----------------|-----|--------|
| bot | `registry.celestium.life/hellodj/bot` | `v2026-08-17` | `sha256:62ba1ef0f71004d844ccbfacdcc9424923d4db82f388a9958c365ab3006e0c01` |
| web-ui | `registry.celestium.life/hellodj/web-ui` | `v2026-08-17` | `sha256:7f0b561a1f1c4cb4474e357eab1dfc1fdb9acd52e296a4cad7f558e12fd5e466` |

- Namespace: `hellodj-service`
- Deployments: `hellodj` (bot + lavalink sidecar), `hellodj-web-ui`, `yt-cipher`
- Ingress: `hellodj-ingress` → `hellodj.celestium.life` (ports 80/443)

## Changes made

1. `kube/bot-configmap.yaml` — added empty-safe `BOT_OWNER_ID: ""` and `STEM_MODEL: ""` (read by `guild_policy.py` and `stems.py`; empty values are safe — policy fails open, stems falls back to importable backend).
2. `kube/kustomization.yaml`, `kube/deployment.yaml`, `kube/web-ui-deployment.yaml` — image tags pinned from `latest` → `v2026-08-17`.
3. `bot/cogs/filters.py` — fixed startup crash (see below).

## Bot startup fix (real bug, not ModuleNotFoundError)

Initial deployment failed with:

```
File "/app/cogs/filters.py", line 469, in Filters
    @filter_group.group(name="stems", ...)
AttributeError: 'Group' object has no attribute 'group'
discord.ext.commands.errors.ExtensionFailed: Extension 'cogs.filters' raised an error
```

Root cause: installed `discord.py` is **2.7.1**, which ships **no** `Group.group()` factory (nested-subcommand API). Verified empirically in the image: `Group.group attr: False`.

Fix: replaced the invalid `@filter_group.group(...)` decorator with an explicit nested group:

```python
stems_group = app_commands.Group(
    name="stems",
    description="Isolate audio stems (vocals/drums/bass/melody)",
    parent=filter_group,
)
```

Verified in the fixed image:
- `filters.py` imports cleanly
- Command tree resolves: `filter > stems > isolate` with qualified name `filter stems isolate`

## Verification (live cluster, reachable)

- `kubectl get pods -n hellodj-service`:
  - `hellodj-68ccdfb785-z5d4t` → **2/2 Running** (bot + lavalink sidecar)
  - `hellodj-web-ui-6d6b598bf4-nkh6g` → **1/1 Running**
  - `yt-cipher-56579858bc-ntg6b` → **1/1 Running**
- `kubectl get svc -n hellodj-service`: `hellodj` (2333/TCP), `hellodj-web-ui` (8080/TCP), `yt-cipher` (8001/TCP)
- Bot logs: `HelloDJ logged in as HelloDJ#8609`, `on_ready fired with 2 guilds`, **no ModuleNotFoundError**, **no startup errors**. All new modules loaded and logged "starting empty": blacklist, allowlist, guild_settings, sleep_settings, guild_policy, file_handler.
- Web-ui logs: gunicorn 26.0.0 started, workers booted, control socket listening at `/app/data/.gunicorn/gunicorn.ctl`.
- `/metrics` route: confirmed registered in running app URL map — `['/api/metrics', '/metrics']`. Both are auth-protected by design (`metrics_page` → `require_auth()`; `api_metrics` → 401 when unauthenticated). HTTP probes returned `403 Forbidden` and `401 UNAUTHORIZED`, proving the routes serve.

## Secrets preserved

- `youtube-secret` live value: length=103, prefix `1//06hzaVjZIFGc3CgYI` — the **valid refresh token is preserved** (not overwritten with empty). Matches `kube/youtube-secret.yaml`. Bot log confirms the OAuth push: `youtube-oauth: pushed refresh token to Lavalink ... /youtube (status=204)`.

## Rollback instructions

If rollback is required, undo to a prior image tag:

```bash
kubectl set image deployment/hellodj bot=registry.celestium.life/hellodj/bot:<prior-tag> -n hellodj-service
kubectl rollout restart deployment/hellodj -n hellodj-service
```

Or undo the last rollout:

```bash
kubectl rollout undo deployment/hellodj -n hellodj-service
```
