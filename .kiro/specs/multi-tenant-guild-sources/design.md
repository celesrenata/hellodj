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
| Invite record | `INVITE#<email>` | `INVITE` | — | — | email, invited_by, status, created_at |
| Guild ownership | `GUILD#<gid>` | `OWNER` | — | — | owner_sub |
| Guild admin edge | `GUILD#<gid>` | `ADMIN#<discordId>` | `DISCORD#<discordId>` | `GUILDADMIN#<gid>` | appointed_by, appointed_at |
| Guild source meta | `GUILD#<gid>` | `SOURCE#<provider>` | — | — | connected, connected_at, connected_by |
| Guild config | `GUILD#<gid>` | `CONFIG` | — | — | (existing per-guild config) |

Reverse lookups via GSI1:
- Discord login → user: `query_gsi1("DISCORD#<id>", sk_prefix="USER")`
- Guilds a Discord id administers: `query_gsi1("DISCORD#<id>", sk_prefix="GUILDADMIN#")`

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
| `invite_service.py` | Cognito `admin_create_user` invite (+ email), invite tracking in core table |
| `user_profile.py` | User profile CRUD, Discord link/unlink, GSI1 reverse lookup |
| `guild_admin_service.py` | Guild ownership, appoint/remove Guild_Admins by Discord id, `can_manage_guild` |
| `guild_sources.py` | Per-guild source metadata + Per_Guild_Secret read/write, ownership-gated |
| `pages.py` (extend) | Admin invite route; user guild-management + source-connect routes |
| `auth.py` (extend) | Discord link callback resolves/creates the user↔discord mapping |

### bot (new module)

| Module | Responsibility |
|--------|----------------|
| `bot/playback/guild_credentials.py` | Resolve a guild's per-provider tokens from Secrets Manager (cached, TTL), fall back to global |

The bot's source resolution (`player.py` / lavasrc config) calls
`guild_credentials.resolve(guild_id, provider)` and injects the resolved token
into the play request / Lavalink node update for that guild.

## Routes (web-ui)

Admin (Platform_Owner only):
- `POST /admin/invite` — invite a user by email
- `GET  /admin/invites` — HTMX partial: invite list

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

- web-ui SA: Cognito `AdminCreateUser` (+ existing admin actions), Secrets Manager
  `CreateSecret`/`PutSecretValue`/`GetSecretValue`/`DeleteSecret`/`DescribeSecret`
  scoped to `arn:aws:secretsmanager:*:*:secret:hellodj/<stage>/guild/*`.
- bot SA: Secrets Manager `GetSecretValue`/`DescribeSecret` scoped to the same
  `hellodj/<stage>/guild/*` prefix (read-only).

## Testing

- Pure logic: `can_manage_guild`, invite dedupe, per-guild secret naming,
  Discord→user resolution — unit tested with fakes (no AWS).
- Property: for any set of guilds and users, `can_manage_guild` grants access
  only to owners/appointed-admins/super-admin; a guild's secret name is unique
  per (guild, provider) and never collides across guilds.
- Isolation test: reading guild B's source with guild A's session is denied.

## Migration / Compatibility

- Global source secrets remain; per-guild takes precedence when present.
- Existing global config untouched; per-guild config already supported by
  `ConfigStore.get_guild/set_guild`.
