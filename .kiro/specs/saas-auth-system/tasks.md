# Implementation Plan: SaaS Auth System

## Overview

Implement three-layer authentication (operator, tenant, delegated access) with Discord OAuth2 as sole identity provider. Phase 1 sets up service layer and database schema. Phase 2 refactors the existing auth blueprint and middleware. Phase 3 adds RBAC enforcement, delegated access management, and tenant context switching. Phase 4 wires everything together and adds security hardening.

## Prerequisites

- Redis 7.x deployed in `redis-service` namespace (from `kube/redis/`)
- PostgreSQL CNPG cluster with `hellodj` database and `tenants` table (from `scripts/migrate_schema.py`)
- Existing `web-ui/blueprints/auth.py` with Discord OAuth2 flow
- Existing `web-ui/auth_middleware.py` with `@login_required` and `@operator_required`
- Existing `web-ui/services/rate_limiter.py` with Redis-backed rate limiting
- `httpx`, `psycopg2-binary`, `redis` packages already in `web-ui/requirements.txt`

## Tasks

- [x] 1. Phase 1: Schema + Service Layer Foundation
  - [x] 1.1 Add `tenant_roles` table to schema migration (`scripts/migrate_schema.py`)
    - Add CREATE TABLE IF NOT EXISTS `tenant_roles` with columns: `tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE`, `discord_user_id BIGINT NOT NULL`, `role TEXT NOT NULL CHECK (role IN ('admin', 'editor', 'viewer'))`, `granted_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `granted_by BIGINT NOT NULL`, `PRIMARY KEY (tenant_id, discord_user_id)`
    - Add to INDEXES_SQL: `CREATE INDEX IF NOT EXISTS idx_tenant_roles_user ON tenant_roles(discord_user_id)` and `CREATE INDEX IF NOT EXISTS idx_tenant_roles_tenant ON tenant_roles(tenant_id)`
    - Verify migration is idempotent (run twice without error)
    - _Requirements: 6.1, 6.3_

  - [x] 1.2 Create `web-ui/services/session_service.py` with SessionService class
    - Define constants: `SESSION_TTL = 86400`, `ABSOLUTE_LIFETIME = 604800`, `COOKIE_NAME = "hellodj_session"`, `KEY_PREFIX = "session:"`
    - Implement `__init__(self, redis_client: redis.Redis)`
    - Implement `create(session_data: dict) -> str` — generate `secrets.token_urlsafe(32)`, store JSON at `session:{token}` with EX=SESSION_TTL, SADD token to `user_sessions:{discord_user_id}`
    - Implement `load(token: str) -> dict | None` — GET key, JSON decode, return None on missing/expired/malformed
    - Implement `extend(token: str)` — EXPIRE key with SESSION_TTL
    - Implement `destroy(token: str)` — DELETE session key, SREM from `user_sessions:{discord_user_id}` SET
    - Implement `update_field(token: str, field: str, value)` — load session, update field, re-SET with remaining TTL
    - Implement `switch_tenant(token: str, tenant_id: str, accessible_tenants: list[str]) -> bool` — validate tenant_id in list, update `active_tenant_id`
    - Implement `invalidate_user_sessions(discord_user_id: str, tenant_id: str | None = None) -> int` — iterate `user_sessions:{discord_user_id}` SET, filter by tenant if provided, delete matching sessions, return count
    - Raise `ServiceUnavailableError` on Redis connection failure
    - _Requirements: 4.1, 4.2, 4.4, 4.5, 4.6, 4.7, 6.7, 10.2, 10.4_

  - [x] 1.3 Create `web-ui/services/tenant_service.py` with TenantService class
    - Implement `__init__(self, pg_uri: str)`
    - Implement `upsert(discord_user_id: int, discord_username: str, email: str | None) -> dict` — INSERT INTO tenants ... ON CONFLICT (discord_user_id) DO UPDATE SET discord_username, email, updated_at; RETURNING id, discord_user_id, discord_username, email, created_at
    - Truncate discord_username to 32 chars, email to 254 chars before INSERT
    - Use single transaction with 5-second statement timeout
    - Implement `get_by_id(tenant_id: str) -> dict | None`
    - Implement `get_by_discord_user_id(discord_user_id: int) -> dict | None`
    - Implement `list_accessible_tenants(discord_user_id: int) -> list[dict]` — query owned tenant UNION ALL tenant_roles entries with joined tenant info
    - Use psycopg2 with connection-per-call and RealDictCursor (matching existing code style)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 10.1_

  - [x] 1.4 Create `web-ui/services/rbac.py` with RBACService class
    - Define `ROLE_HIERARCHY = {"operator": 5, "owner": 4, "admin": 3, "editor": 2, "viewer": 1}`
    - Define `MAX_DELEGATES_PER_TENANT = 20`
    - Implement `__init__(self, pg_uri: str, redis_client: redis.Redis)`
    - Implement `get_user_roles(discord_user_id: int) -> list[dict]` — SELECT tenant_id, role FROM tenant_roles WHERE discord_user_id = $1
    - Implement `grant_role(tenant_id, discord_user_id, role, granted_by)` — validate role in ('admin', 'editor', 'viewer'), SELECT COUNT where tenant_id to check < 20, INSERT ... ON CONFLICT DO UPDATE, call SessionService.invalidate_user_sessions
    - Implement `revoke_role(tenant_id, discord_user_id)` — DELETE FROM tenant_roles, call SessionService.invalidate_user_sessions
    - Implement `check_permission(session: dict, tenant_id: str, required_role: str) -> bool` — get effective role level, compare >= required level
    - Implement `get_effective_role(session: dict, tenant_id: str) -> str | None` — check is_operator, then check if tenant owned, then check roles list for tenant_id match, return None if no access
    - Define `DelegateLimitError` and `InvalidRoleError` exception classes
    - _Requirements: 6.1, 6.2, 6.4, 6.5, 6.6, 6.7, 6.8, 7.1, 7.2, 7.3_

- [x] 2. Phase 2: Auth Blueprint + Middleware Refactor
  - [x] 2.1 Refactor `web-ui/auth_middleware.py` with enhanced session validation
    - Rename `SESSION_COOKIE_NAME` from `"session_token"` to `"hellodj_session"`
    - Add `_get_client_ip()` helper: parse `X-Forwarded-For` header (first entry), fall back to `request.remote_addr`
    - Add IP binding check in `login_required`: compare `session["ip_address"]` with `_get_client_ip()`, on mismatch call `destroy()` and redirect
    - Add absolute expiry check: `time.time() - session["created_at"] > 604800` → destroy + redirect
    - Add sliding expiry: on valid session, call `SessionService.extend(token)`
    - Add `_maybe_refresh_discord_token(token, session)` function: check if `discord_token_expires_at - time.time() < 3600`, acquire distributed lock via `SET session_refresh_lock:{token} 1 NX EX 30`, call Discord refresh endpoint, update session fields, handle `invalid_grant` (destroy session) and network errors (increment retry counter, destroy after 3 consecutive failures)
    - Update `g` context: set `g.session` (full dict), `g.tenant_id` (active_tenant_id), `g.is_operator` (bool)
    - Return HTTP 503 JSON error when Redis raises `ConnectionError` during validation
    - Update `operator_required` to read from `g.session` instead of `g.tenant`
    - _Requirements: 4.3, 4.4, 4.5, 4.6, 4.7, 5.1, 9.1, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6_

  - [x] 2.2 Refactor `web-ui/blueprints/auth.py` login flow
    - Update OAuth2 scope from `"identify email"` to `"identify email guilds"`
    - Increase state entropy: `secrets.token_urlsafe(32)` (was 16)
    - Add `_check_login_rate_limit(ip)` — INCR `ratelimit:login:{ip}`, EXPIRE 300 on first hit, reject if > 10, return (allowed, retry_after)
    - Call rate limit check before generating state in `/auth/login`, return 429 with `Retry-After` header on exceeded
    - Handle Redis unavailability in login: return error page "Login temporarily unavailable" (not redirect to Discord)
    - In callback: store Discord tokens in session (`discord_access_token`, `discord_refresh_token`, `discord_token_expires_at = time.time() + expires_in`)
    - In callback: call `TenantService.upsert()` instead of inline SQL
    - In callback: call `TenantService.list_accessible_tenants()` to build roles list
    - In callback: add `{"tenant_id": tenant["id"], "role": "owner"}` to roles for owned tenant
    - In callback: set `is_operator = str(discord_user_id) == os.environ.get("OPERATOR_DISCORD_ID", "")`
    - In callback: store `ip_address` from `_get_client_ip()`
    - In callback: store `created_at = time.time()`
    - In callback: set `active_tenant_id` to owned tenant ID (or first accessible)
    - In callback: use `SessionService.create()` instead of inline Redis SET
    - Rename cookie to `hellodj_session`, set `Path=/`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.1, 3.2, 4.1, 4.2, 4.3, 5.1, 9.2, 9.3, 10.3_

  - [x] 2.3 Refactor auth blueprint logout and add token revocation
    - Add Discord token revocation: POST to `https://discord.com/api/oauth2/token/revoke` with `token={access_token}&token_type_hint=access_token&client_id=...&client_secret=...`, timeout 5s, best-effort (log failure, proceed)
    - Use `SessionService.destroy()` instead of inline Redis DELETE
    - Clear cookie with `response.set_cookie("hellodj_session", "", max_age=0, path="/", httponly=True, secure=True, samesite="Lax")`
    - Handle unauthenticated logout request (no cookie or expired): redirect to login without cleanup
    - Support both POST and GET for `/auth/logout` (GET for link-based logout)
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6_

