# Technical Design — SaaS Auth System

## Overview

This design implements a three-layer authentication and authorization system for the HelloDJ SaaS platform:

1. **Operator layer** — Platform superuser (identified by Discord user ID)
2. **Tenant layer** — Subscriber accounts auto-created on first Discord OAuth2 login
3. **Delegated access layer** — Tenant owners grant Discord users roles within their tenant

Discord OAuth2 is the sole identity provider. Sessions are stored in Redis. Tenant and RBAC data live in PostgreSQL.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Browser                                                                  │
│  ┌─────────────┐    ┌─────────────────────────────┐                     │
│  │ Login Page  │───▶│ Discord OAuth2 Authorization │                     │
│  └─────────────┘    └──────────────┬──────────────┘                     │
│                                     │ code + state                       │
│  ┌──────────────────────────────────▼──────────────────────────────┐    │
│  │  hellodj.celestium.life (Flask Web UI)                          │    │
│  │                                                                  │    │
│  │  ┌────────────────┐   ┌───────────────────┐   ┌─────────────┐ │    │
│  │  │ auth blueprint │   │ session_middleware │   │ RBAC decorat│ │    │
│  │  │ /auth/login    │   │ @login_required   │   │ @role_req() │ │    │
│  │  │ /auth/callback │   │ load → validate → │   │ hierarchy   │ │    │
│  │  │ /auth/logout   │   │ extend TTL        │   │ check       │ │    │
│  │  │ /auth/me       │   └────────┬──────────┘   └──────┬──────┘ │    │
│  │  └───────┬────────┘            │                       │        │    │
│  │          │                      │                       │        │    │
│  │          ▼                      ▼                       ▼        │    │
│  │  ┌──────────────────────────────────────────────────────────┐   │    │
│  │  │                     Service Layer                        │   │    │
│  │  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │   │    │
│  │  │  │ TenantService│  │ SessionService│  │ RBACService  │  │   │    │
│  │  │  │ upsert()     │  │ create()     │  │ grant_role() │  │   │    │
│  │  │  │ get_by_id()  │  │ validate()   │  │ revoke_role()│  │   │    │
│  │  │  │ get_by_did() │  │ extend()     │  │ list_roles() │  │   │    │
│  │  │  └──────┬───────┘  │ destroy()    │  │ check_perm() │  │   │    │
│  │  │         │           └──────┬───────┘  └──────┬───────┘  │   │    │
│  │  └─────────┼──────────────────┼─────────────────┼──────────┘   │    │
│  │            │                   │                  │              │    │
│  │            ▼                   ▼                  ▼              │    │
│  │  ┌─────────────────┐  ┌─────────────────┐  ┌───────────────┐  │    │
│  │  │   PostgreSQL    │  │     Redis       │  │  PostgreSQL   │  │    │
│  │  │   tenants       │  │  session:{tok}  │  │  tenant_roles │  │    │
│  │  │   CNPG cluster  │  │  oauth_state:*  │  │  CNPG cluster │  │    │
│  │  │                 │  │  ratelimit:*    │  │               │  │    │
│  │  └─────────────────┘  └─────────────────┘  └───────────────┘  │    │
│  └──────────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────────┘
```

## Data Models

### PostgreSQL: `tenants` table (existing)

```sql
CREATE TABLE IF NOT EXISTS tenants (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    discord_user_id   BIGINT UNIQUE NOT NULL,
    discord_username  TEXT,
    email             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### PostgreSQL: `tenant_roles` table (new)

```sql
CREATE TABLE IF NOT EXISTS tenant_roles (
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    discord_user_id BIGINT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('admin', 'editor', 'viewer')),
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_by      BIGINT NOT NULL,
    PRIMARY KEY (tenant_id, discord_user_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_roles_user ON tenant_roles(discord_user_id);
```

### Redis: Session Record

Key: `session:{token}` (token = `secrets.token_urlsafe(32)`)
TTL: 86400 seconds (24h sliding), absolute max 604800 seconds (7 days)

```json
{
    "tenant_id": "uuid-string",
    "discord_user_id": "123456789",
    "discord_username": "celes",
    "email": "celes@example.com",
    "avatar": "hash_or_null",
    "is_operator": true,
    "roles": [
        {"tenant_id": "uuid-1", "role": "owner"},
        {"tenant_id": "uuid-2", "role": "admin"}
    ],
    "active_tenant_id": "uuid-1",
    "ip_address": "1.2.3.4",
    "created_at": 1724500000,
    "discord_access_token": "...",
    "discord_refresh_token": "...",
    "discord_token_expires_at": 1725100000,
    "refresh_retry_count": 0
}
```

### Redis: OAuth State

Key: `oauth_state:{state}` (state = `secrets.token_urlsafe(32)`)
TTL: 300 seconds
Value: `"1"` (presence-only)

### Redis: Rate Limit (Login)

Key: `ratelimit:login:{ip}`
TTL: 300 seconds
Value: integer counter (INCR)

## Components and Interfaces

### 1. Auth Blueprint (`web-ui/blueprints/auth.py`) — REFACTOR

The existing `auth.py` blueprint already handles the OAuth2 flow. Changes needed:

- **Rename session cookie** from `session_token` to `hellodj_session`
- **Increase state entropy** from 16 bytes to 32 bytes (`secrets.token_urlsafe(32)`)
- **Add `guilds` scope** to OAuth2 request (currently only `identify email`)
- **Store Discord tokens** in session (access_token, refresh_token, expires_at)
- **Build roles list** on login by querying `tenant_roles` for the user's `discord_user_id`
- **Store IP address** in session data for IP binding
- **Store `created_at`** timestamp for absolute session expiry
- **Add Discord token revocation** on logout
- **Add `/api/v1/session/tenant` endpoint** for tenant context switching

#### Login Flow (sequence)

```
1. GET /auth/login
   → Rate limit check (10/5min per IP)
   → Generate state (32 bytes)
   → Store in Redis (TTL 300s)
   → 302 → Discord OAuth2 authorize URL

2. GET /auth/callback?code=...&state=...
   → Validate state (Redis GET + DELETE — one-time use)
   → POST Discord token endpoint (10s timeout)
   → GET Discord /users/@me (10s timeout)
   → UPSERT tenant in PostgreSQL (INSERT ... ON CONFLICT)
   → Query tenant_roles for this discord_user_id
   → Check if discord_user_id == OPERATOR_DISCORD_ID
   → Build session data (roles, tokens, IP, timestamps)
   → Store in Redis (TTL 86400s)
   → Set cookie: hellodj_session (HttpOnly, Secure, SameSite=Lax, Path=/)
   → 302 → /dashboard (or `next` URL if stored)

3. POST /auth/logout
   → Read cookie
   → Revoke Discord token (POST /oauth2/token/revoke, 5s timeout, best-effort)
   → DELETE session from Redis
   → Clear cookie (Max-Age=0)
   → 302 → /

4. GET /auth/me
   → Read session from Redis
   → Return JSON profile (tenant_id, username, roles, active_tenant)
```

### 2. Session Middleware (`web-ui/auth_middleware.py`) — REFACTOR

The existing middleware has the right shape but needs enhancement:

- **Rename** `SESSION_COOKIE_NAME` to `"hellodj_session"`
- **Add IP binding** — compare `request.headers.get("X-Forwarded-For", request.remote_addr)` against stored IP
- **Add absolute expiry** — check `created_at + 604800 < now()` to force re-auth
- **Add sliding expiry** — on successful validation, `EXPIRE session:{token} 86400`
- **Add Discord token refresh** — if `discord_token_expires_at - now() < 3600`, trigger refresh
- **Add refresh locking** — Redis `SET session_refresh_lock:{token} 1 NX EX 30` to prevent concurrent refreshes
- **Return HTTP 503** when Redis is unavailable (not silent failure)
- **Attach full session** to `g.session` (not just tenant dict)

#### Updated `@login_required` decorator flow

```python
def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        token = request.cookies.get("hellodj_session")
        if not token:
            return _redirect_to_login()

        session = _load_session(token)
        if session is None:
            return _clear_cookie_and_redirect()

        # IP binding check
        client_ip = _get_client_ip()
        if session.get("ip_address") != client_ip:
            _invalidate_session(token)
            return _clear_cookie_and_redirect()

        # Absolute expiry check (7 days)
        if time.time() - session.get("created_at", 0) > 604800:
            _invalidate_session(token)
            return _clear_cookie_and_redirect()

        # Sliding expiry extension
        _extend_session(token, ttl=86400)

        # Discord token refresh (if < 1h until expiry)
        _maybe_refresh_discord_token(token, session)

        g.session = session
        g.tenant_id = session.get("active_tenant_id")
        g.is_operator = session.get("is_operator", False)
        return f(*args, **kwargs)
    return wrapper
```

### 3. RBAC Service (`web-ui/services/rbac.py`) — NEW

Handles role management and permission checks.

```python
class RBACService:
    """Role-based access control for tenant resources."""

    ROLE_HIERARCHY = {
        "operator": 5,
        "owner": 4,
        "admin": 3,
        "editor": 2,
        "viewer": 1,
    }

    MAX_DELEGATES_PER_TENANT = 20

    def __init__(self, pg_uri: str, redis_client: redis.Redis):
        self._pg_uri = pg_uri
        self._redis = redis_client

    def get_user_roles(self, discord_user_id: int) -> list[dict]:
        """Query all tenant_roles for a given Discord user.

        Returns: [{"tenant_id": "uuid", "role": "admin"}, ...]
        """

    def grant_role(self, tenant_id: str, discord_user_id: int,
                   role: str, granted_by: int) -> None:
        """Assign a role to a Discord user for a tenant.

        - Validates role is in (admin, editor, viewer)
        - Checks delegate count < 20
        - UPSERTs into tenant_roles
        - Invalidates affected user's active sessions
        """

    def revoke_role(self, tenant_id: str, discord_user_id: int) -> None:
        """Remove a user's role for a tenant.

        - DELETE from tenant_roles
        - Invalidates affected user's active sessions
        """

    def check_permission(self, session: dict, tenant_id: str,
                         required_role: str) -> bool:
        """Check if the session has the required role for a tenant.

        Uses role hierarchy: operator > owner > admin > editor > viewer.
        Returns True if the user's effective role >= required_role.
        """

    def get_effective_role(self, session: dict, tenant_id: str) -> str | None:
        """Get the user's effective role for a specific tenant.

        Priority:
        1. is_operator → "operator"
        2. tenant.discord_user_id == session.discord_user_id → "owner"
        3. tenant_roles entry → the assigned role
        4. None (no access)
        """
```

### 4. RBAC Decorator (`web-ui/auth_middleware.py`) — NEW

```python
def role_required(minimum_role: str):
    """Decorator factory that enforces a minimum role for the active tenant.

    Usage:
        @role_required("editor")
        def update_settings(): ...

        @role_required("owner")
        def manage_delegates(): ...
    """
    def decorator(f):
        @functools.wraps(f)
        @login_required
        def wrapper(*args, **kwargs):
            tenant_id = kwargs.get("tenant_id") or g.tenant_id
            if not tenant_id:
                abort(400, description="No active tenant context")

            rbac = get_rbac_service()
            if not rbac.check_permission(g.session, tenant_id, minimum_role):
                # Return 404 for tenant isolation (prevent enumeration)
                # unless user has *some* relationship to the tenant
                effective = rbac.get_effective_role(g.session, tenant_id)
                if effective is None:
                    abort(404)
                else:
                    abort(403, description=f"Requires {minimum_role} role")

            g.effective_role = effective
            return f(*args, **kwargs)
        return wrapper
    return decorator
```

### 5. Tenant Service (`web-ui/services/tenant_service.py`) — NEW

```python
class TenantService:
    """Manages tenant CRUD operations."""

    def __init__(self, pg_uri: str):
        self._pg_uri = pg_uri

    def upsert(self, discord_user_id: int, discord_username: str,
               email: str | None) -> dict:
        """UPSERT tenant on login. Returns tenant dict with id."""

    def get_by_id(self, tenant_id: str) -> dict | None:
        """Fetch tenant by UUID."""

    def get_by_discord_user_id(self, discord_user_id: int) -> dict | None:
        """Fetch tenant by Discord user ID."""

    def list_accessible_tenants(self, discord_user_id: int) -> list[dict]:
        """Get all tenants a user can access (owned + delegated).

        Returns: [{"tenant_id": "...", "role": "owner|admin|editor|viewer",
                   "discord_username": "...", "created_at": "..."}, ...]
        """
```

### 6. Session Service (`web-ui/services/session_service.py`) — NEW

Encapsulates all Redis session operations.

```python
class SessionService:
    """Manages session CRUD in Redis."""

    SESSION_TTL = 86400           # 24h sliding
    ABSOLUTE_LIFETIME = 604800    # 7 days hard max
    COOKIE_NAME = "hellodj_session"
    KEY_PREFIX = "session:"

    def __init__(self, redis_client: redis.Redis):
        self._redis = redis_client

    def create(self, session_data: dict) -> str:
        """Create a new session. Returns the token string."""

    def load(self, token: str) -> dict | None:
        """Load session by token. Returns None if expired/missing."""

    def extend(self, token: str) -> None:
        """Reset sliding TTL to SESSION_TTL."""

    def destroy(self, token: str) -> None:
        """Delete session from Redis."""

    def update_field(self, token: str, field: str, value) -> None:
        """Update a single field in the session JSON."""

    def switch_tenant(self, token: str, tenant_id: str,
                      accessible_tenants: list[str]) -> bool:
        """Switch active tenant context. Returns False if not in access list."""

    def invalidate_user_sessions(self, discord_user_id: str,
                                 tenant_id: str | None = None) -> int:
        """Invalidate all sessions for a user (optionally scoped to a tenant).

        Implementation: Scan Redis for session:* keys, deserialize,
        match discord_user_id (and optionally active_tenant_id), delete.
        Returns count of invalidated sessions.

        Note: For high session volumes, consider a secondary index:
        `user_sessions:{discord_user_id}` → SET of session tokens.
        """
```

### 7. Discord Token Refresh Logic

Integrated into the session middleware's `_maybe_refresh_discord_token()`:

```python
def _maybe_refresh_discord_token(token: str, session: dict) -> None:
    """Refresh Discord access token if within 1 hour of expiry."""
    expires_at = session.get("discord_token_expires_at", 0)
    if time.time() < expires_at - 3600:
        return  # Not yet due for refresh

    # Acquire distributed lock (prevent concurrent refreshes)
    lock_key = f"session_refresh_lock:{token}"
    r = _get_redis()
    acquired = r.set(lock_key, "1", nx=True, ex=30)
    if not acquired:
        return  # Another request is handling the refresh

    try:
        new_tokens = _call_discord_refresh(session["discord_refresh_token"])
        # Update session in Redis
        session["discord_access_token"] = new_tokens["access_token"]
        session["discord_refresh_token"] = new_tokens.get(
            "refresh_token", session["discord_refresh_token"]
        )
        session["discord_token_expires_at"] = (
            time.time() + new_tokens["expires_in"]
        )
        session["refresh_retry_count"] = 0
        _save_session(token, session)
    except InvalidGrantError:
        _invalidate_session(token)
        # Will trigger re-auth on next request
    except NetworkError:
        session["refresh_retry_count"] = session.get("refresh_retry_count", 0) + 1
        if session["refresh_retry_count"] >= 3:
            _invalidate_session(token)
        else:
            _save_session(token, session)
    finally:
        r.delete(lock_key)
```

### 8. Rate Limiting for Login

Simple counter-based (not sliding window — fixed window is sufficient for login):

```python
def _check_login_rate_limit(ip: str) -> tuple[bool, int]:
    """Check if IP has exceeded 10 login attempts in 5 minutes.

    Returns (allowed, retry_after_seconds).
    """
    r = _get_redis()
    key = f"ratelimit:login:{ip}"
    count = r.incr(key)
    if count == 1:
        r.expire(key, 300)  # 5 minute window

    if count > 10:
        ttl = r.ttl(key)
        return False, max(ttl, 1)

    return True, 0
```

### 9. Security Headers (Flask after_request)

```python
@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = (
        "max-age=31536000; includeSubDomains"
    )
    return response
```

### 10. Session Invalidation on Role Change

When a tenant owner grants/revokes a role, affected sessions must be invalidated:

```python
# In RBACService.grant_role() and revoke_role():
def _invalidate_user_tenant_sessions(self, discord_user_id: int, tenant_id: str):
    """Invalidate sessions where this user has the affected tenant active.

    Strategy: Maintain a Redis SET `user_sessions:{discord_user_id}` containing
    all session tokens for that user. On role change, iterate the set,
    load each session, check if it references the affected tenant,
    and delete if so.
    """
    user_sessions_key = f"user_sessions:{discord_user_id}"
    tokens = self._redis.smembers(user_sessions_key)
    for token in tokens:
        session_data = self._redis.get(f"session:{token}")
        if not session_data:
            self._redis.srem(user_sessions_key, token)
            continue
        session = json.loads(session_data)
        # Check if this session references the affected tenant
        if any(r["tenant_id"] == tenant_id for r in session.get("roles", [])):
            self._redis.delete(f"session:{token}")
            self._redis.srem(user_sessions_key, token)
```

## API Endpoints Summary

### Auth Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/auth/login` | None | Initiate Discord OAuth2 |
| GET | `/auth/callback` | None | OAuth2 callback handler |
| POST | `/auth/logout` | Session | Destroy session + revoke token |
| GET | `/auth/me` | Session | Current user profile + roles |

### Session Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/session/tenant` | Session | Switch active tenant context |

### Delegate Management Endpoints

| Method | Path | Auth | Min Role | Description |
|--------|------|------|----------|-------------|
| GET | `/api/v1/tenants/{id}/delegates` | Session | owner | List delegated users |
| POST | `/api/v1/tenants/{id}/delegates` | Session | owner | Grant role to Discord user |
| DELETE | `/api/v1/tenants/{id}/delegates/{discord_user_id}` | Session | owner | Revoke delegated access |
| PATCH | `/api/v1/tenants/{id}/delegates/{discord_user_id}` | Session | owner | Update role |

### Admin Endpoints (Operator Only)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/v1/admin/tenants` | Operator | List all tenants |
| GET | `/api/v1/admin/tenants/{id}` | Operator | Tenant detail |
| GET | `/api/v1/admin/metrics` | Operator | Platform metrics |
| POST | `/api/v1/admin/trials/{id}/approve` | Operator | Approve trial application |
| POST | `/api/v1/admin/trials/{id}/reject` | Operator | Reject trial application |

## Endpoint-to-Role Mapping

| Endpoint Pattern | Min Role | Operations |
|-----------------|----------|------------|
| `GET /api/v1/tenants/{id}/settings` | viewer | Read bot config |
| `PUT /api/v1/tenants/{id}/settings` | editor | Update bot config |
| `GET /api/v1/tenants/{id}/playlists` | viewer | List playlists |
| `POST /api/v1/tenants/{id}/playlists` | editor | Create playlist |
| `DELETE /api/v1/tenants/{id}/playlists/{pid}` | editor | Delete playlist |
| `GET /api/v1/tenants/{id}/bans` | viewer | List bans |
| `POST /api/v1/tenants/{id}/bans` | editor | Add ban |
| `DELETE /api/v1/tenants/{id}/bans/{uid}` | editor | Remove ban |
| `POST /api/v1/tenants/{id}/player/*` | admin | Playback controls |
| `GET /api/v1/tenants/{id}/delegates` | owner | List delegates |
| `POST /api/v1/tenants/{id}/delegates` | owner | Add delegate |
| `DELETE /api/v1/tenants/{id}/delegates/*` | owner | Remove delegate |
| `DELETE /api/v1/tenants/{id}` | owner | Delete tenant |
| `/api/v1/admin/*` | operator | All admin operations |

## File Structure (New/Modified)

```
web-ui/
├── auth_middleware.py              ← REFACTOR: rename cookie, add IP binding,
│                                     absolute expiry, sliding expiry, refresh,
│                                     role_required decorator
├── blueprints/
│   ├── auth.py                     ← REFACTOR: 32-byte state, guilds scope,
│   │                                  store Discord tokens in session, build
│   │                                  roles list, IP in session, rate limit
│   └── delegates.py                ← NEW: delegate management endpoints
├── services/
│   ├── rbac.py                     ← NEW: RBACService class
│   ├── tenant_service.py           ← NEW: TenantService class
│   └── session_service.py          ← NEW: SessionService class
└── ...
```

## Database Migration

Add to `scripts/migrate_schema.py` TABLES_SQL:

```sql
-- RBAC: Delegated tenant roles
CREATE TABLE IF NOT EXISTS tenant_roles (
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    discord_user_id BIGINT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('admin', 'editor', 'viewer')),
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    granted_by      BIGINT NOT NULL,
    PRIMARY KEY (tenant_id, discord_user_id)
);
```

Add to INDEXES_SQL:

```sql
CREATE INDEX IF NOT EXISTS idx_tenant_roles_user ON tenant_roles(discord_user_id);
CREATE INDEX IF NOT EXISTS idx_tenant_roles_tenant ON tenant_roles(tenant_id);
```

## Session Data Flow (Login)

```
Discord OAuth2 Complete
        │
        ▼
┌─────────────────────────────────────────────────────────────────┐
│ 1. UPSERT tenant (PostgreSQL)                                    │
│    INSERT INTO tenants ... ON CONFLICT (discord_user_id) UPDATE   │
│    → Returns: {id, discord_user_id, discord_username, email}     │
├─────────────────────────────────────────────────────────────────┤
│ 2. Query delegated roles (PostgreSQL)                            │
│    SELECT tenant_id, role FROM tenant_roles                      │
│    WHERE discord_user_id = ?                                     │
│    → Returns: [{tenant_id, role}, ...]                           │
├─────────────────────────────────────────────────────────────────┤
│ 3. Build roles list                                              │
│    roles = [{"tenant_id": own_tenant.id, "role": "owner"}]       │
│    roles += delegated_roles                                      │
├─────────────────────────────────────────────────────────────────┤
│ 4. Check operator status                                         │
│    is_operator = str(discord_user_id) == OPERATOR_DISCORD_ID     │
├─────────────────────────────────────────────────────────────────┤
│ 5. Create session (Redis)                                        │
│    token = secrets.token_urlsafe(32)                             │
│    SET session:{token} → JSON{...} EX 86400                      │
│    SADD user_sessions:{discord_user_id} {token}                  │
├─────────────────────────────────────────────────────────────────┤
│ 6. Set cookie                                                    │
│    hellodj_session={token}; HttpOnly; Secure; SameSite=Lax; /    │
└─────────────────────────────────────────────────────────────────┘
```

## Security Considerations

1. **CSRF Protection**: OAuth2 state parameter (32 bytes, one-time use, 5min TTL)
2. **Session Hijacking**: IP binding — session invalidated if IP changes
3. **Session Fixation**: New token generated on every login (never reuse)
4. **Token Exposure**: Cookie is HttpOnly + Secure + SameSite=Lax (no JS access, no cross-site send)
5. **Tenant Isolation**: 404 response for unauthorized tenant access (prevents enumeration)
6. **Rate Limiting**: 10 login attempts per IP per 5 minutes (fixed window counter)
7. **Absolute Expiry**: 7-day hard cap prevents indefinite session persistence
8. **Discord Token Security**: Tokens stored in Redis (ephemeral), revoked on logout
9. **Concurrent Refresh**: Distributed lock prevents race condition on token refresh

## Dependencies (web-ui/requirements.txt additions)

```
httpx>=0.27.0       # Already present (Discord API calls)
psycopg2-binary     # Already present (PostgreSQL)
redis>=5.0          # Already present (session store)
```

No new dependencies required.

## Environment Variables (web-ui container)

| Variable | Source | Current Status |
|----------|--------|----------------|
| `DISCORD_CLIENT_ID` | Secret `hellodj-discord-oauth` | NEW — needs secret creation |
| `DISCORD_CLIENT_SECRET` | Secret `hellodj-discord-oauth` | NEW — needs secret creation |
| `HELLODJ_PG_URI` | Secret `hellodj-pg-uri` | NEW — needs secret creation |
| `REDIS_URL` | Value | NEW — `redis://redis.redis-service.svc.cluster.local:6379/0` |
| `OPERATOR_DISCORD_ID` | ConfigMap or value | NEW — celes's Discord user ID |
| `DISCORD_REDIRECT_URI` | Value | Already in auth.py (defaults correctly) |

## Compatibility Notes

- The existing `auth_middleware.py` uses cookie name `session_token` and key prefix `session:`. The refactored version changes to `hellodj_session`. During rollout, both old and new cookie names should be checked for a brief transition window (or accept that existing sessions are invalidated — acceptable for a one-time deploy).
- The existing `app.py` has its own auth decorators (`require_auth`, `require_owner`). These will be deprecated in favor of `@login_required` and `@role_required`. Migration can happen incrementally per-route.
- The existing `blueprints/auth.py` already implements the full OAuth2 flow with UPSERT. The refactor adds roles, IP binding, and token storage — not a rewrite, an enhancement.

## Error Handling

| Scenario | Behavior | HTTP Code |
|----------|----------|-----------|
| Redis unavailable during login initiation | Return error page "Login temporarily unavailable" | 503 |
| Redis unavailable during session validation | Return JSON error "Service temporarily unavailable" | 503 |
| Discord token exchange fails (timeout/5xx) | Redirect to login with `error=service_unavailable` | 302 |
| Discord token exchange returns non-200 | Redirect to login with `error=service_unavailable`, log response | 302 |
| Discord profile fetch fails | Same as token exchange failure | 302 |
| State parameter mismatch/expired | Redirect to login with `error=state_mismatch` | 302 |
| PostgreSQL tenant UPSERT fails | Redirect to login with `error=service_unavailable`, log exception | 302 |
| Discord token refresh — `invalid_grant` | Invalidate session, force re-auth on next request | 401 |
| Discord token refresh — network error | Continue with cached session, increment retry counter | (pass-through) |
| Discord token refresh — 3 consecutive failures | Invalidate session, force re-auth | 401 |
| Rate limit exceeded (login) | Return error with Retry-After header | 429 |
| IP binding mismatch | Invalidate session, clear cookie | 401 |
| Absolute session expiry (7 days) | Invalidate session, clear cookie, redirect to login | 302 |
| Delegate count exceeds 20 | Return JSON error "Maximum delegate limit reached" | 400 |
| Role assignment for non-existent tenant | Return 404 | 404 |
| Discord token revocation fails on logout | Proceed with local cleanup, log failure | (best-effort) |

All errors are logged with structured context (discord_user_id, tenant_id, error type, HTTP status from upstream).

## Correctness Properties

### Property 1: No Duplicate Tenants
The UNIQUE constraint on `tenants.discord_user_id` combined with `INSERT ... ON CONFLICT` guarantees at most one tenant per Discord account, even under concurrent login attempts.

**Validates: Requirements 3.3**

### Property 2: State One-Time-Use
The OAuth2 state is consumed (deleted from Redis) immediately upon validation in the callback. Replay of the same callback URL will fail state validation.

**Validates: Requirements 2.1**

### Property 3: Session Token Uniqueness
`secrets.token_urlsafe(32)` produces 256 bits of entropy. Collision probability is negligible (birthday bound ~2^128 attempts).

**Validates: Requirements 4.1**

### Property 4: Role Hierarchy Total Order
`operator(5) > owner(4) > admin(3) > editor(2) > viewer(1)`. Permission check is a simple integer comparison — no ambiguity.

**Validates: Requirements 7.1**

### Property 5: Tenant Isolation
Every tenant-scoped endpoint reads `g.tenant_id` from the session's `active_tenant_id`. The RBAC decorator verifies the user has a role for that tenant before allowing access. Users with no relationship see HTTP 404.

**Validates: Requirements 7.3, 7.4**

### Property 6: Session Invalidation Bounded
The `user_sessions:{discord_user_id}` secondary index ensures session scan is O(n) in the user's sessions (typically <5), not O(total sessions).

**Validates: Requirements 6.7**

### Property 7: Concurrent Refresh Safety
Redis `SET NX EX 30` lock ensures exactly one token refresh per session per 30-second window. If the lock holder crashes, the lock auto-expires and the next request retries.

**Validates: Requirements 11.3**

### Property 8: IP Binding Consistency
IP is captured once at session creation and never updated. If the user's IP changes (e.g., mobile roaming), they must re-authenticate — this is the intended security trade-off.

**Validates: Requirements 9.1**

## Testing Strategy

- **Unit tests**: Mock Redis (fakeredis) and PostgreSQL (test container or mock) for each service class
- **Integration tests**: Full OAuth2 flow using Discord's test application credentials
- **Security tests**: Verify IP binding, rate limiting, tenant isolation, cookie attributes
- **Load tests**: Verify session creation/validation under concurrent requests (target: <10ms p99 for session validation)
