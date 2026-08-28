# Design — Multi-Tenant Invites, Discord Linking & Per-Guild Source Ownership

## Overview

Extends the web-ui admin/user surface and the bot playback path to support
multi-tenant guilds where each guild's music sources are owned and authorized
per-guild. Builds on the existing `hellodj-core` single-table model, the
Cognito user pool, and AWS Secrets Manager.

## Data Model (hellodj-core single table)

| Entity | PK | SK | GSI1PK | GSI1SK | data |
|--------|----|----|--------|--------|------|
| User profile | `USER#<sub>` | `PROFILE` | `DISCORD#<id>` (if linked) | `USER` | email, discord_id?, discord_linked, invited_by |
| Invite record | `INVITE#<email>` | `INVITE` | `INVITETOKEN#<tokenHash>` | `INVITE` | email, invited_by, token_hash, expires_at, status, created_at, accepted_at? |
| Guild ownership | `GUILD#<gid>` | `OWNER` | — | — | owner_sub |
| Guild admin edge | `GUILD#<gid>` | `ADMIN#<discordId>` | `DISCORD#<discordId>` | `GUILDADMIN#<gid>` | appointed_by, appointed_at |
| Guild source meta | `GUILD#<gid>` | `SOURCE#<provider>` | — | — | connected, connected_at, connected_by |
| Guild config | `GUILD#<gid>` | `CONFIG` | — | — | (existing per-guild config) |

Reverse lookups via GSI1:
- Discord login → user: `query_gsi1("DISCORD#<id>", sk_prefix="USER")`
- Guilds a Discord id administers: `query_gsi1("DISCORD#<id>", sk_prefix="GUILDADMIN#")`
- Invite token → invite: `query_gsi1("INVITETOKEN#<tokenHash>", sk_prefix="INVITE")`
  so `/invite/<token>` resolves the invite by hashed token in one indexed
  lookup without knowing the email.

## Invite Token Lifecycle

The invitation link carries an opaque random token (`secrets.token_urlsafe`).
Only its **SHA-256 hash** is stored (`token_hash`); the raw token lives only in
the email link. On `/invite/<token>`:

1. Hash the token, resolve the invite via GSI1 (`INVITETOKEN#<hash>`).
2. Validate: exists, `status == invited`, `expires_at` in the future.
3. On successful registration, atomically flip `status invited → accepted`
   (conditional write on the current status) so the token is single-use even
   under concurrent requests (R2.5). The winning writer creates the CONFIRMED
   Cognito account; losers get the "used or expired" message.
4. Any invalid/consumed/expired/unknown token renders the fixed message
   "Sorry, this invitation link has been used or has expired!" (R2.3).

The account is created CONFIRMED (`admin_create_user` with
`MessageAction=SUPPRESS` + `admin_set_user_password(..., Permanent=True)`), so
Cognito sends **no** temp-password email — the branded SES invite is the only
email. The user then links Discord (R3) as their login method (R2.4).

## Secrets: Per-Guild Isolation

Per-guild source tokens live in Secrets Manager, one secret per guild+provider:

```
hellodj/<stage>/guild/<guildId>/<provider>
```

- The DynamoDB `SOURCE#<provider>` item holds only **non-secret metadata**
  (connected flag, timestamps). Tokens NEVER touch DynamoDB.
- Isolation is enforced two ways: (1) the naming scheme scopes each guild's
  tokens to its own secret path, and (2) the web-ui checks the caller's
  ownership/admin of the guild BEFORE reading/writing its secret.
- The global `hellodj/<stage>/tidal-refresh` / `spotify` secrets remain as an
  optional Platform_Owner-controlled fallback (R5.5, R6.2).

## Authorization Model

Guild control is Discord-derived:
- A **User** controls a guild if they are its recorded `OWNER`, OR their linked
  Discord id has a `GUILD#<gid>/ADMIN#<discordId>` edge.
- `can_manage_guild(user, guild_id)` is the single gate used by every guild and
  source route. It checks OWNER match or Guild_Admin edge via GSI1.
- The Platform_Owner (`admins` group) can manage any guild (super-admin).

## Components

### web-ui (new/changed modules)