- [x] 3. Phase 3: RBAC Enforcement + Delegate Management
  - [x] 3.1 Add `role_required` decorator to `web-ui/auth_middleware.py`
    - Implement `role_required(minimum_role: str)` decorator factory
    - Inner decorator calls `@login_required` first
    - Extract `tenant_id` from route kwargs (URL path param) or `g.tenant_id`
    - Call `RBACService.get_effective_role(g.session, tenant_id)` to determine user's role
    - If effective role is None → abort(404) (tenant enumeration prevention)
    - If effective role level < required role level → abort(403) with message indicating required role
    - Set `g.effective_role` on success for downstream use
    - Add `get_rbac_service()` lazy singleton factory function
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

  - [x] 3.2 Create `web-ui/blueprints/delegates.py` with delegate management endpoints
    - Create Blueprint `delegates_bp` with `url_prefix="/api/v1/tenants"`
    - `GET /<tenant_id>/delegates` — `@role_required("owner")`, query tenant_roles for tenant, return JSON list of `{discord_user_id, role, granted_at, granted_by}`
    - `POST /<tenant_id>/delegates` — `@role_required("owner")`, accept body `{"discord_user_id": int, "role": "admin|editor|viewer"}`, validate input, call `RBACService.grant_role()`, return 201 on success
    - `DELETE /<tenant_id>/delegates/<int:discord_user_id>` — `@role_required("owner")`, call `RBACService.revoke_role()`, return 204 on success
    - `PATCH /<tenant_id>/delegates/<int:discord_user_id>` — `@role_required("owner")`, accept body `{"role": "..."}`, call `grant_role()` (UPSERT), return 200
    - Handle `DelegateLimitError` → 400 JSON `{"error": "Maximum delegate limit of 20 reached"}`
    - Handle `InvalidRoleError` → 400 JSON `{"error": "Invalid role. Must be admin, editor, or viewer"}`
    - _Requirements: 6.1, 6.2, 6.4, 6.5, 6.6, 6.7, 6.8_

  - [x] 3.3 Add tenant context switching endpoint
    - Add `POST /api/v1/session/tenant` route (in auth blueprint or new session blueprint)
    - Decorate with `@login_required`
    - Accept JSON body `{"tenant_id": "uuid-string"}`
    - Validate input (require valid UUID format)
    - Check `tenant_id` exists in session's `roles` array (accessible tenants)
    - Call `SessionService.switch_tenant(token, tenant_id, accessible_list)`
    - Return 200 with `{"active_tenant_id": "...", "role": "..."}`
    - Return 403 if tenant_id not in accessible list: `{"error": "You do not have access to this tenant"}`
    - _Requirements: 10.2, 10.3, 10.4, 10.5_

