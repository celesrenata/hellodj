# Design Document

## Overview

Replace the Cognito **hosted-UI redirect** (`_start_cognito` in `auth.py` →
`amazoncognito.com/login|signup`) with **first-party Flask forms** that call the
Cognito `cognito-idp` API server-side. Cognito remains the identity provider
(`auth_routing.route_auth(ADMIN_AUTH|INITIAL_REGISTRATION|ACCOUNT_RECOVERY) ==
COGNITO` is unchanged); only the UI surface moves in-house.

The current flow uses the hosted UI and then reads groups from the ID token
**without verifying its signature** (safe only because that token came straight
from the token endpoint over TLS in the same request). Custom forms broaden the
trust surface, so the design adds **full JWKS signature + claim verification**,
**challenge handling** (new-password, MFA), **rate limiting**, and
**non-enumerating errors**.

## Architecture

The web-ui (Flask/gunicorn) serves branded auth forms and calls the Cognito
`cognito-idp` API server-side; Cognito remains the identity provider. Tokens
returned by Cognito are verified against the pool JWKS before any claim is
trusted. No traffic is redirected to the hosted UI in the primary flow.

```
Browser ── HTTPS ──> CloudFront ──> ALB ──> web-ui pod (auth.py)
                                              │  cognito_auth  ── HTTPS ─> Cognito cognito-idp
                                              │  cognito_jwt   ── HTTPS ─> Cognito JWKS
                                              └─ Flask session (signed cookie, shared FLASK_SECRET_KEY)
```

The auth-routing invariant is unchanged: `route_auth(ADMIN_AUTH |
INITIAL_REGISTRATION | ACCOUNT_RECOVERY) == COGNITO`. This design only changes
the presentation surface for those Cognito-routed purposes.

### Auth flow decision: `USER_PASSWORD_AUTH`

Selected: **`USER_PASSWORD_AUTH`** (`ALLOW_USER_PASSWORD_AUTH` on the app
client). The web-ui posts the password to Cognito over TLS (CloudFront → ALB →
pod all TLS; the pod→Cognito call is HTTPS). This is the standard choice for a
server-side confidential-ish web app and keeps the challenge handling simple.

Rejected: **SRP (`USER_SRP_AUTH`)** — the password never leaves as plaintext,
but it requires implementing the SRP handshake (SRP_A/PASSWORD_VERIFIER,
`aws_srp`-style math) in Python. Higher complexity for marginal benefit given
TLS everywhere and an admin-panel threat model. Can be revisited later without
changing the route/template surface.

The app client is public (`generateSecret: false`), so no `SECRET_HASH` is
required on the `InitiateAuth`/`SignUp`/`ForgotPassword` calls.

## Components and Interfaces

All web-ui changes live under `platform/components/web-ui/`. Files stay under
the 500-line ceiling (`check_line_count.py`); auth logic is split across a thin
route module + focused service modules (mirrors the existing
`auth.py` / `auth_oauth.py` split).

| File | Change | Purpose |
|------|--------|---------|
| `cognito_auth.py` (new) | add | `CognitoAuth` service: `initiate_auth`, `respond_challenge`, `sign_up`, `confirm_sign_up`, `forgot_password`, `confirm_forgot_password`. Wraps boto3 `cognito-idp`, injectable client (fakes-friendly Protocol), never logs secrets, normalizes Cognito errors to non-enumerating results. |
| `cognito_jwt.py` (new) | add | `verify_token(token, expected_use)`: RS256 JWKS verification (`iss`/`aud`/`client_id`/`token_use`/`exp`), bounded-TTL JWKS cache, single refetch on `kid` miss. `groups_from_verified(claims)`. |
| `auth.py` | modify | Replace `_start_cognito` redirect routes (`/admin`, `/register`, `/recover`) with first-party form GET/POST handlers driving `CognitoAuth`; establish session using **verified** claims. Discord/Tidal/source routes untouched. |
| `auth_oauth.py` | modify | `groups_from_id_token` gains verification via `cognito_jwt` (or the hosted-callback path is retired). Keep Discord helpers unchanged. |
| `auth_ratelimit.py` (new) | add | Small in-process fixed-window limiter keyed by client ip + route (session/Dynamo-free; sufficient per-pod, documented as best-effort). |
| `templates/pages/login.html` | modify | First-party login form (username/email + password) instead of hosted-UI buttons. |
| `templates/pages/auth_new_password.html` (new) | add | Set-new-password challenge form. |
| `templates/pages/auth_mfa.html` (new) | add | MFA (SOFTWARE_TOKEN_MFA) code form. |
| `templates/pages/auth_register.html` (new) | add | Self-registration form + confirm-code step. |
| `templates/pages/auth_recover.html` (new) | add | Forgot-password + confirm-reset forms. |
| `bootstrap.py` | modify | Build `CognitoAuth` (+ jwt verifier) from env; degrade to `None` when unconfigured. |

