# Implementation Plan: Custom Auth Forms

## Overview

Replace the Cognito hosted-UI redirect with first-party, branded Flask forms
for login, self-registration, and account recovery that call Cognito
server-side, with full JWKS token verification, challenge handling, rate
limiting, and non-enumerating errors. Cognito remains the identity provider.

## Tasks

- [x] 1. Confirm the JWT crypto dependency available in the web-ui image
  - Check the web-ui flake / requirements for `PyJWT[crypto]`; if absent,
    confirm `cryptography` is present (it is used elsewhere) and plan a minimal
    RS256 verify path. Record the choice in `cognito_jwt.py`'s module docstring.
  - _Requirements: 4.1, 4.3_

- [x] 2. Implement `cognito_jwt.py` (token verification)
  - `verify_token(token, expected_use)`: RS256 JWKS verify + `iss`/`aud` or
    `client_id`/`token_use`/`exp` checks; bounded-TTL JWKS cache keyed by `kid`
    with single refetch on miss. `groups_from_verified(claims)`.
  - Unit tests: accepts valid signed token; rejects bad-sig / wrong-iss /
    wrong-aud / wrong-use / expired; `kid`-miss refetch.
  - _Requirements: 4.1, 4.2, 4.3_

- [x] 3. Implement `cognito_auth.py` (`CognitoAuth` service)
  - Injectable `cognito-idp` client Protocol. Methods: `initiate_auth`,
    `respond_challenge`, `sign_up`, `confirm_sign_up`, `forgot_password`,
    `confirm_forgot_password`. Normalize Cognito exceptions to non-enumerating
    results; never log/return secrets or codes.
  - Unit tests with a fake client for each method + error mapping.
  - _Requirements: 1.2, 1.3, 1.4, 2.2, 2.3, 3.2, 3.3, 5.3_

- [x] 4. Implement `auth_ratelimit.py` (best-effort limiter)
  - In-process fixed-window limiter keyed by client ip + route; documented as
    per-pod best-effort. Helper usable as a guard in the auth POST handlers.
  - Unit tests: trips after N failures in window; resets after window.
  - _Requirements: 5.1_

- [x] 5. Rewrite login routes in `auth.py`
  - Replace `_start_cognito` for `/admin` (login) with first-party
    GET (render form) + POST (`CognitoAuth.initiate_auth` → verify → session).
  - Handle `NEW_PASSWORD_REQUIRED` and `SOFTWARE_TOKEN_MFA` challenges (hold
    `Session` server-side, CSRF-guarded hops to the challenge templates).
  - Generic auth error copy; CSRF via existing session-state pattern.
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 4.2, 5.2, 6.3_

- [x] 6. Add self-registration routes in `auth.py`
  - `/auth/register` GET/POST → `SignUp` → confirm-code step → `ConfirmSignUp`
    → redirect `/auth/login`. Password-policy validation surfaced field-level.
  - Leave `/invite/<token>` registration in `pages.py` untouched.
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

- [x] 7. Add account-recovery routes in `auth.py`
  - `/auth/recover` GET/POST → `ForgotPassword` (always non-enumerating
    confirmation) → confirm step → `ConfirmForgotPassword` → `/auth/login`.
  - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 8. Build the branded templates
  - `login.html` (replace hosted-UI buttons with the form),
    `auth_new_password.html`, `auth_mfa.html`, `auth_register.html`,
    `auth_recover.html`. HelloDJ dark-glass; labelled inputs; `aria-live`
    error regions; visible focus.
  - _Requirements: 1.1, 2.1, 3.1, 7.1, 7.2_

- [x] 9. Wire services on `app.extensions`
  - Build `CognitoAuth` + jwt verifier + `RateLimiter` from env in
    `app.create_app` (alongside `admin_directory`, the existing extension-wiring
    site — not `bootstrap.build_services`, which returns datastore services);
    each degrades to `None` when Cognito is unconfigured.
  - Route handlers render "auth unavailable" when the service is `None`.
  - _Requirements: 6.4_

- [x] 10. Enable `USER_PASSWORD_AUTH` on the app client (CDK)
  - `auth-stack.ts` `WebUiClient` `authFlows: { userSrp: true, userPassword: true }`.
  - `auth-stack.test.ts` asserts `ALLOW_USER_PASSWORD_AUTH` present.
  - `tsc --noEmit && jest`.
  - _Requirements: 6.1_

- [x] 11. Route + property tests (Flask test client)
  - Each auth GET renders its branded form; each POST drives the injected fake
    to the correct next step; CSRF enforced; invalid creds/codes never
    establish a session and never enumerate.
  - _Requirements: 1.5, 3.4, 4.2, 5.2, 5.3_

- [x] 12. Preservation regression pass
  - Confirm Discord/Tidal/source-OAuth routes and invite registration are
    unchanged and green; `_is_admin` gate unchanged.
  - Run web-ui gates: `ruff --target-version py314 .`, `pytest -q`,
    `check_line_count.py platform/components/web-ui`.
  - _Requirements: 6.2, 6.3_

- [x] 13. Update steering / architecture docs
  - Note in `website-debug-context.md` that admin login/registration/recovery
    are first-party forms calling Cognito server-side (hosted UI retired /
    fallback only), tokens JWKS-verified, `USER_PASSWORD_AUTH` enabled.
  - _Requirements: 6.1_

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1", "10"], "dependsOn": [] },
    { "wave": 2, "tasks": ["2", "3", "4"], "dependsOn": ["1"] },
    { "wave": 3, "tasks": ["5", "6", "7"], "dependsOn": ["2", "3", "4"] },
    { "wave": 4, "tasks": ["8", "9"], "dependsOn": ["5", "6", "7"] },
    { "wave": 5, "tasks": ["11"], "dependsOn": ["8", "9"] },
    { "wave": 6, "tasks": ["12"], "dependsOn": ["11"] },
    { "wave": 7, "tasks": ["13"], "dependsOn": ["12"] }
  ]
}
```

- Tasks 2, 3, 4 are independent and can proceed in parallel after task 1.
- Tasks 5, 6, 7 depend on 2 + 3 (+ 4 for the POST guards).
- Task 10 (CDK) is independent of the Python work but MUST land before deploy.

## Notes

- Auth-routing invariant (`auth_routing.py`) is NOT modified — this is a
  presentation change to the Cognito-routed purposes only.
- Every file stays under the 500-line ceiling (`check_line_count.py`); split
  services (`cognito_auth`, `cognito_jwt`, `auth_ratelimit`) keep `auth.py` thin.
- Gates before push: web-ui `ruff --target-version py314 . && pytest -q` +
  `check_line_count.py`; infra `tsc --noEmit && jest`.
- Deploy path: CDK app-client change via `cdk deploy hellodj-auth`; web-ui
  Python via CodeCommit push → pipeline → `rollout restart deploy/web-ui`.