| Module | Responsibility |
|--------|----------------|
| `invite_service.py` | Mint single-use Invite_Token, store invite (token hash + expiry + status), validate/consume token; create CONFIRMED Cognito account on registration |
| `invite_email.py` | Render + send the branded invitation email via SES (verified sender identity per stage) |
| `user_profile.py` | User profile CRUD, Discord link/unlink, GSI1 reverse lookup |
| `guild_admin_service.py` | Guild ownership, appoint/remove Guild_Admins by Discord id, `can_manage_guild` |
| `guild_sources.py` | Per-guild source metadata + Per_Guild_Secret read/write, ownership-gated |
| `pages.py` (extend) | Admin invite route; **public** `/invite/<token>` registration page (GET form + POST register); user guild-management + source-connect routes |
| `auth.py` (extend) | Discord link callback resolves/creates the user↔discord mapping; post-registration Discord-link handoff |

### bot (new module)

| Module | Responsibility |
|--------|----------------|
| `bot/playback/guild_credentials.py` | Resolve a guild's per-provider tokens from Secrets Manager (cached, TTL), fall back to global |

The bot's source resolution (`player.py` / lavasrc config) calls
`guild_credentials.resolve(guild_id, provider)` and injects the resolved token
into the play request / Lavalink node update for that guild.

## Routes (web-ui)

Public (no session required):
- `GET  /invite/<token>` — validate token; render registration form or the
  "used or expired" message
- `POST /invite/<token>` — consume token (single-use), create CONFIRMED account,
  redirect into Discord linking

Admin (Platform_Owner only):
- `POST /admin/invite` — mint token + send branded SES invitation email
- `POST /admin/invite/<email>/resend` — re-send a pending invite (new token)
- `POST /admin/invite/<email>/revoke` — revoke a pending invite
- `GET  /admin/invites` — HTMX partial: invite list with status

User (any authenticated user, gated by `can_manage_guild`):
- `GET  /account` — profile + Discord link status
- `GET  /auth/discord/link` → `GET /auth/discord/link/callback` — link Discord
- `GET  /guilds/<gid>` — guild management (admins + sources)
- `POST /guilds/<gid>/admins` — appoint Guild_Admin by Discord id
- `POST /guilds/<gid>/admins/<discordId>/remove` — remove admin
- `GET  /guilds/<gid>/sources/<provider>/connect` — start per-guild source OAuth
- `GET  /guilds/<gid>/sources/<provider>/callback` — finish + store Per_Guild_Secret
- `POST /guilds/<gid>/sources/<provider>/disconnect` — delete Per_Guild_Secret

## IAM (least privilege)

- web-ui SA: Cognito `AdminCreateUser`/`AdminSetUserPassword` (+ existing admin
  actions), SES `SendEmail`/`SendRawEmail` for the verified sender identity, and
  Secrets Manager
  `CreateSecret`/`PutSecretValue`/`GetSecretValue`/`DeleteSecret`/`DescribeSecret`
  scoped to `arn:aws:secretsmanager:*:*:secret:hellodj/<stage>/guild/*`.
- bot SA: Secrets Manager `GetSecretValue`/`DescribeSecret` scoped to the same
  `hellodj/<stage>/guild/*` prefix (read-only).

## Testing

- Pure logic: `can_manage_guild`, invite dedupe, per-guild secret naming,
  Discord→user resolution — unit tested with fakes (no AWS).
- Invite token lifecycle: mint → validate → consume; a consumed/expired/unknown
  token is rejected with the fixed message; single-use is enforced (only one of
  two concurrent consume attempts succeeds); only the token hash is persisted.
- Property: for any set of guilds and users, `can_manage_guild` grants access
  only to owners/appointed-admins/super-admin; a guild's secret name is unique
  per (guild, provider) and never collides across guilds.
- Isolation test: reading guild B's source with guild A's session is denied.

## Migration / Compatibility

- **Invite flow change (amended):** replaces the earlier Cognito built-in
  invitation (temp-password email + hosted-UI password set) with a single-use
  tokenized link to a HelloDJ-hosted registration page + branded SES email.
  Accounts are now created CONFIRMED (`MessageAction=SUPPRESS` +
  `AdminSetUserPassword Permanent=True`) so Cognito sends no temp-password
  email. Any pending invites created under the old flow should be re-sent under
  the new flow. New infra dependency: a verified SES sender identity per stage.
- Global source secrets remain; per-guild takes precedence when present.
- Existing global config untouched; per-guild config already supported by
  `ConfigStore.get_guild/set_guild`.
