# Implementation Plan: HelloDJ SaaS Platform

## Overview

Transform HelloDJ from a single-instance personal Discord music bot into a multi-tenant SaaS platform. Implementation proceeds in layers: infrastructure (PostgreSQL schema, Redis), core services (credential store, auth, subscriptions, payments), user-facing features (web portal, web player, admin panel), orchestration (bot pods, GPU scheduling, feature gating), enhanced Discord controls, and finally data migration from SQLite.

## Tasks

- [x] 1. Database schema and infrastructure setup
  - [x] 1.1 Create PostgreSQL schema migration script
    - Create `scripts/migrate_schema.py` that idempotently creates the `hellodj` database and user in the CNPG cluster
    - Create all tables: `credentials`, `tenants`, `subscriptions`, `bot_instances`, `payments`, `trial_applications`, `sessions`, `playlists`
    - Add all CHECK constraints, foreign keys (ON DELETE RESTRICT), unique constraints, and indexes as defined in the design
    - Use `asyncpg` for connection; support `HELLODJ_PG_URI` env var for connection string
    - Script must be idempotent (IF NOT EXISTS patterns)
    - _Requirements: 1.1, 1.2, 2.1, 2.2, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

  - [x] 1.2 Deploy Redis 7.x manifests
    - Create `kube/redis/` directory with: namespace.yaml, statefulset.yaml, service.yaml, configmap.yaml
    - Deploy Redis 7.x in `redis-service` namespace with persistence (RDB + AOF)
    - Create headless service at `redis.redis-service.svc.cluster.local:6379`
    - Configure `maxmemory-policy allkeys-lru` for session/cache eviction
    - _Requirements: 4.5 (session store), 10.4 (heartbeats), 13.6 (pub/sub), 17.2 (WebSocket state)_

- [x] 2. Credential Store (PostgreSQL-backed)
  - [x] 2.1 Implement PostgreSQL-backed CredentialStore class
    - Create `bot/credential_store_pg.py` with identical public API to `bot/credentials.py`
    - Use `asyncpg` connection pool (min=2, max=10) with health checks
    - Preserve Fernet encryption: SHA-256 of `HELLODJ_DB_KEY` → base64 → Fernet key
    - Implement exponential backoff retry (1s, 2s, 4s, 8s, max 30s, 5 attempts) on connection loss
    - Implement row-level locking (`SELECT ... FOR UPDATE`) on writes for concurrent access safety
    - Raise `RuntimeError` if `HELLODJ_DB_KEY` is missing/empty at init
    - Provide sync wrappers for the same API surface (`get`, `set`, `delete`, `get_prefix`, `get_bool`, `get_int`, `get_float`, `exists`, `keys`)
    - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8_

  - [x] 2.2 Write property test for credential encryption round-trip
    - **Property 1: Credential Encryption Round-Trip**
    - For arbitrary string values and keys, `set(key, value)` then `get(key)` returns original value unchanged; raw bytes in DB do not contain plaintext as substring
    - **Validates: Requirements 1.3, 1.5**

  - [x] 2.3 Write property test for credential store API behavioral equivalence
    - **Property 2: Credential Store API Behavioral Equivalence**
    - For any sequence of operations with arbitrary valid inputs, PostgreSQL-backed store produces identical results to SQLite-backed store with same key
    - **Validates: Requirements 1.6**

- [x] 3. Checkpoint - Ensure credential store tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Auth Service (Discord OAuth2)
  - [x] 4.1 Implement Discord OAuth2 auth blueprint
    - Create `web-ui/blueprints/auth.py` with Flask Blueprint
    - Implement `GET /auth/login` — redirect to Discord OAuth2 with `identify` + `email` scopes, cryptographically random `state` (128-bit) stored in Redis
    - Implement `GET /auth/callback` — validate state, exchange code (10s timeout), fetch profile, UPSERT tenant, create session token (128-bit random) in Redis with 7-day TTL
    - Implement `POST /auth/logout` — delete session from Redis, clear cookie
    - Implement `GET /auth/me` — return current tenant profile JSON
    - Handle error cases: state mismatch → `?error=state_mismatch`, code exchange fail → `?error=service_unavailable`, user denies → `?error=denied`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_

  - [x] 4.2 Implement session middleware and auth decorators
    - Create `web-ui/auth_middleware.py` with `@login_required` and `@operator_required` decorators
    - Session lookup via Redis (`session:{token}` key)
    - On expired/invalid token: redirect to login, discard session from Redis
    - Operator check: compare `tenant.discord_user_id` against configured operator ID
    - Return-to-URL handling: store originally requested route, redirect after auth
    - _Requirements: 4.5, 4.6, 9.1, 12.3_

  - [x] 4.3 Write unit tests for OAuth2 flow and session management
    - Test state generation and CSRF validation
    - Test callback error handling (state mismatch, code exchange failure, denial)
    - Test session creation, lookup, expiry, and invalidation
    - Test operator authorization check
    - _Requirements: 4.1, 4.2, 4.6, 4.7, 9.1_

