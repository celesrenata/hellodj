# Requirements — Multi-Tenant Invites, Discord Linking & Per-Guild Source Ownership

## Introduction

Today the HelloDJ AWS platform has a single global configuration and global
source credentials (one Tidal token, one Spotify secret, etc.), and only the
Platform_Owner (Cognito `admins` group) can sign in. This feature turns HelloDJ
into a multi-tenant SaaS:

- The Platform_Owner **invites** new users by email (Cognito-managed invite).
- An invited user **verifies** their email and sets a password, then logs in.
- Once in, a user can **link Discord OAuth** to bypass password login thereafter.
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
- **User**: An invited, verified account (Cognito user). Owns/administers one or
  more guilds and their sources.
- **Guild_Admin**: A Discord user id appointed by a User to administer a guild.
- **Guild**: A Discord server the bot serves, identified by its Discord guild id.
- **Source**: A music provider — `youtube`, `youtube_music`, `tidal`, `spotify`.
- **Per_Guild_Secret**: A Secrets Manager entry holding one guild's OAuth tokens
  for one source, isolated from all other guilds.

## Requirements

### Requirement 1: Email invite flow (Platform_Owner)

**User Story:** As the Platform_Owner, I want to invite a new user by email so
they can create an account without me sharing a password.

#### Acceptance Criteria
1. WHEN the Platform_Owner submits an email in the admin panel, THE system SHALL
   create a Cognito user for that email and trigger Cognito's invitation email
   containing a temporary password.
2. THE system SHALL record the invite (email, invited-by, timestamp, status) so
   the admin panel can list pending and accepted invites.
3. WHEN an invited user first authenticates with the temporary password, THE
   system SHALL require them to set a permanent password (Cognito
   FORCE_CHANGE_PASSWORD flow).
4. THE admin panel SHALL show each account's invite status (invited / verified).
5. IF an email is already invited, THE system SHALL NOT create a duplicate and
   SHALL surface a clear message.

### Requirement 2: Email verification then login

**User Story:** As an invited user, I want to verify my email and log in.

#### Acceptance Criteria
1. WHEN an invited user completes the Cognito verification + password set, THE
   system SHALL mark their account CONFIRMED.
2. WHEN a CONFIRMED user signs in through Cognito, THE system SHALL establish an
   authenticated session bound to their Cognito subject id.
3. A non-verified account SHALL NOT be able to reach any authenticated route.

### Requirement 3: Discord OAuth linking (bypass password login)

**User Story:** As a logged-in user, I want to link my Discord account so I can
log in with Discord from then on.

#### Acceptance Criteria
1. WHEN a logged-in user initiates Discord linking, THE system SHALL run the
   Discord OAuth flow and store the mapping Discord-user-id → Cognito-subject.
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
1. THE web-ui service role SHALL be granted only the Cognito admin actions and
   the Secrets Manager actions scoped to `hellodj/<stage>/guild/*` it needs.
2. THE bot service role SHALL be granted read-only access to per-guild source
   secrets under `hellodj/<stage>/guild/*`.
3. NO static credentials SHALL be embedded; all AWS access is via IRSA.
