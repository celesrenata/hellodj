# Requirements Document

## Introduction

This feature adds an admin-controlled global registration mode to the HelloDJ
web-ui. The mode is a single toggle — OPEN or CLOSED — that governs whether
anonymous visitors may self-register through the standard first-party Cognito
sign-up flow at `/register`. The login page displays the current mode so
visitors know whether they can create an account. Administrators (Cognito
`admins` group) view and change the mode from the admin panel, and every change
is recorded to an audit trail.

Enforcement is server-side authoritative: hiding the "Register" link on the
login page is advisory only; the `/register` route itself rejects access when
the mode is CLOSED. Single-use invite links (`/invite/<token>`) are governed by
the existing invite flow and are unaffected by the registration mode in either
state.

## Glossary

- **Web_UI**: The HelloDJ Flask web application serving the login page, the
  self-registration route (`auth.register`), and the admin panel (`pages.admin`).
- **Registration_Mode**: The single global setting with exactly two values,
  `OPEN` or `CLOSED`, stored in the global config payload via
  `ConfigStore.get_global()` / `ConfigStore.set_global()` (CoreTable item
  `PK=CONFIG#GLOBAL`, `SK=CONFIG`).
- **Registration_Mode_Store**: The read/write component that returns the current
  `Registration_Mode` and persists changes, backed by `ConfigStore` over
  CoreTable.
- **Self_Registration**: The first-party Cognito sign-up flow reachable at the
  `/register` route (`auth.register` → `handle_register` → Cognito `SignUp` →
  email verification code → `ConfirmSignUp`).
- **Invite_Registration**: The single-use invite-link flow reachable at
  `/invite/<token>`, which creates an account regardless of `Registration_Mode`.
- **Admin**: An authenticated session whose verified Cognito `admins` group
  membership sets `user.is_admin`, as evaluated by `pages._is_admin()`.
- **Non_Admin**: Any session, authenticated or anonymous, for which
  `pages._is_admin()` returns false.
- **Login_Page**: The public page rendered by `pages.login` from
  `templates/pages/login.html`.
- **Admin_Panel**: The admin-only page rendered by `pages.admin` from
  `templates/pages/admin.html`.
- **Mode_Change_Audit_Record**: A persisted record of a single
  `Registration_Mode` change containing the acting Admin identity, the previous
  value, the new value, and a timestamp.

## Requirements

### Requirement 1: Secure default registration mode

**User Story:** As a platform operator, I want registration to default to closed
when no mode has been set, so that the platform stays invite-only unless an admin
deliberately opens it.

#### Acceptance Criteria

1. IF the global config payload contains no stored `Registration_Mode` value,
   THEN THE Registration_Mode_Store SHALL report the current
   `Registration_Mode` as `CLOSED`.
2. WHERE a stored `Registration_Mode` value is present, THE
   Registration_Mode_Store SHALL report that stored value as the current
   `Registration_Mode`.
3. IF a stored `Registration_Mode` value is neither `OPEN` nor `CLOSED`, THEN
   THE Registration_Mode_Store SHALL report the current `Registration_Mode` as
   `CLOSED`.

### Requirement 2: Server-side enforcement at the registration route

**User Story:** As a platform operator, I want the `/register` route itself to
reject self-registration when the mode is closed, so that enforcement does not
depend on hiding a link in the page.

#### Acceptance Criteria

1. WHILE the current `Registration_Mode` is `CLOSED`, WHEN a visitor sends a GET
   request to the `/register` route, THE Web_UI SHALL redirect the visitor to
   the Login_Page with a registration-closed notice and SHALL NOT render the
   Self_Registration form.
2. WHILE the current `Registration_Mode` is `CLOSED`, WHEN a visitor sends a
   POST request to the `/register` route, THE Web_UI SHALL redirect the visitor
   to the Login_Page with a registration-closed notice and SHALL NOT invoke the
   Cognito `SignUp` flow.
3. WHILE the current `Registration_Mode` is `OPEN`, WHEN a visitor sends a GET
   request to the `/register` route, THE Web_UI SHALL render the
   Self_Registration form.
4. WHILE the current `Registration_Mode` is `OPEN`, WHEN a visitor submits the
   Self_Registration form via POST to the `/register` route, THE Web_UI SHALL
   run the Cognito `SignUp` → email-verification → `ConfirmSignUp` flow and
   SHALL require email verification before the account is usable.
5. WHEN a visitor accesses the `/invite/<token>` route, THE Web_UI SHALL process
   Invite_Registration through the existing invite flow independent of the
   current `Registration_Mode`.

### Requirement 3: Login page mode banner

**User Story:** As a visitor, I want the login page to tell me whether
registration is open or closed, so that I know whether I can create an account.

#### Acceptance Criteria

1. WHILE the current `Registration_Mode` is `OPEN`, WHEN the Login_Page is
   rendered, THE Web_UI SHALL display the banner text "Registration is open —
   create an account".
2. WHILE the current `Registration_Mode` is `CLOSED`, WHEN the Login_Page is
   rendered, THE Web_UI SHALL display the banner text "Registration is currently
   closed — invite only".
3. WHILE the current `Registration_Mode` is `OPEN`, WHEN the Login_Page is
   rendered, THE Web_UI SHALL display a link to the `/register` route.
4. WHILE the current `Registration_Mode` is `CLOSED`, WHEN the Login_Page is
   rendered, THE Web_UI SHALL omit the link to the `/register` route.

### Requirement 4: Admin-only mode control

**User Story:** As an admin, I want to view and change the registration mode from
the admin panel, so that only administrators can open or close self-registration.

#### Acceptance Criteria

1. WHEN an Admin views the Admin_Panel, THE Web_UI SHALL display the current
   `Registration_Mode` and a control to set it to `OPEN` or `CLOSED`.
2. WHEN an Admin submits a mode change to `OPEN` or `CLOSED`, THE
   Registration_Mode_Store SHALL persist the submitted value as the current
   `Registration_Mode`.
3. IF a Non_Admin requests the Admin_Panel or the mode-change route, THEN THE
   Web_UI SHALL deny the request and SHALL NOT change the current
   `Registration_Mode`.
4. WHEN the Admin_Panel is rendered for a Non_Admin, THE Web_UI SHALL omit the
   registration-mode control.

### Requirement 5: Auditability of mode changes

**User Story:** As a platform operator, I want each registration-mode change
recorded, so that I can review who changed the mode and when.

#### Acceptance Criteria

1. WHEN an Admin changes the current `Registration_Mode`, THE Web_UI SHALL write
   a Mode_Change_Audit_Record containing the acting Admin identity, the previous
   value, the new value, and a timestamp.
2. WHEN an Admin submits the current `Registration_Mode` value unchanged, THE
   Web_UI SHALL leave the current `Registration_Mode` at that value.
