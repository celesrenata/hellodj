# Requirements Document

## Introduction

The HelloDJ web-ui currently delegates administrator login, initial
registration, and account recovery to the **Cognito Hosted UI** (an
authorization-code + PKCE redirect to `<pool>.auth.<region>.amazoncognito.com`).
The hosted UI is visually off-brand (it cannot match the HelloDJ dark-glass
design system) and takes the user off-site mid-flow.

This feature replaces the hosted-UI redirect with **first-party Flask forms**
served by the web-ui itself, styled with the HelloDJ design system. The forms
collect credentials and call Cognito's API server-side (`InitiateAuth`,
`SignUp`, `ConfirmSignUp`, `ForgotPassword`, `ConfirmForgotPassword`, and the
`RespondToAuthChallenge` challenge flows). Cognito remains the identity
provider of record — this changes only the **presentation surface**, not the
routing.

### Auth-routing invariant (unchanged)

`hellodj_platform_logic.auth_routing.route_auth` routes `ADMIN_AUTH`,
`INITIAL_REGISTRATION`, and `ACCOUNT_RECOVERY` to `AuthProvider.COGNITO`. That
invariant is preserved: custom forms still authenticate **against Cognito**.
"Cognito" means the identity provider, not the hosted-UI surface. No change to
`auth_routing.py` is required or permitted by this feature.

### Security posture

Because the web-ui now handles credentials directly (rather than only receiving
a post-hosted-UI authorization code), this feature MUST add controls the hosted
UI provided for free: server-side token verification, challenge handling
(new-password, MFA), rate limiting, and generic (non-enumerating) error copy.

## Glossary

- **Hosted UI**: Cognito's off-site login/signup pages at
  `<pool>.auth.<region>.amazoncognito.com`. The surface this feature replaces.
- **First-party forms**: HelloDJ-styled login/register/recover forms served by
  the Flask web-ui itself, calling Cognito's API server-side.
- **`USER_PASSWORD_AUTH`**: Cognito `InitiateAuth` flow where the app submits
  the username + password (over TLS) rather than performing SRP.
- **Challenge**: a Cognito follow-up step (`NEW_PASSWORD_REQUIRED`,
  `SOFTWARE_TOKEN_MFA`) completed via `RespondToAuthChallenge`.
- **JWKS**: the pool's public signing keys used to verify Cognito JWT signatures.
- **Non-enumerating**: error copy that does not reveal whether an account exists.
- **admins group**: the Cognito group whose members are administrators.

## Requirements

### Requirement 1: Branded first-party login

**User Story:** As an administrator, I want to sign in through a HelloDJ-styled
form on the web-ui, so that I never get redirected to the off-brand Cognito
hosted UI.

#### Acceptance Criteria

1. WHEN a user visits `/auth/login` THEN the system SHALL render a first-party
   login form (username/email + password) styled with the HelloDJ design
   system, served by the web-ui (no redirect to `amazoncognito.com`).
2. WHEN a user submits valid credentials THEN the system SHALL call Cognito
   `InitiateAuth` server-side, establish the Flask session, and set
   `user.is_admin` from the `admins` group claim, exactly as the hosted-UI
   callback does today.
