# Requirements — Multi-Tenant Invites, Discord Linking & Per-Guild Source Ownership

## Introduction

Today the HelloDJ AWS platform has a single global configuration and global
source credentials (one Tidal token, one Spotify secret, etc.), and only the
Platform_Owner (Cognito `admins` group) can sign in. This feature turns HelloDJ
into a multi-tenant SaaS:

- The Platform_Owner **invites** new users by email. The email carries a
  **single-use invitation link** to a HelloDJ-hosted registration page.
- An invited user **opens the link, registers**, and their account is created.
  The link **burns after one use** (or on expiry) and shows a clear
  "already used or expired" message on any subsequent visit.
- A registered user **logs in via Discord OAuth** (the primary login method);
  they link their Discord identity as part of / immediately after registration.
- A user **appoints other users by Discord user id** to administer a specific
  guild they control.
- Each guild's music **sources (YouTube, YouTube Music, Tidal, Spotify) are
  owned and authorized per-guild by that guild's users** — never globally. One
  guild's OAuth tokens are isolated from every other guild.
- The bot/Lavalink playback path **resolves per-guild source credentials at
  play time**, so a track played in guild A uses guild A's Tidal/Spotify/YT
  auth, and guild B uses guild B's.

## Glossary

- **Platform_Owner**: The super-admin (Cognito `admins` group). Invites users,
  manages all accounts.
- **User**: An invited, registered account (Cognito user). Owns/administers one
  or more guilds and their sources.
- **Invite_Token**: An opaque, single-use, time-limited token embedded in the
  invitation link. Bound to one email; consumed on successful registration.
- **Guild_Admin**: A Discord user id appointed by a User to administer a guild.
- **Guild**: A Discord server the bot serves, identified by its Discord guild id.
- **Source**: A music provider — `youtube`, `youtube_music`, `tidal`, `spotify`.
- **Per_Guild_Secret**: A Secrets Manager entry holding one guild's OAuth tokens
  for one source, isolated from all other guilds.

## Requirements

### Requirement 1: Email invite flow with single-use link (Platform_Owner)

**User Story:** As the Platform_Owner, I want to invite a new user by email so
they receive a branded invitation with a single-use link to register, without
me sharing any password.

#### Acceptance Criteria
1. WHEN the Platform_Owner submits an email in the admin panel, THE system SHALL
   generate an opaque, single-use, time-limited Invite_Token bound to that
   email and send a branded invitation email (via SES) containing a link of the
   form `<public-base>/invite/<token>`.
2. THE system SHALL record the invite (email, invited-by, timestamp, token hash,
   expiry, status ∈ {invited, accepted, expired, revoked}) so the admin panel
   can list pending and accepted invites.
3. THE Invite_Token SHALL expire after a configurable TTL (default 7 days);
   after expiry it SHALL be treated as invalid.
4. THE admin panel SHALL show each account's invite status (invited / accepted /
   expired) and allow re-sending or revoking a pending invite.
5. IF an email is already invited (pending) or already registered, THE system
   SHALL NOT create a duplicate and SHALL surface a clear message.

### Requirement 2: Registration via the invite link, then Discord login

**User Story:** As an invited user, I want to open my invitation link, register,
and then log in.

#### Acceptance Criteria
1. WHEN an invitee opens `/invite/<token>` with a valid, unused, unexpired
   token, THE system SHALL render a HelloDJ-hosted registration page bound to
   the invite's email.
2. WHEN the invitee completes registration, THE system SHALL create a CONFIRMED
   Cognito account for the invite's email (no temporary password) and mark the
   Invite_Token consumed (status `accepted`).
3. WHEN a token is already consumed, expired, revoked, or unknown, THE system
   SHALL NOT render the registration form and SHALL show:
   "Sorry, this invitation link has been used or has expired!"
4. AFTER successful registration, THE system SHALL direct the user to link their
   Discord account and SHALL treat Discord OAuth as their login method going
   forward (R3); the registration link itself SHALL NOT grant a further session.
5. A token SHALL be usable at most once (single-use); concurrent attempts to
   consume the same token SHALL result in exactly one success.
6. AN account that has not completed registration SHALL NOT be able to reach any
   authenticated route.

### Requirement 3: Discord OAuth linking (primary login for users)

**User Story:** As a registered user, I want to link my Discord account so I log
in with Discord from then on (my primary login method).

#### Acceptance Criteria
1. WHEN a registered user initiates Discord linking (during or after
   registration), THE system SHALL run the Discord OAuth flow and store the
   mapping Discord-user-id → Cognito-subject.
2. WHEN a user with a linked Discord account signs in via Discord OAuth, THE
   system SHALL resolve their Cognito account and establish the session without
   a password.
3. THE Discord id → user mapping SHALL be reverse-indexed (GSI1) so a Discord
   login is a single indexed lookup.
4. A Discord identity SHALL link to at most one user account.

### Requirement 4: Appoint guild admins by Discord id

**User Story:** As a user, I want to appoint other people by Discord user id to
administer a guild I control.

#### Acceptance Criteria
1. WHEN a user adds a Discord user id as an admin of a guild they own, THE system
   SHALL persist a Guild_Admin edge (guild, discord id, appointed-by).
2. THE system SHALL list a guild's admins and allow removing them.
3. A user SHALL only appoint/remove admins for guilds they own or administer.
4. A Guild_Admin (by Discord id) SHALL be authorized to manage that guild's
   sources through the panel once they link their Discord account.

### Requirement 5: Per-guild source ownership & OAuth (isolation)

**User Story:** As a guild's user, I want to connect YouTube, YouTube Music,
Tidal, and Spotify for **my** guild, isolated from every other guild.

#### Acceptance Criteria
1. THE system SHALL store each guild's source OAuth tokens in a Per_Guild_Secret
   keyed by guild id and provider, isolated from other guilds.
2. A user SHALL only read/write a guild's source credentials for guilds they own
   or administer; a request for a guild they don't control SHALL be denied.
3. Source connection state SHALL be per-guild — connecting Tidal for guild A
   SHALL NOT affect guild B.
4. THE panel SHALL show per-guild, per-provider connection status and allow
   connect/disconnect per provider.
5. NO global source credential SHALL be used for a guild that has its own
   Per_Guild_Secret; the global secrets remain only as an optional fallback the
   Platform_Owner controls.

### Requirement 6: Bot/Lavalink per-guild source resolution

**User Story:** As a listener, I want playback in my guild to use my guild's own
source authorization.

#### Acceptance Criteria
1. WHEN the bot resolves a track for a guild, THE system SHALL load that guild's
   Per_Guild_Secret for the relevant provider.
2. IF a guild has no credential for a provider, THE system SHALL fall back to
   the global credential (if present) or skip that provider gracefully.
3. Per-guild resolution SHALL NOT leak one guild's tokens to another guild's
   playback.
4. Credential resolution SHALL be cached per-guild with a bounded TTL and
   refreshed on expiry.

### Requirement 7: Least-privilege access

#### Acceptance Criteria
1. THE web-ui service role SHALL be granted only the Cognito admin actions, the
   SES `SendEmail`/`SendRawEmail` action for the verified sender identity, and
   the Secrets Manager actions scoped to `hellodj/<stage>/guild/*` it needs.
2. THE bot service role SHALL be granted read-only access to per-guild source
   secrets under `hellodj/<stage>/guild/*`.
3. NO static credentials SHALL be embedded; all AWS access is via IRSA.
4. Invitation emails SHALL be sent from a verified SES identity (domain or
   address) for the stage; the raw Invite_Token SHALL appear only in the email
   link and never be logged or stored in plaintext (store a hash).