### CDK

| File | Change |
|------|--------|
| `platform/infra/lib/auth-stack.ts` | Add `authFlows: { userPassword: true }` (`ALLOW_USER_PASSWORD_AUTH`) to the `WebUiClient`. Keep existing `userSrp` + hosted-UI OAuth for fallback. Assert in `auth-stack.test.ts`. |

No workloads-stack env change is required: the web-ui already receives
`COGNITO_CLIENT_ID`, `COGNITO_DOMAIN`, and `HELLODJ_COGNITO_USER_POOL_ID`. JWKS
verification needs the **user pool id + region**, both already in env.

## Data Models

No new persistent datastore entities. State is transient:

- **Flask session (signed cookie)**: `user = {sub, is_admin, groups, provider}`
  on success (identical shape to today). During a challenge, the opaque Cognito
  `Session` string and the pending `username` are held server-side in the Flask
  session under short-lived keys (`auth_challenge_session`,
  `auth_challenge_user`, `auth_challenge_name`) and cleared on completion.
- **JWKS cache (in-process)**: `{kid -> public_key}` with a TTL timestamp;
  per-pod, rebuilt on miss/expiry. Not persisted.
- **Rate-limit counters (in-process)**: `{(ip, route) -> (count, window_start)}`;
  per-pod best-effort, not persisted.
- **Cognito claims (verified, in-memory only)**: `sub`, `cognito:groups`,
  `token_use`, `aud`/`client_id`, `iss`, `exp`. Never stored beyond the session
  fields above.

No secrets (passwords, confirmation codes, `Session` tokens) are ever written to
logs or persistent storage.

## Sequences

### Login (happy path)

```
Browser        web-ui (auth.py)          Cognito              cognito_jwt
  |  GET /auth/login                       |                     |
  |<-- login form (glass) --|              |                     |
  |  POST user+pass -------->|             |                     |
  |             InitiateAuth(USER_PASSWORD_AUTH) ------->        |
  |             <----- AuthenticationResult (id/access) ---      |
  |                          | verify_token(id, "id") --------->|
  |                          |<------------ verified claims -----|
  |             session['user'] = {sub, is_admin, groups}       |
  |<-- 302 /dashboard ------ |             |                     |
```

### Login (challenges)

- `InitiateAuth` returns `ChallengeName=NEW_PASSWORD_REQUIRED` + `Session` →
  render `auth_new_password.html` → POST →
  `RespondToAuthChallenge(NEW_PASSWORD_REQUIRED, {NEW_PASSWORD}, Session)` →
  verify tokens → session.
- `ChallengeName=SOFTWARE_TOKEN_MFA` + `Session` → render `auth_mfa.html` →
  `RespondToAuthChallenge(SOFTWARE_TOKEN_MFA, {SOFTWARE_TOKEN_MFA_CODE},
  Session)`. The `Session` token is held in the Flask session (server-side),
  never exposed to the page beyond an opaque CSRF-guarded hop.

### Register / Recover