- [x] 5. Subscription Manager
  - [x] 5.1 Implement subscription manager service
    - Create `web-ui/services/subscription_manager.py`
    - Define plans: Base ($6.99/mo, 1 bot, audio only), Trial (free, 30 days, audio only)
    - Define addons: Video (+$1.99), Premium (+$1.99), Additional Bot (+$1.99 per instance, max 9)
    - Implement `create_subscription(tenant_id, plan, addons)` → status `pending_payment`
    - Implement `activate(subscription_id)` → set status `active`, trigger bot provisioning
    - Implement `expire(subscription_id)` → set status `expired`, deactivate bot instances after 3-day grace
    - Implement `cancel(subscription_id)` → set status `cancelled`
    - Auto-cancel if payment not verified within 24 hours
    - Reject addon subscriptions without active Base_Plan
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 7.9, 7.10_

  - [x] 5.2 Implement trial manager
    - Create `web-ui/services/trial_manager.py`
    - Implement `apply(tenant_id)` → create trial_application with status `pending`
    - Implement `approve(application_id, decided_by)` → activate 30-day trial with Base_Plan features
    - Implement `deny(application_id, decided_by)` → set status `rejected`
    - Reject if tenant already has active trial or subscription
    - Auto-expire trials at 30 days from activation
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_

  - [x] 5.3 Implement feature flag computation and API
    - Create `web-ui/services/feature_flags.py`
    - Compute flags from plan + addons: Base → `audio` only; Video_Addon → `video`, `activity`, `hls`, `visualizer`; Premium_Addon → `tidal_hifi`, `lossless`, `priority_queue`
    - Expose `GET /api/v1/features/{tenant_id}` endpoint returning JSON feature flags
    - Cache feature flags in Redis (`features:{tenant_id}`, 5-min TTL)
    - Publish to Redis pub/sub `feature_change:{tenant_id}` on subscription changes
    - _Requirements: 13.1, 13.2, 13.3, 13.6_

  - [x] 5.4 Write property test for trial lifecycle state machine
    - **Property 4: Trial Lifecycle State Machine**
    - Approving a trial sets expiry to exactly 30 days from approval; applying with existing active trial/subscription is rejected
    - **Validates: Requirements 6.3, 6.4, 6.5**

  - [x] 5.5 Write property test for subscription timeout lifecycle
    - **Property 5: Subscription Timeout Lifecycle**
    - Unverified payment within 24h → cancelled; expired subscription transitions only after 3-day grace period
    - **Validates: Requirements 7.7, 7.8**

  - [x] 5.6 Write property test for addon prerequisite enforcement
    - **Property 6: Addon Prerequisite Enforcement**
    - Adding any addon without active Base_Plan is rejected; subscription state remains unchanged
    - **Validates: Requirements 7.9, 7.10**

  - [x] 5.7 Write property test for feature flag computation correctness
    - **Property 9: Feature Flag Computation Correctness**
    - For any valid plan+addon combination, exactly the defined features are enabled; no feature enabled without corresponding addon
    - **Validates: Requirements 13.1, 13.2, 13.3, 13.4, 13.5**

