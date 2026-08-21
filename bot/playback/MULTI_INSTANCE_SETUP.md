# Multi-Instance Music Setup

This guide explains how to configure additional Discord bot applications for
multi-channel music playback. Each instance can serve one voice channel at a
time within a guild, allowing simultaneous music in multiple channels.

---

## Overview

Discord limits each bot application to a single voice connection per guild.
To play music in multiple channels simultaneously, HelloDJ supports running
**secondary bot instances** — separate Discord applications that share the
same Lavalink sidecar for audio processing.

The primary bot (the one registered with slash commands) orchestrates
secondary instances transparently. Users interact only with `/play` — the
InstanceOrchestrator assigns an available instance behind the scenes.

---

## Discord Developer Portal Setup

### 1. Create Additional Bot Applications

For each secondary instance you want (up to 10 total):

1. Go to <https://discord.com/developers/applications>
2. Click **New Application**
3. Name it something recognizable (e.g., "HelloDJ #2", "HelloDJ #3")
4. Navigate to the **Bot** section
5. Click **Reset Token** to generate a bot token — **copy it immediately**
6. Under **Privileged Gateway Intents**, enable:
   - **Message Content Intent** — not strictly required for secondary
     instances but recommended for consistency
   - **Server Members Intent** — optional, only if you want presence
7. Note the **Application ID** from the General Information page

> **Important:** Each application has its own token. Do NOT reuse the primary
> bot's token. Discord's Terms of Service require one token per application.

### 2. Configure Bot Permissions

Secondary instances need minimal permissions since they only join voice:

- **Connect** — join voice channels
- **Speak** — transmit audio
- **Use Voice Activity** — optional, for voice-activated features

The invite URL permission integer for these is `3145728`.

### 3. Invite Secondary Bots to Your Guild

For each secondary application, generate an invite URL:

```
https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot&permissions=3145728
```

Replace `<APP_ID>` with the application ID from step 1.

> **Note:** Each secondary bot will appear as a separate member in your guild.
> They do NOT need to be verified unless your bot is in 75+ guilds. For
> private/community servers under that threshold, unverified bots work fine.

### 4. Bot Verification (75+ Guilds)

If your server uses secondary bots across 75+ guilds:

- Each application must go through Discord's bot verification process
  independently
- Submit a verification request per application at
  <https://discord.com/developers/applications/{APP_ID}/verification>
- Verification requires a description of the bot's purpose and why multiple
  applications are needed (multi-channel music is a valid use case)

---

## Storing Credentials

### Via the `/hellodj` Admin Command

The recommended way to configure instances is through the bot's admin
interface:

```
/hellodj instances add token:<BOT_TOKEN> app_id:<APPLICATION_ID> name:HelloDJ #2
/hellodj instances add token:<BOT_TOKEN> app_id:<APPLICATION_ID> name:HelloDJ #3
```

This stores credentials in the encrypted SQLite credential store using
Fernet encryption at rest.

### Via the Web UI

Navigate to **Settings → Instances** at `https://hellodj.celestium.life`
and use the "Add Instance" form.

### Credential Store Keys

The following keys are written to the credential store:

| Key | Description | Example |
|-----|-------------|---------|
| `instance.0.token` | Bot token for instance 0 | `Bot MTIz...` |
| `instance.0.app_id` | Application ID | `1234567890123456789` |
| `instance.0.name` | Display name | `HelloDJ #2` |
| `instance.1.token` | Bot token for instance 1 | `Bot NDU2...` |
| `instance.1.app_id` | Application ID | `9876543210987654321` |
| `instance.1.name` | Display name | `HelloDJ #3` |
| `playback.instance_count` | Total secondary instances | `2` |

### Setting Instance Count

After adding credentials, set the instance count to enable them:

```
/hellodj instances count 2
```

Or via the credential store directly:

```python
from config import cfg
cfg.set("playback.instance_count", "2")
```

---

## Shared Lavalink Sidecar

All bot instances (primary + secondaries) connect to the **same Lavalink
sidecar** running in the HelloDJ pod. Lavalink natively supports multiple
client sessions — each instance gets its own wavelink session ID but shares:

- Audio decoding infrastructure
- Plugin suite (youtube-source, lavasrc, etc.)
- YouTube OAuth credentials (pushed by primary bot)
- PoToken (pushed by primary bot)

No additional Lavalink configuration is needed for multi-instance. The
`InstanceOrchestrator` creates separate `wavelink.Node` connections for
each secondary client using the same `lavalink.host` / `lavalink.port` /
`lavalink.password` credentials.

---

## Configuration Keys

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `playback.instance_count` | int (0–10) | `0` | Number of secondary instances configured |
| `playback.legacy_video_enabled` | bool | `true` | Whether `/video` commands are active with deprecation notices |

---

## Discord Terms of Service Compliance

Multi-instance operation is ToS-compliant when:

1. **Each application has its own token** — never share tokens between apps
2. **Each application is a separate entry** in the Developer Portal
3. **Bot automation rules** are followed — no self-botting, no user tokens
4. **Rate limits** are respected per-application — each instance has its own
   rate limit bucket
5. **Intent usage** is justified — secondary instances should only request
   intents they actually use

Discord explicitly supports bot sharding and multi-application architectures
for music bots. The key constraint is: one bot token = one application = one
set of rate limits.

---

## Troubleshooting

### Instance shows "unhealthy"

- Verify the token is valid (hasn't been reset in the Developer Portal)
- Check that the bot has been invited to the guild
- Ensure the bot has Connect + Speak permissions in the target channel

### "All music slots are in use"

- Each instance can only be in one voice channel per guild at a time
- Wait for a channel to finish playing, or add more instances (up to 10)
- Use `/hellodj status` to see which instances are assigned where

### Instance won't connect

- Secondary bots need to be guild members — invite them first
- Check for 2FA requirements on the guild that might block bot joins
- Verify the application ID matches the token (mismatched = auth failure)

### Credentials not saving

- Ensure `HELLODJ_DB_KEY` environment variable is set (encryption key)
- Verify the data volume is writable (`/app/data/hellodj.db`)
- Check bot logs for credential store errors
