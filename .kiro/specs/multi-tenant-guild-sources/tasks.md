# Implementation Plan — Multi-Tenant Invites, Discord Linking & Per-Guild Source Ownership

## Overview

These tasks reflect the amended invite flow: a single-use tokenized invitation
link to a HelloDJ-hosted registration page + a branded SES email, replacing the
old Cognito temp-password invitation. Tasks are ordered so each builds on the
previous and every step ends in runnable, tested code. Requirements 3–6 were
largely built already; those tasks verify the existing implementation against
the amended spec and fill gaps rather than build from scratch.

Gate commands (must pass before a task is considered done):

- `cd platform/components/web-ui && ruff check --target-version py314 . && python3 -m pytest tests/ -q`
- `python3 platform/tools/check_line_count.py platform/components/web-ui` (500-line ceiling)
- Infra tasks: `cd platform/infra && npx tsc --noEmit && npx jest`

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": [1], "dependsOn": [] },
    { "wave": 2, "tasks": [2], "dependsOn": [1] },
    { "wave": 3, "tasks": [3], "dependsOn": [2] },
    { "wave": 4, "tasks": [4, 5, 7], "dependsOn": [3] },
    { "wave": 5, "tasks": [6, 8], "dependsOn": [5, 7] },
    { "wave": 6, "tasks": [9], "dependsOn": [6, 8] },
    { "wave": 7, "tasks": [10], "dependsOn": [8] },
    { "wave": 8, "tasks": [11], "dependsOn": [10] },
    { "wave": 9, "tasks": [12], "dependsOn": [11] },
    { "wave": 10, "tasks": [13], "dependsOn": [12] },
    { "wave": 11, "tasks": [14, 16], "dependsOn": [9] },
    { "wave": 12, "tasks": [15], "dependsOn": [14] }
  ]
}
```

## Tasks

### Invite token model + service

- [x] 1. Add the Invite_Token data model to `invite_service.py`
  - Mint an opaque token with `secrets.token_urlsafe`; store only its SHA-256
    hash (`token_hash`), plus `email`, `invited_by`, `expires_at`
    (configurable TTL, default 7 days), and `status` (`invited`).
  - Write the invite via `CoreTable.put_new` with
    `gsi1pk=INVITETOKEN#<hash>`, `gsi1sk=INVITE` for token lookup.
  - Reject duplicates: existing pending invite OR already-registered email (R1.5).
  - _Requirements: 1.1, 1.2, 1.3, 1.5_

- [x] 2. Add token validation + single-use consume to `invite_service.py`
  - `resolve_by_token(raw_token)`: hash → `CoreTable.query_gsi1("INVITETOKEN#<hash>", sk_prefix="INVITE")`; return the invite only if `status == invited` and `expires_at` is in the future.
  - `consume(raw_token)`: use `CoreTable.update_with_lock` with a mutator that
    flips `invited → accepted` (sets `accepted_at`), guaranteeing single-use
    under concurrency via the optimistic-lock version condition; raise/return a
    clear "already used or expired" outcome on any other status.
  - _Requirements: 2.2, 2.3, 2.5_

- [x] 3. Create CONFIRMED Cognito account on registration (no temp password)
  - In `invite_service.py`, `register(raw_token, ...)`: after a successful
    `consume`, create the account with `admin_create_user`
    (`MessageAction=SUPPRESS`, UUID username, email attribute + `email_verified`)
    then `admin_set_user_password(..., Permanent=True)` so Cognito sends no email.
  - Persist the user profile (`UserProfileService.ensure`) bound to the Cognito
    subject; record `invited_by`.
  - _Requirements: 2.2, 2.6_

- [x] 4. Unit-test the invite token lifecycle
  - Fakes for Cognito + a fake `CoreTable`: mint → resolve → consume → register.
  - Assert: only the hash is stored (raw token never persisted); consumed /
    expired / unknown tokens are rejected with the fixed outcome; two concurrent
    `consume` calls yield exactly one success (single-use).
  - _Requirements: 1.2, 2.3, 2.5_

### Branded invitation email (SES)

- [x] 5. Implement `invite_email.py` (SES sender)
  - Render a branded HTML + text invitation containing
    `<PUBLIC_BASE_URL>/invite/<raw_token>`; send via SES `send_email` from the
    stage's verified sender identity (`INVITE_SENDER` config).
  - The raw token appears only in the link; never log it.
  - Degrade gracefully (surface an error to the admin panel) if SES is not
    configured, without leaving a half-created invite.
  - _Requirements: 1.1, 7.4_

- [x] 6. Wire `invite_service.invite()` to send the email
  - After recording the invite, call `invite_email.send(...)`; on send failure,
    revoke/rollback the just-created invite record so a retry is clean.
  - Unit-test with a fake SES client (assert recipient, link contains the raw
    token, sender is the configured identity).
  - _Requirements: 1.1, 1.2_