- [x] 6. Payment Gateway (PayPal IPN)
  - [x] 6.1 Implement PayPal payment gateway
    - Create `web-ui/services/payment_gateway.py`
    - Implement `generate_payment_url(subscription)` → PayPal redirect URL with correct amount
    - Implement `POST /api/v1/payments/ipn` — receive IPN, echo back to PayPal for verification (30s timeout)
    - On `VERIFIED`: create payment record, notify Subscription Manager
    - On `INVALID` or timeout: discard, log failure
    - Flag for manual review after 3 consecutive failures for same txn
    - Implement `GET /api/v1/payments` — paginated billing history (up to 100 per page)
    - Handle cancel redirect (`GET /api/v1/payments/cancel`) and success redirect (`GET /api/v1/payments/success`)
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.9_

  - [x] 6.2 Write unit tests for IPN verification and payment flow
    - Test IPN parsing and verification mock
    - Test timeout handling and consecutive failure flagging
    - Test payment record creation on success
    - Test cancel/success redirects
    - _Requirements: 8.2, 8.3, 8.7, 8.8, 8.9_

- [x] 7. Checkpoint - Ensure all core service tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Web Portal (public pages, dashboard, configuration)
  - [x] 8.1 Implement landing page with pricing and feature comparison
    - Create `web-ui/blueprints/public.py` with Flask Blueprint
    - Create `web-ui/templates/pages/landing.html` — platform description, feature list, pricing cards (Trial, Base, Video, Premium, Full Bundle)
    - Implement "Compare All Features" expandable section with feature matrix table (General, Audio, Video, Premium, Voice categories)
    - Monthly/yearly toggle with discount indicator
    - Showcase sections with interface mockups
    - "Vote for us on top.gg" CTA link
    - Dark glassmorphism design with OKLCH colors, glass panels, Tailwind CSS v4
    - _Requirements: 12.1, 19.1, 19.2, 19.3, 19.4, 19.5, 19.6, 19.7_

  - [x] 8.2 Implement tenant dashboard
    - Create `web-ui/blueprints/dashboard.py` with Flask Blueprint
    - Create `web-ui/templates/pages/dashboard.html` — subscription overview, bot status cards, billing summary
    - Display Bot_Instance status (online/offline/restarting) with 30s update via HTMX polling
    - Show active subscription plan, addons, next billing date
    - Display registration confirmation for first-time users
    - Show trial apply / subscribe options when no active subscription
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 12.2, 12.5_

  - [x] 8.3 Implement bot configuration interface
    - Create `web-ui/blueprints/bot_config.py` with Flask Blueprint
    - Create `web-ui/templates/pages/bot_config.html` — source provider, autoplay, content filters, EQ presets
    - Save config immediately to PostgreSQL, apply to running instance within 30s via Redis pub/sub
    - Show notice if Bot_Instance is offline (config will apply on next start)
    - _Requirements: 12.6, 12.7, 12.8_

  - [x] 8.4 Implement subscription management routes
    - Create `web-ui/blueprints/subscriptions.py` with Flask Blueprint
    - `GET /api/v1/subscriptions` — list tenant's subscriptions
    - `POST /api/v1/subscriptions` — create subscription, redirect to PayPal
    - `DELETE /api/v1/subscriptions/{id}` — cancel subscription
    - `POST /api/v1/trials/apply` — submit trial application
    - `GET /api/v1/trials/status` — check trial application status
    - _Requirements: 7.5, 7.6, 6.1_

- [x] 9. Admin Panel
  - [x] 9.1 Implement admin panel blueprint
    - Create `web-ui/blueprints/admin.py` with Flask Blueprint
    - Protect all routes with `@operator_required` decorator
    - `GET /api/v1/admin/trials` — pending trial applications (oldest first)
    - `POST /api/v1/admin/trials/{id}/approve` — approve trial
    - `POST /api/v1/admin/trials/{id}/deny` — deny trial
    - `GET /api/v1/admin/subscriptions` — all subscriptions with tenant details, plan, addons, payment status, bot health
    - `POST /api/v1/admin/subscriptions/{id}/suspend` — suspend (with confirmation)
    - `POST /api/v1/admin/subscriptions/{id}/terminate` — terminate (with confirmation)
    - `GET /api/v1/admin/metrics` — total tenants, active trials, active subscriptions, total bot instances, GPU utilization per node
    - `GET /api/v1/admin/instances` — all bot instances with health status
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7_

  - [x] 9.2 Create admin panel templates
    - Create `web-ui/templates/pages/admin/` with: trials.html, subscriptions.html, metrics.html, instances.html
    - HTMX partial updates for approve/deny actions (no full page reload)
    - Confirmation modals (Alpine.js) before suspend/terminate
    - System metrics auto-refresh every 60s via `hx-trigger="every 60s"`
    - Bot health indicators: running (green), degraded (yellow), stopped (gray), unreachable (red)
    - _Requirements: 9.2, 9.3, 9.4, 9.5, 9.6, 9.7_

  - [x] 9.3 Write property test for authorization enforcement
    - **Property 7: Authorization Enforcement**
    - Non-operator users rejected from all admin endpoints; non-owner tenants get HTTP 403 on player API
    - **Validates: Requirements 9.1, 17.3**

