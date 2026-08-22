# Authentication & Security

## YouTube Authentication

YouTube requires multiple authentication mechanisms to avoid rate limiting and bot detection.

### OAuth (TV Client)

The TV client is the only one that supports OAuth. A refresh token is stored in the credential DB and pushed to Lavalink at startup.

**Flow:**
```
1. Bot startup → push_youtube_oauth()
2. POST /youtube to Lavalink with:
   - refreshToken (from creds DB)
   - poToken + visitorData (from creds DB)
3. Lavalink exchanges refresh token for access token via Google OAuth2
4. Access token used for all TV client requests
```

**Key rule:** OAuth + PoToken must be sent in a SINGLE POST request. The youtube-source plugin replaces ALL fields on each call.

### PoToken (Proof of Origin)

Defeats YouTube's "Sign in to confirm you're not a bot" for WEB-family clients.

**Generation:** bgutil-ytdlp-pot-provider (in-cluster at port 4416)

**Refresh cycle:**
```
1. _potoken_refresh_task() runs every POTOKEN_REFRESH_INTERVAL (1 hour)
2. POST /get_pot to potoken-server:4416
3. Response: {poToken, contentBinding, expiresAt}
4. Store in creds DB: youtube.pot_token, youtube.pot_visitor_data
5. Re-push full auth payload to Lavalink (OAuth + PoToken together)
```

**Graceful degradation:** If potoken-server is unavailable, task logs debug message and skips. TVHTML5_SIMPLY and ANDROID_VR work without PoToken on clean IPs.

### Remote Cipher (yt-cipher)

Offloads YouTube player-script signature deciphering:
- URL: `http://yt-cipher.hellodj-service.svc.cluster.local:8001`
- Auth: API_TOKEN (shared between yt-cipher env and Lavalink config)
- Without this, plugin falls back to local cipher extraction (fragile)

### YouTube Client Cascade

```
1. TV           — OAuth-capable, primary streaming client
2. TVHTML5_SIMPLY — Robust unauthenticated, works on clean IPs
3. ANDROID_VR   — Unauthenticated streaming fallback
4. MUSIC        — Search only (playback: false, videoLoading: false)
5. WEB          — Metadata/playlist only (playback: false)
```

### SABR (Server Adaptive Bitrate)

YouTube now serves ONLY SABR streams to WEB clients. The official youtube-plugin does NOT support SABR. A custom `youtube-plugin-sabr.jar` is baked into the Lavalink image.

## Tidal Authentication

### Token Refresh

Tidal access tokens expire every ~4 hours. The bot refreshes them:

```
1. _token_refresh_watchdog() runs every 5 minutes
2. refresh_tidal_token():
   a. Check expires_at (skip if still valid with 5min buffer)
   b. POST to https://auth.tidal.com/v1/oauth2/token
      - grant_type: refresh_token
      - refresh_token: from creds DB
      - client_id: issuing_client_id or "6BDSRdpK9hqEBTgU" (tidalapi PKCE)
   c. On 401: retry with developer portal client_id as fallback
   d. Store new access_token, refresh_token, expires_at
3. push_tidal_token():
   - PATCH /v4/lavasrc/config with {"tidal": {"token": "..."}}
   - LavasRC's static-token mode doesn't self-refresh
```

**Important:** The refresh token may have been issued by tidalapi's PKCE client (`6BDSRdpK9hqEBTgU`). The refresh request MUST use the same client_id.

### Tidal Stream Sidecar

The tidal-stream sidecar reads OAuth tokens from the shared data PVC (`/app/data`). If tokens expire, re-auth via the web UI's `/auth/tidal/login` flow.

## Guild Authorization Policy

### Activation Keys

Guilds must be activated before commands work:
- `/activate <key>` command (always passes permission check)
- Stores `guild.<id>.activated = "true"` in creds DB
- Checked on every interaction

### Guild Approval

New guilds enter "pending" state. Must be approved via admin portal:

```
Join → "pending" (commands disabled, notification sent)
  │
  ├─ Admin approves → "approved" (commands enabled)
  ├─ 24 hours pass → "denied" (bot leaves automatically)
  └─ Admin denies → "denied" (bot leaves)
```

**Admin determination:**
- `BOT_OWNER_ID` from config
- OAuth-bound admins from `oauth_store.get_admin_ids()`

### Stored Policy

`data/guild_policy.json`:
```json
{
  "1501686893765595296": {
    "status": "approved",
    "reason": "approved by administrator",
    "checked_at": 1724100000,
    "name": "Under The Influence"
  }
}
```

## Content Security

### Credential Store Encryption

- All values encrypted at rest with Fernet (AES-128-CBC)
- Thread-safe access via thread-local SQLite connections
- WAL mode for concurrent read/write
- Auto-detection of read-only filesystems (init container)

### Discord Token Protection

- Token read from creds DB, never logged
- `DISCORD_TOKEN` env var feeds the k8s secret → bot container
- Gateway connection uses the token from creds DB (not env var)

### Volume Security

- `/app/data` PVC: read-write for bot, read-only for init container
- NFS mounts: config (logs), models (read-only), backups
- `fsGroup: 1000` ensures consistent ownership
- HLS temp uses memory-backed emptyDir (no disk persistence)

### Network Security

- All inter-container communication is localhost (pod network)
- External services use in-cluster DNS (no internet exposure)
- Lavalink password: "youshallnotpass" (pod-internal only)
- yt-cipher: API_TOKEN shared secret
- Activity backend: Discord instance_id as auth token