### Public registration route

- [x] 7. Add the public `/invite/<token>` GET route in `pages.py`
  - No session required. Resolve the token; if valid, render a HelloDJ-hosted
    registration page bound to the invite's email (email shown read-only).
  - If invalid/consumed/expired/unknown, render exactly:
    "Sorry, this invitation link has been used or has expired!" (R2.3).
  - Add the registration + used/expired templates under `templates/pages/`.
  - _Requirements: 2.1, 2.3_

- [x] 8. Add the public `/invite/<token>` POST route in `pages.py`
  - Call `invite_service.register(...)`; on success redirect into Discord
    linking (`/auth/discord/link`); the registration link itself grants no
    lasting session (R2.4).
  - On a token that lost the single-use race or expired mid-flow, show the
    used/expired message.
  - _Requirements: 2.2, 2.4, 2.5_

- [x] 9. Admin invite management routes in `pages.py`
  - `POST /admin/invite` (mint + send), `POST /admin/invite/<email>/resend`
    (new token + resend), `POST /admin/invite/<email>/revoke`, and the
    `GET /admin/invites` HTMX partial showing status (invited/accepted/expired).
  - Update `templates/pages/admin.html` + the invite-list partial.
  - _Requirements: 1.2, 1.4_

### Discord linking as primary login (already partially built — verify/extend)

- [x] 10. Confirm the post-registration Discord-link handoff in `auth.py`
  - Ensure `/auth/discord/link` → `/auth/discord/link/callback` links the new
    user's Discord id (via `UserProfileService.link_discord`, GSI1 reverse
    index) and that Discord OAuth establishes the session thereafter (R3.2).
  - Add/extend unit tests for the one-account-per-Discord-identity rule (R3.4).
  - _Requirements: 3.1, 3.2, 3.3, 3.4_

### Per-guild admins & sources (verify existing implementation against spec)

- [x] 11. Verify guild admin appointment by Discord id
  - Confirm `guild_admin_service.py` persists/removes Guild_Admin edges, gates
    on `can_manage_guild`, and that an appointed admin gains source management
    once their Discord account is linked (R4.4). Add tests for any gap.
  - _Requirements: 4.1, 4.2, 4.3, 4.4_

- [x] 12. Verify per-guild source isolation
  - Confirm `guild_sources.py` stores tokens only in
    `hellodj/<stage>/guild/<gid>/<provider>` (metadata-only in DynamoDB),
    ownership-gated read/write, and connect/disconnect per provider.
  - Property test: secret name is unique per (guild, provider), never collides;
    reading guild B's source with guild A's session is denied.
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5_

- [x] 13. Verify bot per-guild credential resolution
  - Confirm `bot/playback/guild_credentials.py` resolves a guild's per-provider
    tokens (cached, bounded TTL), falls back to global, and never leaks across
    guilds. Add tests for cache TTL + fallback + isolation.
  - _Requirements: 6.1, 6.2, 6.3, 6.4_

### Infrastructure (CDK — deploy via `cdk deploy`, not a CodeCommit push)

- [x] 14. Add SES sender identity + web-ui SES permission
  - In the infra stack, add/verify a stage SES sender identity and grant the
    web-ui service account (IRSA) `ses:SendEmail`/`ses:SendRawEmail` for that
    identity, plus Cognito `AdminSetUserPassword` (added to existing admin
    actions).
  - Inject `INVITE_SENDER`, `INVITE_TOKEN_TTL`, and `PUBLIC_BASE_URL` into the
    web-ui container env.
  - _Requirements: 7.1, 7.4_

- [x] 15. Update CDK tests for the new IAM + env
  - Assert the web-ui role has SES send scoped to the sender identity and
    `AdminSetUserPassword`; assert the new env vars are wired.
  - _Requirements: 7.1, 7.4_

### Migration

- [x] 16. Migrate/clear stale pending invites
  - Any invites created under the old Cognito temp-password flow (no
    `token_hash`) are re-sent under the new flow or marked `expired`; document
    the one-time step in the deploy notes.
  - _Requirements: 1.2, 1.4_

## Notes

- Deployment follows the pipeline rules: component source changes (web-ui `*.py`,
  templates) take effect on a CodeCommit push; CDK/IAM changes (tasks 14–15)
  require `cd platform/infra && npx cdk deploy <stack>` and do NOT deploy via a
  push. After the pipeline pushes a new web-ui image, roll it out with
  `kubectl rollout restart deploy/web-ui -n hellodj-<stage>`.
- SES starts in sandbox mode per account/region; sending to arbitrary invitee
  addresses requires production access (or verified recipients for testing).
  Flag this before task 5 if beta invites go to unverified external addresses.
- The single-use guarantee relies on `CoreTable.update_with_lock` optimistic
  locking (status `invited → accepted`), not a DynamoDB TTL; expiry is checked
  in application code against `expires_at`.