- [x] 10. Web Player
  - [x] 10.1 Implement web player routes and WebSocket handler
    - Create `web-ui/blueprints/player.py` with Flask Blueprint
    - Implement `GET /player` — authenticated route rendering player page
    - Implement REST endpoints: `GET/POST /api/v1/player/{instance_id}/state|play|pause|resume|skip|previous|shuffle|repeat|volume|queue/*`
    - Implement WebSocket at `/ws/player/{instance_id}` via `flask-sock`
    - Authenticate via query param `?token={session_token}`
    - Validate tenant owns the bot instance (HTTP 403 if not)
    - Forward commands to Bot_Instance via Redis pub/sub command channel
    - Broadcast state updates to all connected WebSocket clients for that instance
    - _Requirements: 16.1, 16.6, 16.7, 17.1, 17.2, 17.3, 17.4, 17.6_

  - [x] 10.2 Implement rate limiter for playback API
    - Create `web-ui/services/rate_limiter.py`
    - Redis-backed sliding window rate limiter (sorted set per `tenant:endpoint`)
    - 60 requests/minute per tenant per bot instance
    - Return HTTP 429 with `Retry-After` header when exceeded
    - Also apply 60 messages/minute limit on WebSocket connections
    - _Requirements: 17.5_

  - [x] 10.3 Create web player frontend templates
    - Create `web-ui/templates/pages/player.html` — now playing (title, artist, art, progress), queue (drag-to-reorder), controls (prev, pause/resume, skip, shuffle, repeat, volume slider)
    - Create `web-ui/templates/partials/player_queue.html` — HTMX fragment for queue list
    - Create `web-ui/templates/partials/player_search.html` — search results fragment
    - Alpine.js for volume slider (debounced 500ms), drag-to-reorder queue, keyboard shortcuts
    - WebSocket JS client for real-time state updates (track change, progress ticks, volume)
    - Auto-reconnect WebSocket with exponential backoff
    - Disabled state when bot offline with "Bot not active" message
    - Search interface across enabled sources, playlist browser, "Recently Played" (50 tracks), "Popular Today"
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5, 16.7, 16.8, 16.9_

  - [x] 10.4 Write property test for rate limiting correctness
    - **Property 12: Rate Limiting Correctness**
    - Requests within 60-per-minute accepted; 61st request in any sliding 60s window returns HTTP 429
    - **Validates: Requirements 17.5**