3. WHEN Cognito returns a `NEW_PASSWORD_REQUIRED` challenge (e.g. the seeded
   Admin_Bootstrap_Credential's first login) THEN the system SHALL render a
   set-new-password form and complete the challenge via
   `RespondToAuthChallenge`.
4. WHEN the pool requires an MFA challenge (`SOFTWARE_TOKEN_MFA`) THEN the
   system SHALL render an MFA code form and complete the challenge via
   `RespondToAuthChallenge`.
5. WHEN credentials are invalid THEN the system SHALL show a single generic
   error ("Incorrect username or password") that does not reveal whether the
   account exists (no user enumeration).

### Requirement 2: Branded self-registration

**User Story:** As a new user, I want to self-register through a HelloDJ-styled
form, so that sign-up matches the rest of the product.

#### Acceptance Criteria

1. WHEN a user visits `/auth/register` THEN the system SHALL render a
   first-party registration form (email + password) styled with the HelloDJ
   design system.
2. WHEN a user submits the registration form THEN the system SHALL call Cognito
   `SignUp` and render a confirmation-code entry form.
3. WHEN a user submits a valid confirmation code THEN the system SHALL call
   `ConfirmSignUp` and direct the user to login.
4. WHEN a submitted password does not meet the pool password policy THEN the
   system SHALL surface a clear, field-level validation error before or from
   the `SignUp` call.
5. The existing invite-based registration path (`/invite/<token>` in
   `pages.py`, which creates a CONFIRMED account with a SUPPRESSED Cognito
   email) SHALL remain unchanged and continue to work.

### Requirement 3: Branded account recovery

**User Story:** As a user who forgot my password, I want to reset it through a
HelloDJ-styled flow, so that recovery matches the product.

#### Acceptance Criteria

1. WHEN a user visits `/auth/recover` THEN the system SHALL render a
   first-party "forgot password" form (email) styled with the design system.
2. WHEN a user submits the recover form THEN the system SHALL call Cognito
   `ForgotPassword` and render a form to enter the emailed code + new password.
3. WHEN a user submits a valid code + policy-compliant new password THEN the
   system SHALL call `ConfirmForgotPassword` and direct the user to login.
4. WHEN the submitted email is not a registered account THEN the system SHALL
   render the same "if an account exists, a code was sent" confirmation (no
   enumeration), matching Cognito's own non-enumerating behavior.

### Requirement 4: Token verification

**User Story:** As the platform owner, I want tokens verified so that a forged
or tampered token can never grant admin.

#### Acceptance Criteria

1. WHEN the system reads group/identity claims from a Cognito-issued token
   (id/access token from `InitiateAuth` / `RespondToAuthChallenge`) THEN it
   SHALL verify the token's RS256 signature against the pool's JWKS, and verify
   `iss`, `aud`/`client_id`, `token_use`, and `exp`, BEFORE trusting any claim.
2. IF signature or claim verification fails THEN the system SHALL reject the
   login (no session established) and show the generic auth error.
3. The JWKS SHALL be fetched from the pool's well-known endpoint and cached with
   a bounded TTL; a `kid` miss SHALL trigger a single refetch before failing.

### Requirement 5: Abuse resistance

**User Story:** As the platform owner, I want auth endpoints rate-limited so
brute-force and code-guessing are impractical.

#### Acceptance Criteria

1. WHEN repeated failed login / confirm / recover attempts originate from the
   same source within a short window THEN the system SHALL throttle further
   attempts (fixed backoff / lockout) and return a generic try-later message.
2. All auth POST routes SHALL be CSRF-protected using the existing Flask
   session state pattern already used for OAuth `state`.
3. Passwords and confirmation codes SHALL never be logged, and SHALL never
   appear in any error surfaced to the client.

### Requirement 6: Provider wiring and preservation

**User Story:** As a maintainer, I want the change wired into CDK correctly and
existing flows preserved, so nothing else breaks.

#### Acceptance Criteria

1. The Cognito app client SHALL enable the auth flow the forms use
   (`USER_PASSWORD_AUTH`, i.e. `ALLOW_USER_PASSWORD_AUTH`) in `auth-stack.ts`.
   The hosted-UI OAuth config MAY remain for fallback.
2. Day-to-day **Discord OAuth** login (`/auth/discord/callback`), **Tidal**
   source auth, and all per-guild **source OAuth** routes SHALL be unchanged
   (preservation).
3. The `admins`-group-driven admin gate (`_is_admin` / `user.is_admin`) SHALL
   behave identically to today.
4. WHEN Cognito is unconfigured (no client id/pool) THEN the auth routes SHALL
   degrade gracefully (render a clear "auth unavailable" state) rather than
   crashing, matching the existing degraded-mode convention.

### Requirement 7: Design system and accessibility

**User Story:** As a user, I want the auth forms to look and behave like the
rest of HelloDJ.

#### Acceptance Criteria

1. All auth forms SHALL use the HelloDJ dark-glass design system (glass panel,
   OKLCH palette, focus-visible rings) consistent with the app shell.
2. Forms SHALL be keyboard-accessible with visible focus, labelled inputs,
   `aria-live` error regions, and adequate color contrast (WCAG AA target;
   full validation requires manual assistive-tech testing).