- Register: `SignUp(email,password)` → `auth_register.html` confirm step →
  `ConfirmSignUp(email, code)` → redirect `/auth/login`.
- Recover: `ForgotPassword(email)` → always render "if an account exists, a
  code was sent" → `ConfirmForgotPassword(email, code, newpass)` →
  `/auth/login`.

## Token verification (`cognito_jwt.py`)

- Issuer: `https://cognito-idp.<region>.amazonaws.com/<userPoolId>`.
- JWKS: `<issuer>/.well-known/jwks.json`, cached with TTL (e.g. 1h) keyed by
  `kid`; a `kid` miss forces one refetch before failing.
- Verify: RS256 signature; `iss` matches; `token_use` in {`id`,`access`} as
  expected; `aud` (id token) or `client_id` (access token) == app client id;
  `exp`/`iat` within skew. Groups from the **verified** `cognito:groups` claim.
- Dependency: prefer `PyJWT[crypto]` if already vendored in the web-ui flake;
  otherwise implement minimal RS256 verify with `cryptography` (already present
  in the platform). Design task 1 confirms which is available before coding.

## Error Handling

Map Cognito exceptions to generic, non-enumerating outcomes:

| Cognito error | Surfaced result |
|---|---|
| `NotAuthorizedException`, `UserNotFoundException` | "Incorrect username or password." |
| `UserNotConfirmedException` | route to confirm-code step (no leak beyond "confirm your email") |
| `CodeMismatchException`, `ExpiredCodeException` | "That code is invalid or expired." |
| `ForgotPassword` on unknown email | same "if an account exists…" copy |
| `InvalidPasswordException` | field-level password-policy message |
| throttling / limiter trip | "Too many attempts, try again shortly." |

## Correctness Properties

### Property 1: No unverified trust

No session is ever established from a token whose signature or standard claims
(`iss` / `aud` | `client_id` / `token_use` / `exp`) fail verification.

**Validates: Requirements 4.1, 4.2**

### Property 2: Admin only from verified group

`user.is_admin` is true iff the verified `cognito:groups` claim contains
`admins`.

**Validates: Requirements 1.2, 6.3**

### Property 3: No enumeration

For any email/username, an invalid login or an unknown-account recovery request
yields byte-identical generic copy to the valid-but-wrong-password /
known-account case (modulo the intended confirm-code redirect for unconfirmed
users).

**Validates: Requirements 1.5, 3.4**

### Property 4: Secret hygiene

For all inputs, passwords, confirmation codes, and the Cognito `Session` string
never appear in logs or client-visible errors.

**Validates: Requirements 5.3**

### Property 5: Routing preserved

`route_auth` output for every purpose is unchanged by this feature.

**Validates: Requirements 6.2**

## Preservation

- `/auth/discord/callback`, `/auth/tidal/callback`, `/auth/sources/*` unchanged.
- Invite registration (`/invite/<token>` in `pages.py`) unchanged.
- `_is_admin` / `user.is_admin` semantics unchanged (now fed by verified claims).
- Degraded mode: unconfigured Cognito → auth routes render "auth unavailable".

## Testing strategy

- `cognito_auth`: unit tests with a fake `cognito-idp` client covering each
  flow + challenge + error mapping; assert secrets never appear in logs/errors.
- `cognito_jwt`: verify accepts a correctly-signed token and rejects
  bad-signature / wrong-iss / wrong-aud / wrong-use / expired; `kid`-miss
  refetch path.
- Property test (hypothesis): random invalid credentials/codes never establish
  a session and never enumerate.
- Route tests (Flask test client): each GET renders the branded form; each POST
  drives the injected fake and lands on the right next step; CSRF enforced.
- CDK: `auth-stack.test.ts` asserts `ALLOW_USER_PASSWORD_AUTH` on the client.
- Gates: `ruff --target-version py314`, `pytest -q`, `check_line_count.py`,
  and `tsc --noEmit && jest` for infra.