- [x] 11. Checkpoint - Ensure all web tier tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Bot Orchestrator
  - [x] 12.1 Implement bot orchestrator controller
    - Create `web-ui/services/bot_orchestrator.py`
    - Implement `provision(tenant_id, subscription)` — create K8s Pod via `kubernetes-client` with correct resource limits per tier
    - Implement `deprovision(bot_instance_id, grace_seconds=30)` — send SIGTERM, allow graceful shutdown
    - Implement `health_check()` — check Redis heartbeats, restart pods with no heartbeat for 60s
    - Implement `restart(bot_instance_id)` — restart pod, track restart count (max 5 in 10 min)
    - Set status to `pending_resources` if cluster resources insufficient, retry every 60s up to 10 attempts
    - Set status to `failed` after max restarts exceeded, notify tenant
    - _Requirements: 10.1, 10.4, 10.5, 10.6, 10.8, 10.9_

  - [x] 12.2 Implement Pod spec builder with GPU scheduling
    - Create `web-ui/services/pod_spec_builder.py`
    - Generate Pod spec from template in design: init container (render-lavalink-config), bot container with TENANT_ID env var
    - Base_Plan: 250m CPU, 512Mi RAM, 0 GPU
    - Video_Addon: 500m CPU, 1Gi RAM, `intel.com/sriov-gpudevice: 1`, `supplementalGroups: [26]`, privileged, /dev/dri mount
    - CUDA workloads: `nvidia.com/gpu: 1` + node affinity → gremlin-1
    - No node affinity for Intel VFs (natural distribution across 4 nodes)
    - Include HELLODJ_DB_KEY, HELLODJ_PG_URI from secrets, LAVALINK_HOST pointing to shared pool
    - _Requirements: 10.1, 10.3, 10.5, 11.1, 11.2, 11.3, 11.4, 11.5_

  - [x] 12.3 Implement bot heartbeat and health monitoring
    - Add heartbeat publishing to bot startup: Redis `heartbeat:{instance_id}` with 30s TTL, refreshed every 15s
    - Orchestrator health loop: check heartbeats every 30s, restart unresponsive pods
    - Track restart count per instance, escalate to `failed` after 5 restarts in 10 minutes
    - _Requirements: 10.4, 10.9_

  - [x] 12.4 Write property test for pod spec correctness per subscription tier
    - **Property 8: Pod Spec Correctness Per Subscription Tier**
    - Generated pod spec matches tier definition; subscriptions without Video_Addon have no GPU resource requests
    - **Validates: Requirements 10.5, 11.1**

- [x] 13. Feature Gating in Bot
  - [x] 13.1 Implement feature gating decorator and client
    - Create `bot/feature_gate.py`
    - Implement `@feature_required(feature)` decorator for bot commands
    - Query feature flag API at startup, cache in-memory for 5 minutes
    - Subscribe to Redis `feature_change:{tenant_id}` for immediate invalidation
    - Default to Base_Plan restrictions if API unreachable and no cache
    - On blocked command: respond with informational message about required addon
    - Tidal fallback: if Premium_Addon not active, fall back to Base_Plan source (YouTube/Spotify free/SoundCloud)
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7_

  - [x] 13.2 Write unit tests for feature gating behavior
    - Test Base_Plan restricts to audio only
    - Test Video_Addon enables video commands
    - Test Premium_Addon enables Tidal/lossless
    - Test fallback behavior when API unreachable
    - Test Tidal fallback to free sources
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.7_

- [x] 14. Shared Lavalink Pool
  - [x] 14.1 Create shared Lavalink StatefulSet and headless Service
    - Create `kube/lavalink-pool/statefulset.yaml` — 2 replicas of Lavalink (existing custom image)
    - Create `kube/lavalink-pool/service.yaml` — headless ClusterIP None service at `lavalink-pool.hellodj-service.svc.cluster.local:2333`
    - Move Lavalink from sidecar container in main deployment to standalone StatefulSet
    - Tenant bots connect via service DNS name; queue isolation maintained at bot level (wavelink Player per guild)
    - Update kustomization.yaml to include lavalink-pool resources
    - _Requirements: 10.7_

