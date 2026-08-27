# Requirements Document

## Introduction

The HelloDJ SaaS Auth System provides a three-layer authentication and authorization mechanism for the HelloDJ multi-tenant platform. Discord OAuth2 serves as the sole identity provider. The system manages operator (admin) access, tenant lifecycle (auto-creation on first login), session management via Redis, and role-based delegated access within tenants. The auth system integrates with the existing Flask web-ui at hellodj.celestium.life and stores tenant data in the CNPG PostgreSQL cluster.

## Glossary

- **Auth_Service**: The Flask-based authentication service handling OAuth2 flows, session management, and access control within the hellodj-web-ui application.
- **Tenant**: A first-class entity representing a subscriber to HelloDJ, identified by a UUID and linked to a Discord user account. Stored in the `tenants` PostgreSQL table.
- **Operator**: The platform superuser (celes) identified by Discord user ID via the OPERATOR_DISCORD_ID environment variable. Has unrestricted access to all platform resources.
- **Tenant_Owner**: The Discord user who created a tenant (the tenant's `discord_user_id`). Has full control over that tenant's resources and can delegate access.
- **Delegated_User**: A Discord user granted access to a tenant's resources by the Tenant_Owner. Authenticates via Discord OAuth2 and receives a role within the tenant.
- **Session_Store**: The Redis 7.x instance at `redis://redis.redis-service.svc.cluster.local:6379/0` used for session token storage, expiry tracking, and rate limiting.
- **Tenant_Store**: The PostgreSQL `tenants` table in the CNPG cluster at `postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj`.
- **Discord_OAuth_Provider**: Discord's OAuth2 authorization server at `https://discord.com/api/oauth2/authorize` and token endpoint at `https://discord.com/api/oauth2/token`.
- **Access_Token**: A cryptographically random session token issued by the Auth_Service after successful OAuth2 authentication, stored in the Session_Store.
- **Tenant_Role**: One of three permission levels within a tenant: `admin` (full tenant control), `editor` (modify settings, playlists, bans), `viewer` (read-only dashboard access).
- **RBAC_Store**: The PostgreSQL `tenant_roles` table storing delegated user-to-tenant role mappings (columns: tenant_id UUID, discord_user_id BIGINT, role TEXT, granted_at TIMESTAMPTZ, granted_by BIGINT).

## Requirements

### Requirement 1: Discord OAuth2 Login Initiation

**User Story:** As a user, I want to log in via Discord OAuth2, so that I can authenticate without creating a separate account.

#### Acceptance Criteria

1. WHEN a user navigates to the login endpoint, THE Auth_Service SHALL redirect the user to the Discord_OAuth_Provider authorization URL with `client_id`, `redirect_uri` set to `https://hellodj.celestium.life/auth/callback`, `response_type=code`, `scope=identify email guilds`, and a cryptographically random `state` parameter of at least 32 bytes (256 bits) of entropy.
2. THE Auth_Service SHALL store the `state` parameter in the Session_Store with a time-to-live of 300 seconds before redirecting.
3. IF the Discord_OAuth_Provider returns an error in the callback (via `error` and `error_description` query parameters), THEN THE Auth_Service SHALL redirect the user to an error page displaying a message indicating the authorization was denied or failed, and log the `error`, `error_description`, and `state` values.
4. WHEN the Discord_OAuth_Provider callback is received with a `state` parameter, THE Auth_Service SHALL validate that the `state` matches an entry in the Session_Store that has not expired, before processing the authorization code.
5. IF the `state` parameter in the callback is missing, does not match any entry in the Session_Store, or has expired, THEN THE Auth_Service SHALL reject the callback, redirect the user to an error page indicating the request could not be verified, and not exchange the authorization code.
6. IF the Session_Store is unavailable when the Auth_Service attempts to store the `state` parameter, THEN THE Auth_Service SHALL return an error page indicating login is temporarily unavailable and not redirect to the Discord_OAuth_Provider.

### Requirement 2: OAuth2 Callback and Token Exchange

**User Story:** As a user completing Discord login, I want the platform to securely exchange my authorization code for identity data, so that my session is established.

#### Acceptance Criteria

1. WHEN the Discord_OAuth_Provider redirects to the callback endpoint with a `code` and `state` parameter, THE Auth_Service SHALL validate the `state` parameter by checking that a matching key exists in the Session_Store (Redis) and SHALL consume it (delete from the store) so that the same state value cannot be used again.
2. IF the `state` parameter is missing, does not match any stored value, or was stored more than 300 seconds ago (expired TTL), THEN THE Auth_Service SHALL reject the request and redirect the user to the login page with an `error=state_mismatch` query parameter.
3. IF the Discord_OAuth_Provider redirects to the callback endpoint with an `error` parameter or without a `code` parameter, THEN THE Auth_Service SHALL redirect the user to the login page with an `error=denied` query parameter without attempting a token exchange.
4. WHEN the `state` parameter is valid, THE Auth_Service SHALL exchange the authorization code for an access token by sending a POST request to the Discord_OAuth_Provider token endpoint (`https://discord.com/api/oauth2/token`) with `client_id`, `client_secret`, `grant_type=authorization_code`, `code`, and `redirect_uri`, using a timeout of 10 seconds.
5. WHEN the token exchange succeeds, THE Auth_Service SHALL fetch the user's Discord profile from `https://discord.com/api/v10/users/@me` using the obtained access token as a Bearer token in the Authorization header, with a timeout of 10 seconds.
6. IF the token exchange or profile fetch fails (non-200 HTTP response or network timeout), THEN THE Auth_Service SHALL redirect the user to the login page with an `error=service_unavailable` query parameter and log the failure details including the HTTP status code and response body.

### Requirement 3: Tenant Auto-Creation on First Login

**User Story:** As a new Discord user logging in for the first time, I want a tenant to be automatically created for me, so that I can immediately access my dashboard.

#### Acceptance Criteria

1. WHEN a Discord user completes OAuth2 authentication and no tenant record exists with their `discord_user_id`, THE Auth_Service SHALL create a new tenant record in the Tenant_Store with a generated UUID, the user's `discord_user_id`, `discord_username` (truncated to 32 characters), `email` (if provided by Discord, max 254 characters), `created_at` set to the current UTC timestamp, and `updated_at` set to the current UTC timestamp, and SHALL return the tenant's UUID to the calling session.
2. WHEN a Discord user completes OAuth2 authentication and a tenant record already exists with their `discord_user_id`, THE Auth_Service SHALL update the `discord_username` and `updated_at` fields on the existing tenant record and SHALL return the existing tenant's UUID to the calling session.
3. THE Auth_Service SHALL perform tenant lookup and creation within a single database transaction with a timeout of no more than 5 seconds to prevent duplicate tenant records under concurrent login attempts.
4. IF the database transaction for tenant lookup or creation fails, THEN THE Auth_Service SHALL return an error indication to the calling session specifying that tenant provisioning failed, and SHALL NOT create a partial or corrupt tenant record.

### Requirement 4: Session Establishment and Management

**User Story:** As an authenticated user, I want a secure session that persists across requests, so that I do not need to re-authenticate on every page load.

#### Acceptance Criteria

1. WHEN authentication and tenant resolution complete successfully, THE Auth_Service SHALL generate a cryptographically random Access_Token of at least 256 bits using `secrets.token_urlsafe(32)`.
2. THE Auth_Service SHALL store the Access_Token in the Session_Store with the associated tenant ID, Discord user ID, Discord username, roles, IP address, `created_at` timestamp, and a time-to-live of 86400 seconds (24 hours).
3. THE Auth_Service SHALL set the Access_Token as an HTTP-only, Secure, SameSite=Lax cookie named `hellodj_session` with `Path=/` on the response.
4. WHEN a request includes a valid `hellodj_session` cookie, THE Auth_Service SHALL extend the session time-to-live in the Session_Store by 86400 seconds (sliding expiry).
5. IF a request includes an expired or invalid session cookie, THEN THE Auth_Service SHALL clear the cookie and redirect to the login endpoint, preserving the original request URL as a `next` query parameter.
6. THE Auth_Service SHALL enforce an absolute session lifetime of 7 days (604800 seconds) regardless of sliding expiry extensions, requiring re-authentication after that period.
7. IF the Session_Store is unavailable when validating a session cookie, THEN THE Auth_Service SHALL return HTTP 503 with a message indicating the service is temporarily unavailable.

### Requirement 5: Operator (Admin) Identification and Access

**User Story:** As the platform operator, I want superuser access to all platform resources, so that I can manage tenants, view metrics, and control the system.

#### Acceptance Criteria

1. WHEN an authenticated user's `discord_user_id` (compared as string) matches the OPERATOR_DISCORD_ID environment variable, THE Auth_Service SHALL assign the `operator` role to that user's session in addition to any tenant roles.
2. WHILE a user has the `operator` role, THE Auth_Service SHALL grant access to all admin API endpoints (prefixed with `/api/v1/admin/`) including tenant management, metrics, trial approvals, and system configuration.
3. WHILE a user has the `operator` role, THE Auth_Service SHALL grant read and write access to any tenant's resources regardless of tenant membership.
4. IF a non-operator user attempts to access an admin API endpoint, THEN THE Auth_Service SHALL return HTTP 403 with an error message indicating insufficient permissions.
5. IF the OPERATOR_DISCORD_ID environment variable is unset or empty, THEN THE Auth_Service SHALL deny all admin API endpoint access and log a warning on startup indicating operator access is not configured.
6. IF an unauthenticated user attempts to access an admin API endpoint, THEN THE Auth_Service SHALL redirect to the login endpoint with the original URL preserved as a `next` parameter.

### Requirement 6: Delegated Access and Role Assignment

**User Story:** As a tenant owner, I want to invite other Discord users to manage my bot settings, so that trusted members can help administer my bot instance.

#### Acceptance Criteria

1. WHILE a user is the Tenant_Owner, THE Auth_Service SHALL allow that user to create delegated access invitations specifying a Discord user ID and a Tenant_Role (`admin`, `editor`, or `viewer`), up to a maximum of 20 delegated users per tenant.
2. IF a Tenant_Owner creates an invitation for a Discord user ID that already has a role assignment for that tenant, THEN THE Auth_Service SHALL update the existing role assignment to the newly specified Tenant_Role and invalidate any active sessions for that user scoped to the affected tenant within 5 seconds.
3. WHEN a Delegated_User authenticates via Discord OAuth2 and has a role assignment in the RBAC_Store for a tenant, THE Auth_Service SHALL include that tenant and role in the user's session data.
4. WHILE a Delegated_User has the `admin` Tenant_Role, THE Auth_Service SHALL grant read and write access to the associated tenant's bot settings, playlists, ban lists, filters, queue, and playback controls — excluding tenant deletion, subscription management, and delegated access management.
5. WHILE a Delegated_User has the `editor` Tenant_Role, THE Auth_Service SHALL grant read and write access to bot settings, playlists, ban lists, and filters for the associated tenant, but SHALL NOT grant access to playback controls, subscription management, or delegated access management.
6. WHILE a Delegated_User has the `viewer` Tenant_Role, THE Auth_Service SHALL grant read-only access to the associated tenant's dashboard, settings, and playlists, and SHALL NOT grant write access to any resource.
7. WHEN a Tenant_Owner revokes a Delegated_User's access, THE Auth_Service SHALL remove the role assignment from the RBAC_Store and invalidate any active sessions for that user scoped to the affected tenant within 5 seconds of the revocation request.
8. IF a Tenant_Owner attempts to create a delegated access invitation that would exceed 20 delegated users for that tenant, THEN THE Auth_Service SHALL reject the request with an error message indicating the maximum delegate limit has been reached.

### Requirement 7: Role-Based Access Control Enforcement

**User Story:** As the system, I want to enforce role-based permissions on every API request, so that users can only access resources they are authorized for.

#### Acceptance Criteria

1. THE Auth_Service SHALL evaluate the authenticated user's role against the required permission for every protected API endpoint before processing the request, using the role hierarchy `operator > owner > admin > editor > viewer` where each role inherits all permissions of roles below it.
2. IF an authenticated user lacks the required Tenant_Role for the requested resource, THEN THE Auth_Service SHALL return HTTP 403 with an error message indicating the minimum role required to access that endpoint.
3. THE Auth_Service SHALL enforce tenant isolation such that a user's session grants access only to tenants where the user is the owner, has a delegated role, or is the Operator.
4. IF an authenticated user requests a resource belonging to a tenant they have no relationship with, THEN THE Auth_Service SHALL return HTTP 404 with a response body indistinguishable from a genuine not-found response to prevent tenant enumeration.
5. THE Auth_Service SHALL classify each protected endpoint as requiring either `viewer` (read-only GET requests), `editor` (write operations on settings, playlists, bans, and filters), `admin` (tenant configuration and user management), or `owner` (tenant deletion and delegated access management) minimum role.
6. IF a request to a protected endpoint has no valid session, THEN THE Auth_Service SHALL return HTTP 401 before any RBAC evaluation is performed.

### Requirement 8: Session Logout and Revocation

**User Story:** As an authenticated user, I want to log out and have my session immediately invalidated, so that my account is secure when I leave.

#### Acceptance Criteria

1. WHEN a user requests the logout endpoint (`/auth/logout`), THE Auth_Service SHALL delete the session entry from the Session_Store.
2. WHEN a user requests the logout endpoint, THE Auth_Service SHALL clear the `hellodj_session` cookie by setting it to an empty value with `Max-Age=0` and `Path=/`.
3. WHEN a user requests the logout endpoint, THE Auth_Service SHALL revoke the Discord OAuth2 access token by calling the Discord token revocation endpoint (`https://discord.com/api/oauth2/token/revoke`) with a timeout of 5 seconds.
4. IF the Discord token revocation fails (network error or non-200 response), THEN THE Auth_Service SHALL proceed with local session cleanup, redirect the user to the login page, and log the revocation failure including HTTP status.
5. WHEN logout completes successfully, THE Auth_Service SHALL redirect the user to the login page.
6. IF an unauthenticated user (no valid session cookie) requests the logout endpoint, THEN THE Auth_Service SHALL redirect to the login page without performing any cleanup.

### Requirement 9: Session Security and Rate Limiting

**User Story:** As the platform, I want to protect against session hijacking and brute-force attacks, so that user accounts remain secure.

#### Acceptance Criteria

1. IF a request includes a valid `hellodj_session` cookie but the requesting IP address (from the X-Forwarded-For header) does not match the IP address stored in the session record at authentication time, THEN THE Auth_Service SHALL delete the session from the Session_Store, clear the `hellodj_session` cookie, and return HTTP 401 requiring re-authentication.
2. THE Auth_Service SHALL limit OAuth2 login initiation requests (requests to the login endpoint that trigger the Discord redirect) to 10 per IP address per fixed 5-minute window, using a Session_Store key with format `ratelimit:login:{ip}` and a TTL of 300 seconds as the rate-limit counter.
3. IF the rate limit is exceeded, THEN THE Auth_Service SHALL return HTTP 429 with a `Retry-After` header indicating the number of seconds remaining until the current 5-minute window expires, and a response body containing an error message indicating the rate limit has been exceeded.
4. THE Auth_Service SHALL set the following security headers on all responses via a Flask `after_request` handler: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Strict-Transport-Security: max-age=31536000; includeSubDomains`.

### Requirement 10: Multi-Tenant Session Context

**User Story:** As a user with access to multiple tenants, I want to switch between tenant contexts, so that I can manage different bot instances from a single session.

#### Acceptance Criteria

1. WHEN an authenticated user has roles in multiple tenants, THE Auth_Service SHALL include all accessible tenant IDs and their corresponding roles in the session data stored in the Session_Store.
2. WHEN a user sends a POST request to `/api/v1/session/tenant` with a valid `tenant_id` that exists in the user's accessible tenant list, THE Auth_Service SHALL update the `active_tenant_id` field in the session within 500 milliseconds and return HTTP 200 with the updated active tenant ID and role.
3. WHEN a new session is established, THE Auth_Service SHALL set the `active_tenant_id` to the user's owned tenant if one exists, or the first tenant in alphabetical order by tenant UUID from their access list otherwise.
4. IF a user sends a POST request to `/api/v1/session/tenant` with a `tenant_id` that is not in the user's accessible tenant list, THEN THE Auth_Service SHALL return HTTP 403 with an error message indicating the user lacks access to the specified tenant, and leave the session's `active_tenant_id` unchanged.
5. WHILE an `active_tenant_id` is set in the session, THE Auth_Service SHALL filter all tenant-scoped API responses to return only resources belonging to that `active_tenant_id`.

### Requirement 11: Discord OAuth2 Token Refresh

**User Story:** As the platform, I want to refresh Discord access tokens before they expire, so that user profile data remains current and token revocation remains functional.

#### Acceptance Criteria

1. WHEN the Auth_Service stores a Discord OAuth2 token pair, THE Auth_Service SHALL store the access token, refresh token, and token expiry as a Unix timestamp (seconds) in the Session_Store under keys `discord_access_token`, `discord_refresh_token`, and `discord_token_expires_at` within the session record.
2. WHEN an authenticated request accesses a session whose associated Discord access token has less than 3600 seconds remaining before expiry, THE Auth_Service SHALL use the refresh token to obtain a new access token from the Discord_OAuth_Provider and persist the updated access token, refresh token, and expiry timestamp back to the Session_Store before completing the request.
3. IF multiple concurrent requests trigger a token refresh for the same session, THEN THE Auth_Service SHALL execute only one refresh operation and serve remaining concurrent requests with the updated token, preventing redundant refresh calls to the Discord_OAuth_Provider.
4. IF the token refresh fails with an `invalid_grant` error from the Discord_OAuth_Provider, THEN THE Auth_Service SHALL invalidate the session in the Session_Store and redirect the user to re-authenticate via the Discord OAuth2 login flow.
5. IF the token refresh fails due to a network error (connection timeout exceeding 10 seconds, connection refused, DNS resolution failure, or HTTP 5xx response from the Discord_OAuth_Provider), THEN THE Auth_Service SHALL continue serving the request using cached session data, increment a per-session retry counter, and attempt the refresh again on the next session access.
6. IF the per-session network-error retry counter reaches 3 consecutive failed refresh attempts, THEN THE Auth_Service SHALL invalidate the session and redirect the user to re-authenticate via the Discord OAuth2 login flow.