- [x] 4. Phase 4: Integration + Security Hardening
  - [x] 4.1 Add security headers via Flask `after_request` handler
    - Add `@app.after_request` in `app.py` that sets `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Strict-Transport-Security: max-age=31536000; includeSubDomains`
    - Ensure headers don't override existing Content-Type or CORS headers
    - Verify headers appear on error responses (404, 500) as well
    - _Requirements: 9.4_

  - [x] 4.2 Wire services into Flask app initialization
    - Create `get_session_service()`, `get_tenant_service()`, `get_rbac_service()` factory functions in `web-ui/services/__init__.py` or a dedicated `web-ui/services/factories.py`
    - Use lazy singleton pattern (matching existing `_get_redis()` in rate_limiter.py)
    - Read `REDIS_URL` and `HELLODJ_PG_URI` from environment
    - Register `delegates_bp` blueprint in `app.py`
    - Log startup warning if `OPERATOR_DISCORD_ID` is unset or empty
    - _Requirements: 5.5_

  - [x] 4.3 Update web-ui deployment manifest with new env vars
    - Add `HELLODJ_PG_URI` from Secret `hellodj-pg-uri` key `HELLODJ_PG_URI` to web-ui container env
    - Add `REDIS_URL` with value `redis://redis.redis-service.svc.cluster.local:6379/0`
    - Add `OPERATOR_DISCORD_ID` with celes's Discord user ID
    - Verify `DISCORD_CLIENT_SECRET` is already available (from `hellodj-secret`)
    - Verify `DISCORD_APPID` can serve as `DISCORD_CLIENT_ID` (update auth.py to read from `DISCORD_APPID` if `DISCORD_CLIENT_ID` not set)
    - _Requirements: 5.1, 5.5_

  - [x] 4.4 Migrate existing routes to new auth decorators
    - Audit all routes in `app.py` using `require_auth` or `require_owner`
    - Replace `@require_auth` with `@login_required` on appropriate routes
    - Replace `@require_owner` with `@operator_required` on admin routes
    - Add `@role_required("editor")` to write routes (config update, playlist CRUD, ban CRUD, filter CRUD)
    - Add `@role_required("viewer")` to read-only routes (config get, playlist list, ban list)
    - Update route handlers to use `g.session` and `g.tenant_id` instead of old `g.tenant` or inline session lookups
    - Verify no regressions in existing functionality
    - _Requirements: 7.1, 7.5, 7.6_

  - [x] 4.5 End-to-end validation of complete auth flow
    - Verify login: GET /auth/login → Discord OAuth2 → callback → session in Redis → cookie set → dashboard redirect
    - Verify session validation: valid cookie loads session, extends TTL, populates g.session
    - Verify IP binding: different IP invalidates session
    - Verify absolute expiry: session > 7 days old is rejected
    - Verify rate limiting: 11th login attempt from same IP returns 429
    - Verify logout: session deleted, cookie cleared, Discord token revoked
    - Verify tenant switching: valid switch updates active_tenant_id, invalid switch returns 403
    - Verify delegate CRUD: grant/revoke/update roles work, session invalidation fires
    - Verify role enforcement: viewer can't POST, editor can't manage delegates, stranger gets 404
    - Verify operator: can access /api/v1/admin/* and any tenant's resources
    - Verify token refresh: expired Discord token triggers refresh with lock
    - _Requirements: All_

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4"] },
    { "id": 2, "tasks": ["2.1", "2.2"] },
    { "id": 3, "tasks": ["2.3", "3.1"] },
    { "id": 4, "tasks": ["3.2", "3.3"] },
    { "id": 5, "tasks": ["4.1", "4.2"] },
    { "id": 6, "tasks": ["4.3", "4.4"] },
    { "id": 7, "tasks": ["4.5"] }
  ]
}
```

## Notes

- The cookie rename from `session_token` to `hellodj_session` will invalidate all existing sessions on deploy. This is acceptable for a one-time SaaS migration.
- The existing `app.py` `require_auth`/`require_owner` decorators are left in place during migration (task 4.4) and can be removed once all routes are migrated.
- Discord `DISCORD_APPID` already in `hellodj-secret` can double as `DISCORD_CLIENT_ID` — auth.py should fall back to `DISCORD_APPID` if `DISCORD_CLIENT_ID` is not set.
- Rate limiting uses a fixed-window counter (INCR + EXPIRE) rather than the sliding-window sorted set from `rate_limiter.py` — simpler and sufficient for login attempts.