- [x] 15. Drift Visualizer Engine (Multipass Feedback Renderer)
  - [x] 15.1 Implement DriftEngine class (GPUEngineBase subclass)
    - Create `bot/video/visualizer_engines/drift.py`
    - Implement ping-pong FBO pair (2 RGBA8 color textures for frame feedback)
    - Implement 48×36 warp mesh (vertex grid with per-frame UV update driven by audio)
    - Frame decay pass (multiply warped frame by decay factor 0.94–0.98, audio-modulated)
    - Basic waveform composite (distance-field oscilloscope line in fragment shader)
    - EGL headless context creation on Intel VF render node
    - glReadPixels → pipe to ffmpeg for HLS encoding
    - Register engine in `bot/video/visualizer_registry.py`
    - _Requirements: 11.3 (GPU VF usage), 13.2 (Video_Addon enables visualizer)_

  - [x] 15.2 Create warp and decay shaders
    - Create `bot/video/visualizer_engines/shaders/drift_warp.vert` — mesh vertex shader with audio-driven per-vertex zoom, rotation, displacement
    - Create `bot/video/visualizer_engines/shaders/drift_warp.frag` — texture sample from previous frame
    - Create `bot/video/visualizer_engines/shaders/drift_decay.frag` — decay multiplication pass
    - Warp parameters: zoom (bass-driven), rotation (mids-driven), per-vertex displacement (organic flow)
    - _Requirements: 11.3_

  - [x] 15.3 Implement bloom post-process
    - Create `bot/video/visualizer_engines/shaders/drift_bloom_h.frag` — horizontal Gaussian blur
    - Create `bot/video/visualizer_engines/shaders/drift_bloom_v.frag` — vertical Gaussian blur
    - Create `bot/video/visualizer_engines/shaders/drift_final.frag` — final compositing (main + bloom)
    - Half-resolution FBO pair (640×360) for separable blur
    - Bloom intensity controlled by audio energy
    - _Requirements: 11.3_

  - [x] 15.4 Implement composite shapes (waveform, spectrum ring, beat particles)
    - Create `bot/video/visualizer_engines/shaders/drift_composite.vert`
    - Create `bot/video/visualizer_engines/shaders/drift_composite.frag`
    - Oscilloscope waveform (raw audio as glowing line)
    - Spectrum ring (FFT bins drawn radially from center)
    - Beat particle burst (bright dots on transients)
    - Additive blend compositing onto warped frame
    - _Requirements: 11.3, 13.2_

  - [x] 15.5 Implement preset system with crossfade
    - Create `bot/video/visualizer_engines/drift_presets.py`
    - Preset data model: Python dicts defining warp params, decay, composite config, bloom, shader name
    - Create 10-15 factory presets with distinct aesthetics (Cosmic Drift, Neon Pulse, etc.)
    - Linear interpolation between presets over 3 seconds
    - Auto-advance on track change or timed interval (configurable)
    - _Requirements: 13.2_

  - [x] 15.6 Upgrade HLS encoding quality parameters
    - Update `bot/video/hls_transcode.py` ffmpeg command for Drift engine output
    - Change `global_quality`: 28 → 20 (higher quality for detailed visuals)
    - Change `maxrate`: 3000k → 6000k
    - Change `bufsize`: 5000k → 10000k
    - Force key frames every 1s instead of 2s
    - Add `preset veryslow` for better compression efficiency
    - Keep 1280×720 resolution (Discord Activity caps at this)
    - _Requirements: 11.3_

- [x] 16. Enhanced Discord Remote Control
  - [x] 16.1 Implement enhanced `/remote` command with persistent view
    - Create or update `bot/cogs/remote.py`
    - Implement `/remote` slash command showing embed: current track (title, artist, duration, progress), queue preview (next 5), volume, repeat/shuffle state, user avatar as author
    - Attach persistent view (timeout=None, fixed custom_ids, registered in setup_hook): Previous, Pause/Resume, Skip, Volume Down/Up, Shuffle toggle, AutoPlay toggle, Like, Stop, Dashboard link, "⬆️ Upvote on top.gg" link
    - Auto-update embed on track change
    - "Not Playing" idle state with Dashboard link
    - Button actions execute and update embed within 3 seconds
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5, 18.6, 18.7_

  - [x] 16.2 Write unit tests for remote command
    - Test embed generation with current track
    - Test button interactions (pause, skip, volume)
    - Test idle state display
    - Test persistent view registration
    - _Requirements: 18.1, 18.4, 18.6, 18.7_

- [x] 17. Checkpoint - Ensure all orchestration and bot tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 18. Data Migration (SQLite → PostgreSQL)
  - [x] 18.1 Implement migration scripts
    - Create `scripts/migrate_credentials.py` — read all rows from `hellodj.db` SQLite `credentials` table, insert into PG preserving Fernet-encrypted values byte-for-byte
    - Create `scripts/migrate_sessions.py` — read `data/sessions.json`, insert into PG `sessions` table with configurable `DEFAULT_TENANT_ID` (CLI arg or env var)
    - Create `scripts/migrate_playlists.py` — read `data/playlists.json`, insert into PG `playlists` table with configurable `DEFAULT_TENANT_ID`
    - Skip-on-conflict semantics (existing keys not overwritten, log warning)
    - Handle missing source files gracefully (log warning, continue)
    - Handle malformed JSON entries (log key + error, skip, continue)
    - Output summary: migrated/skipped counts per source
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6_

  - [x] 18.2 Implement rollback export script
    - Create `scripts/rollback_export.py` — re-export PostgreSQL data back to SQLite + JSON format
    - Produce files in same schema as original sources
    - Available for execution within 24 hours post-migration
    - _Requirements: 14.7_

  - [x] 18.3 Write property test for migration data preservation
    - **Property 10: Migration Data Preservation**
    - For any set of credential records in SQLite, migration produces identical key and value (byte-for-byte) in PG; existing keys not modified
    - **Validates: Requirements 14.1, 14.4**

- [x] 19. Init Container Adaptation
  - [x] 19.1 Update render_lavalink_config.py for PostgreSQL
    - Modify `bot/render_lavalink_config.py` to connect to PostgreSQL via `HELLODJ_PG_URI` instead of SQLite
    - Use same Fernet decryption logic (SHA-256 of HELLODJ_DB_KEY → base64 → Fernet key)
    - Render identical `application.yml` structure
    - Exit with non-zero status if PG connection fails (10s timeout) or `HELLODJ_PG_URI` missing
    - _Requirements: 15.1, 15.2, 15.3, 15.4, 15.5_

  - [x] 19.2 Write property test for config rendering equivalence
    - **Property 11: Config Rendering Equivalence**
    - Rendering from PG produces output identical (byte-for-byte) to rendering from SQLite when both contain same data
    - **Validates: Requirements 15.3**

- [x] 20. Tenant Isolation Integration Tests
  - [x] 20.1 Write property test for tenant data isolation
    - **Property 3: Tenant Data Isolation**
    - Writing data scoped to tenant A and reading as tenant B returns empty result set, regardless of overlapping guild_id/channel_id
    - Use testcontainers PostgreSQL for real DB isolation testing
    - **Validates: Requirements 2.3, 2.4, 10.2, 10.7**

  - [x] 20.2 Write integration tests for end-to-end flows
    - Test OAuth2 → tenant creation → trial application → approval → bot provisioning
    - Test subscription creation → PayPal flow → activation → feature flags
    - Test subscription expiry → grace period → deactivation
    - _Requirements: 4.2, 4.3, 6.3, 7.5, 7.6, 7.7, 10.1_

- [x] 21. Test infrastructure and Hypothesis strategies
  - [x] 21.1 Create shared test fixtures and Hypothesis strategies
    - Create `tests/conftest.py` with fixtures: PG testcontainer, Redis (fakeredis), mock K8s client, mock Discord OAuth
    - Create `tests/strategies.py` with Hypothesis strategies: credential keys/values, tenant_ids, discord_user_ids, plans, addon_sets, subscription_statuses, feature_subscriptions
    - Configure Hypothesis settings: `max_examples=100` per property
    - _Requirements: (testing infrastructure for all properties)_

- [x] 22. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document (12 properties total)
- Unit tests validate specific examples and edge cases
- The project uses Python 3.11, Hypothesis for property-based testing, pytest + pytest-asyncio for the test framework
- PostgreSQL connection: `postgresql-rw.postgresql-service.svc.cluster.local:5432`
- Redis deployment replaces the existing CrashLoopBackOff instance
- Web UI uses Flask + HTMX + Alpine.js + Tailwind CSS v4 (dark glassmorphism theme)
- Lavalink moves from sidecar to shared StatefulSet (2 replicas)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "21.1"] },
    { "id": 1, "tasks": ["2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "4.1", "4.2"] },
    { "id": 3, "tasks": ["4.3", "5.1", "5.2", "5.3"] },
    { "id": 4, "tasks": ["5.4", "5.5", "5.6", "5.7", "6.1"] },
    { "id": 5, "tasks": ["6.2", "8.1", "8.2", "8.3", "8.4"] },
    { "id": 6, "tasks": ["9.1", "9.2", "10.1", "10.2"] },
    { "id": 7, "tasks": ["9.3", "10.3", "10.4"] },
    { "id": 8, "tasks": ["12.1", "12.2", "12.3", "14.1"] },
    { "id": 9, "tasks": ["12.4", "13.1", "15.1", "15.2"] },
    { "id": 10, "tasks": ["13.2", "15.3", "15.4", "16.1"] },
    { "id": 11, "tasks": ["15.5", "15.6", "16.2", "18.1", "18.2", "19.1"] },
    { "id": 12, "tasks": ["18.3", "19.2", "20.1", "20.2"] }
  ]
}
```
