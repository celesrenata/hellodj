# Requirements Document

## Introduction

Transform HelloDJ from a single-instance personal Discord music bot into a multi-tenant SaaS platform. The platform enables public registration, subscription management, and isolated bot instances for paying customers. The migration preserves the existing Fernet-encrypted credential pattern while moving from SQLite to the existing CloudNativePG PostgreSQL cluster. Visual workloads (HLS transcoding, visualizer rendering) are distributed across all 4 gremlin nodes using Intel SR-IOV GPU virtual functions.

## Glossary

- **Platform**: The HelloDJ SaaS system comprising the Web_Portal, Auth_Service, Subscription_Manager, Bot_Orchestrator, and supporting infrastructure
- **Tenant**: A registered user who owns one or more Bot_Instances; identified by Discord user ID
- **Bot_Instance**: An isolated Discord bot process serving a single Tenant's guild(s), with dedicated queue, session state, and resource limits
- **Web_Portal**: The public-facing web application at hellodj.celestium.life providing registration, subscription management, and customer dashboards
- **Auth_Service**: The authentication/authorization subsystem using Discord OAuth2 as the identity provider
- **Subscription_Manager**: The subsystem tracking Tenant subscription tiers, add-ons, trial periods, and billing state
- **Trial_Manager**: The subsystem managing 30-day early access trial applications, approvals, and expirations
- **Payment_Gateway**: The PayPal integration handling payment verification via IPN/webhooks to celes@frameshift.net
- **Admin_Panel**: The server owner interface for manual approval of trials, subscriptions, and system-wide management
- **Credential_Store**: The Fernet-encrypted key-value store for secrets, migrated from SQLite to PostgreSQL while preserving the encryption-at-rest pattern
- **Bot_Orchestrator**: The subsystem responsible for creating, scheduling, health-checking, and tearing down Bot_Instances across the Kubernetes cluster
- **GPU_Scheduler**: The Kubernetes scheduling mechanism distributing GPU-accelerated workloads across nodes using `intel.com/sriov-gpudevice` resource requests
- **CNPG_Cluster**: The existing CloudNativePG PostgreSQL 18.3 cluster at `postgresql-rw.postgresql-service.svc.cluster.local:5432`
- **Base_Plan**: The $6.99/mo subscription tier providing 1 Bot_Instance with audio-only features
- **Video_Addon**: The +$1.99/mo add-on enabling video streaming, Discord Activity, and HLS transcoding
- **Premium_Addon**: The +$1.99/mo add-on enabling premium music sources (Tidal HiFi, lossless audio, priority queue)
- **Additional_Bot_Addon**: The +$1.99/mo per-instance add-on for multi-server support

## Requirements

### Requirement 1: PostgreSQL Database Provisioning

**User Story:** As a platform operator, I want HelloDJ data stored in the existing CNPG PostgreSQL cluster, so that the system benefits from cluster-grade reliability, replication, and backup capabilities.

#### Acceptance Criteria

1. WHEN the Platform is deployed, THE Credential_Store SHALL create a `hellodj` database and `hellodj` user in the CNPG_Cluster at `postgresql-rw.postgresql-service.svc.cluster.local:5432` idempotently, succeeding without error if the database and user already exist
2. THE Credential_Store SHALL store all key-value credential pairs in a PostgreSQL `credentials` table with columns: `key TEXT PRIMARY KEY`, `value BYTEA NOT NULL`, `updated_at TIMESTAMPTZ DEFAULT NOW()`
3. THE Credential_Store SHALL encrypt all secret values using Fernet encryption with the key derived from `HELLODJ_DB_KEY` (SHA-256 hash, base64-encoded to 32-byte Fernet key) before writing to PostgreSQL
4. IF the `HELLODJ_DB_KEY` environment variable is missing or empty at startup, THEN THE Credential_Store SHALL raise a RuntimeError and prevent the application from starting
5. WHEN the Credential_Store reads a credential from PostgreSQL, THE Credential_Store SHALL decrypt the value using the same Fernet key derivation as the current SQLite implementation
6. THE Credential_Store SHALL maintain the existing public API (`get`, `set`, `delete`, `get_prefix`, `get_bool`, `get_int`, `get_float`, `exists`, `keys`) with no changes to calling code
7. WHEN a connection to the CNPG_Cluster is lost, THE Credential_Store SHALL retry with exponential backoff (1s, 2s, 4s, 8s, max 30s) for up to 5 attempts before raising an exception to the caller
8. THE Credential_Store SHALL support concurrent access from multiple containers (bot, tidal-stream, spotify-stream) connecting to the same PostgreSQL database without data corruption, using row-level locking on writes

### Requirement 2: Session and Playlist Migration to PostgreSQL

**User Story:** As a platform operator, I want session and playlist persistence moved to PostgreSQL, so that all stateful data benefits from the same reliability guarantees and supports multi-tenant isolation.

#### Acceptance Criteria

1. THE Platform SHALL store playback sessions in a PostgreSQL `sessions` table with columns: `tenant_id UUID`, `guild_id BIGINT`, `channel_id BIGINT`, `session_data JSONB` (maximum 1 MB), `updated_at TIMESTAMPTZ`, with a composite primary key of `(tenant_id, guild_id, channel_id)`
2. THE Platform SHALL store playlists in a PostgreSQL `playlists` table with columns: `tenant_id UUID`, `playlist_id UUID PRIMARY KEY`, `guild_id BIGINT`, `name TEXT` (maximum 100 characters), `tracks JSONB` (maximum 5 MB), `created_at TIMESTAMPTZ`, `updated_at TIMESTAMPTZ`, with a unique constraint on `(tenant_id, guild_id, lower(name))`
3. WHEN a Bot_Instance saves a session, THE Platform SHALL write the session data scoped to the owning Tenant's `tenant_id`, completing the write within 2 seconds or returning a timeout error
4. WHEN a Bot_Instance reads sessions, THE Platform SHALL return only sessions belonging to the requesting Tenant, filtering by `tenant_id` at the query level so that no cross-tenant data is accessible regardless of the guild_id or channel_id provided
5. THE Platform SHALL provide a one-time migration script that reads existing `data/sessions.json` and `data/playlists.json` and inserts records into PostgreSQL using a configurable default Tenant ID (provided as a CLI argument or environment variable `DEFAULT_TENANT_ID`)
6. IF the migration script encounters a missing source file, THEN THE Platform SHALL log a warning message indicating the missing file path and skip that file without aborting the overall migration
7. IF the migration script encounters a malformed JSON entry that cannot be parsed, THEN THE Platform SHALL log the entry key and error detail, skip that entry, and continue processing remaining entries, reporting a summary count of skipped entries upon completion

### Requirement 3: Multi-Tenant Schema

**User Story:** As a platform operator, I want a database schema supporting multiple tenants, so that each customer's data is logically isolated.

#### Acceptance Criteria

1. THE Platform SHALL store tenant records in a `tenants` table with columns: `id UUID PRIMARY KEY`, `discord_user_id BIGINT UNIQUE NOT NULL`, `discord_username TEXT`, `email TEXT`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`
2. THE Platform SHALL store subscription records in a `subscriptions` table with columns: `id UUID PRIMARY KEY`, `tenant_id UUID NOT NULL REFERENCES tenants(id)`, `plan TEXT NOT NULL`, `addons TEXT[] DEFAULT '{}'`, `status TEXT NOT NULL`, `started_at TIMESTAMPTZ NOT NULL`, `expires_at TIMESTAMPTZ`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
3. THE Platform SHALL store bot instance records in a `bot_instances` table with columns: `id UUID PRIMARY KEY`, `tenant_id UUID NOT NULL REFERENCES tenants(id)`, `discord_bot_token_encrypted BYTEA`, `guild_ids BIGINT[]`, `status TEXT NOT NULL`, `node_name TEXT`, `pod_name TEXT`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
4. THE Platform SHALL store payment records in a `payments` table with columns: `id UUID PRIMARY KEY`, `tenant_id UUID NOT NULL REFERENCES tenants(id)`, `paypal_txn_id TEXT UNIQUE`, `amount_cents INTEGER NOT NULL CHECK (amount_cents > 0)`, `currency TEXT NOT NULL DEFAULT 'USD'`, `status TEXT NOT NULL`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`
5. THE Platform SHALL enforce foreign key constraints between tenants and all tenant-scoped tables using `ON DELETE RESTRICT` to prevent deletion of a tenant that has associated subscriptions, bot instances, or payments
6. THE Platform SHALL constrain `subscriptions.status` to one of: `active`, `past_due`, `cancelled`, `expired`; `bot_instances.status` to one of: `provisioning`, `running`, `stopped`, `error`; and `payments.status` to one of: `pending`, `completed`, `refunded`, `failed`
7. THE Platform SHALL constrain `subscriptions.plan` to one of: `base`, `trial`

### Requirement 4: Discord OAuth2 Authentication

**User Story:** As a visitor, I want to log in with my Discord account, so that I can register and manage my HelloDJ subscription without creating a separate account.

#### Acceptance Criteria

1. WHEN a visitor navigates to the Web_Portal login page, THE Auth_Service SHALL redirect the visitor to Discord's OAuth2 authorization endpoint with scopes `identify` and `email`, including a cryptographically random `state` parameter (minimum 128 bits of entropy) stored in the visitor's server-side session for CSRF validation
2. WHEN Discord redirects back with an authorization code, THE Auth_Service SHALL validate the returned `state` parameter against the stored value, exchange the code for an access token within a 10-second HTTP timeout, and retrieve the user's Discord profile (user ID, username, avatar, email)
3. WHEN a returning user authenticates, THE Auth_Service SHALL match the Discord user ID to an existing Tenant record and create a session
4. WHEN a new user authenticates for the first time, THE Auth_Service SHALL create a new Tenant record with the Discord user ID and profile information
5. THE Auth_Service SHALL store session tokens (minimum 128 bits of entropy, cryptographically random) in a server-side session store with a configurable TTL (default 7 days)
6. WHEN a session token expires or is invalid, THE Auth_Service SHALL redirect the user to the Web_Portal login page and discard the expired session from the server-side store
7. IF the Discord OAuth2 callback contains a `state` parameter that does not match the stored value, or if the code exchange fails, or if the user denies authorization, THEN THE Auth_Service SHALL redirect the visitor to the login page with an error indication describing the failure category (denied, expired, or service unavailable) without exposing internal details

### Requirement 5: User Registration and Profile

**User Story:** As a new user, I want to register for the platform and view my account details, so that I can manage my subscription and bot instances.

#### Acceptance Criteria

1. WHEN a new user completes Discord OAuth2 authentication for the first time, THE Web_Portal SHALL display a registration confirmation page showing the user's Discord username and avatar within 3 seconds of receiving the OAuth2 callback
2. WHEN an already-registered user completes Discord OAuth2 authentication, THE Web_Portal SHALL redirect the user to their profile page without displaying the registration confirmation
3. THE Web_Portal SHALL provide a profile page displaying: Discord username, avatar, account creation date, subscription status, active Bot_Instances (showing count and instance names), and the most recent 50 billing history entries in reverse chronological order; IF the user has no billing history, THEN THE Web_Portal SHALL display an empty-state message indicating no billing records exist
4. WHEN a Tenant views their profile, THE Web_Portal SHALL display the current subscription plan name, active add-ons, and next billing date
5. WHEN a Tenant has no active subscription or trial, THE Web_Portal SHALL display options to apply for a trial or subscribe to a plan
6. IF Discord OAuth2 authentication fails or the user denies authorization, THEN THE Web_Portal SHALL display an error message indicating the authentication did not complete and provide an option to retry

### Requirement 6: Early Access Trial System

**User Story:** As a new user, I want to apply for a free 30-day trial, so that I can evaluate HelloDJ before committing to a paid subscription.

#### Acceptance Criteria

1. WHEN a Tenant without an active subscription clicks "Apply for Trial", THE Trial_Manager SHALL create a trial application record with status `pending` in the `trial_applications` table
2. WHEN a trial application is created, THE Trial_Manager SHALL add the application to the pending applications list in the Admin_Panel within 5 seconds of creation
3. WHEN the platform operator approves a trial application, THE Trial_Manager SHALL activate a 30-day trial subscription for the Tenant with Base_Plan features (audio only, 1 Bot_Instance, no video)
4. WHEN a trial period reaches 30 days from activation, THE Trial_Manager SHALL set the trial status to `expired` and deactivate the associated Bot_Instance
5. IF a Tenant attempts to apply for a trial while already having an active trial or subscription, THEN THE Trial_Manager SHALL reject the application and display an error message indicating the Tenant already has an active trial or subscription
6. WHEN a trial expires, THE Web_Portal SHALL display a prompt to subscribe to a paid plan on the Tenant's next dashboard access
7. WHEN the platform operator rejects a trial application, THE Trial_Manager SHALL set the application status to `rejected` and notify the Tenant that their trial application was not approved

### Requirement 7: Subscription Plans and Add-ons

**User Story:** As a user, I want to choose a subscription plan and add-ons, so that I can access the features I need at a price I choose.

#### Acceptance Criteria

1. THE Subscription_Manager SHALL support the Base_Plan at $6.99/mo providing 1 Bot_Instance with audio playback (YouTube, Spotify free-tier, SoundCloud) and no video support
2. THE Subscription_Manager SHALL support the Video_Addon at +$1.99/mo enabling video streaming, Discord Activity, and HLS transcoding for the Tenant's Bot_Instance(s), requiring an active Base_Plan subscription
3. THE Subscription_Manager SHALL support the Premium_Addon at +$1.99/mo enabling Tidal HiFi, lossless audio, and priority queue positioning (tracks queued by Premium subscribers are inserted ahead of non-premium queued tracks) for the Tenant's Bot_Instance(s), requiring an active Base_Plan subscription
4. THE Subscription_Manager SHALL support the Additional_Bot_Addon at +$1.99/mo per additional Bot_Instance beyond the first, up to a maximum of 9 additional instances (10 total per Tenant)
5. WHEN a Tenant subscribes to a plan, THE Subscription_Manager SHALL record the subscription with status `pending_payment` until payment is verified or 24 hours have elapsed, whichever comes first
6. WHEN payment is verified by the Payment_Gateway, THE Subscription_Manager SHALL set subscription status to `active` and provision the corresponding Bot_Instance(s) within 60 seconds
7. WHEN a subscription expires without renewal, THE Subscription_Manager SHALL set status to `expired` and deactivate the Tenant's Bot_Instance(s) after a 3-day grace period
8. IF payment is not verified within 24 hours of subscription creation, THEN THE Subscription_Manager SHALL set subscription status to `cancelled` and discard the pending subscription without provisioning any Bot_Instance(s)
9. IF the Payment_Gateway rejects a payment attempt, THEN THE Subscription_Manager SHALL retain the subscription in `pending_payment` status and notify the Tenant with an error message indicating the payment was declined
10. IF a Tenant attempts to subscribe to an add-on without an active Base_Plan, THEN THE Subscription_Manager SHALL reject the request with an error message indicating that a Base_Plan is required

### Requirement 8: PayPal Payment Integration

**User Story:** As a subscriber, I want to pay via PayPal, so that I can activate and maintain my subscription.

#### Acceptance Criteria

1. WHEN a Tenant selects a plan and clicks "Subscribe", THE Payment_Gateway SHALL generate a PayPal payment URL within 10 seconds, with the amount equal to the sum of the selected plan price plus all selected add-on prices, and redirect the Tenant to PayPal to complete payment
2. WHEN PayPal sends an IPN (Instant Payment Notification) for a completed payment, THE Payment_Gateway SHALL verify the IPN with PayPal's verification endpoint within 30 seconds of receipt
3. WHEN an IPN is verified as authentic and complete, THE Payment_Gateway SHALL create a payment record and notify the Subscription_Manager of successful payment
4. THE Payment_Gateway SHALL store payment records including PayPal transaction ID (up to 64 characters), amount (2 decimal places), currency code, and UTC timestamp
5. WHEN a recurring payment fails, THE Payment_Gateway SHALL update the subscription status to `payment_failed` and display a notification on the Tenant's Web_Portal dashboard indicating the failed payment date and affected subscription plan
6. THE Web_Portal SHALL display billing history showing up to 100 past payments ordered by date descending, with date, amount, currency, and transaction ID, and provide pagination when more than 100 records exist
7. IF IPN verification fails or PayPal's verification endpoint does not respond within 30 seconds, THEN THE Payment_Gateway SHALL discard the IPN, log the failure, and not activate or modify the subscription
8. IF the Tenant cancels payment on the PayPal page, THEN THE Payment_Gateway SHALL redirect the Tenant back to the Web_Portal plan selection page without creating a payment record or modifying subscription status
9. IF PayPal's verification endpoint is unreachable for 3 consecutive IPN attempts for the same transaction, THEN THE Payment_Gateway SHALL flag the transaction for manual review and not activate the subscription

### Requirement 9: Admin Panel for Manual Approvals

**User Story:** As the platform operator, I want an admin panel to approve trials and subscriptions, so that I maintain control over who accesses the platform during early operation.

#### Acceptance Criteria

1. IF a user who is not identified by a configured operator Discord user ID attempts to access the Admin_Panel, THEN THE Admin_Panel SHALL reject the request and display no panel content
2. THE Admin_Panel SHALL display a list of pending trial applications ordered by application date (oldest first), showing applicant Discord username, user ID, application date, and approve/deny actions
3. WHEN the operator approves a trial application, THE Admin_Panel SHALL trigger Trial_Manager activation for that Tenant and remove the application from the pending list
4. WHEN the operator denies a trial application, THE Admin_Panel SHALL mark the application as denied, remove it from the pending list, and record the denial date
5. THE Admin_Panel SHALL display a list of all active subscriptions with Tenant details, plan, add-ons, payment status, and Bot_Instance health indicated as one of: running, degraded, stopped, or unreachable
6. WHEN the operator selects activate, suspend, or terminate on a Tenant's subscription, THE Admin_Panel SHALL present a confirmation prompt before executing the action
7. THE Admin_Panel SHALL display system-wide metrics: total tenants, active trials, active subscriptions, total Bot_Instances, and GPU utilization as a percentage per node, refreshed no less frequently than every 60 seconds

### Requirement 10: Multi-Tenant Bot Orchestration

**User Story:** As a paying subscriber, I want my own isolated bot instance, so that my music queue, sessions, and preferences are private and unaffected by other users.

#### Acceptance Criteria

1. WHEN a subscription is activated, THE Bot_Orchestrator SHALL create a new Bot_Instance as a Kubernetes Pod in the `hellodj-service` namespace with resource limits based on the subscription tier within 120 seconds of activation
2. THE Bot_Orchestrator SHALL isolate each Bot_Instance's queue, session state, playlists, and preferences using the Tenant's `tenant_id` as a partition key
3. THE Bot_Orchestrator SHALL configure each Bot_Instance with the Tenant's Discord bot token (stored encrypted in the Credential_Store) and the Tenant's assigned guild IDs (maximum 5 per Bot_Instance)
4. WHEN a Bot_Instance crashes or becomes unresponsive (no heartbeat for 60 seconds), THE Bot_Orchestrator SHALL restart the Pod and resume from the persisted session state, up to a maximum of 5 restart attempts within a 10-minute window before marking the instance as `failed`
5. THE Bot_Orchestrator SHALL enforce resource limits per tier: Base_Plan (250m CPU, 512Mi memory, 0 GPU VFs), Video_Addon (500m CPU, 1Gi memory, 1 GPU VF)
6. WHEN a subscription is deactivated, THE Bot_Orchestrator SHALL send SIGTERM to the Bot_Instance Pod, allow up to 30 seconds for the process to persist current session state and disconnect from Discord, then force-terminate the Pod
7. THE Bot_Orchestrator SHALL share the existing Lavalink infrastructure across all Bot_Instances while maintaining queue isolation per Tenant
8. IF the Bot_Orchestrator cannot schedule a Bot_Instance Pod due to insufficient cluster resources, THEN THE Bot_Orchestrator SHALL set the Bot_Instance status to `pending_resources`, notify the Tenant via the Web_Portal, and retry scheduling every 60 seconds for up to 10 attempts
9. IF a Bot_Instance exceeds the maximum restart attempts (5 within 10 minutes), THEN THE Bot_Orchestrator SHALL set the Bot_Instance status to `failed` and notify the Tenant via the Web_Portal with an indication that manual intervention or support contact is required

### Requirement 11: Multi-Node GPU Distribution

**User Story:** As a subscriber with the Video add-on, I want my video transcoding workload distributed across available GPU nodes, so that I get consistent performance regardless of cluster load.

#### Acceptance Criteria

1. WHEN the Bot_Orchestrator creates a Bot_Instance with the Video_Addon, THE GPU_Scheduler SHALL include a resource request for `intel.com/sriov-gpudevice: 1` in the Pod spec
2. THE GPU_Scheduler SHALL NOT apply node affinity constraints that restrict Bot_Instance Pods to a subset of gremlin nodes, allowing Kubernetes to distribute Pods across all 4 gremlin nodes based on available `intel.com/sriov-gpudevice` capacity
3. WHILE a Bot_Instance Pod is running with a GPU VF allocation, THE Bot_Instance SHALL use the allocated VF for HLS transcoding and visualizer rendering via QSV/VA-API
4. THE GPU_Scheduler SHALL rely on the Intel SR-IOV device plugin resource accounting to enforce a maximum of 7 GPU VFs allocated per gremlin node
5. WHERE a Tenant has requested CUDA workloads, THE GPU_Scheduler SHALL schedule the Bot_Instance on gremlin-1 with an `nvidia.com/gpu: 1` resource request and a node affinity constraint targeting gremlin-1
6. IF all `intel.com/sriov-gpudevice` resources are exhausted across the cluster, THEN THE GPU_Scheduler SHALL leave the Bot_Instance Pod in Pending state and report an error indication to the Tenant that no GPU capacity is currently available

### Requirement 12: Web Portal Expansion

**User Story:** As a visitor or subscriber, I want a public-facing web portal, so that I can register, manage my subscription, and configure my bot without needing direct Discord admin access.

#### Acceptance Criteria

1. THE Web_Portal SHALL serve a public landing page at `hellodj.celestium.life` displaying: a platform description, feature list, subscription plan pricing, and a call-to-action linking to the Discord OAuth2 login flow
2. THE Web_Portal SHALL provide authenticated routes for: dashboard (subscription overview), bot configuration, playlist management, billing history, and profile settings
3. WHEN an unauthenticated user accesses a protected route, THE Web_Portal SHALL redirect to the Discord OAuth2 login flow and, upon successful authentication, redirect the user to the originally requested route
4. THE Web_Portal SHALL retain the existing admin functionality (config editing, backup/restore, provider OAuth flows, guild management, moderation, metrics) accessible only to the platform operator as determined by the owner binding in the Auth_Service
5. THE Web_Portal SHALL display Bot_Instance status (online/offline/restarting) on the Tenant's dashboard, updating within 30 seconds of a status change
6. THE Web_Portal SHALL provide a bot configuration interface allowing Tenants to set: source provider preference, autoplay settings, content filters, and equalizer presets for their Bot_Instance(s)
7. WHEN a Tenant saves a bot configuration change, THE Web_Portal SHALL persist the configuration immediately and apply it to the Bot_Instance within 30 seconds if the instance is online
8. IF a Tenant saves a bot configuration change while the associated Bot_Instance is offline, THEN THE Web_Portal SHALL persist the configuration and display a notice indicating the change will apply when the Bot_Instance next starts

### Requirement 13: Tenant Feature Gating

**User Story:** As a platform operator, I want features gated by subscription tier, so that free trial users cannot access premium features without paying.

#### Acceptance Criteria

1. WHILE a Tenant has only the Base_Plan active, THE Bot_Instance SHALL restrict playback to audio-only sources (YouTube, Spotify free-tier, SoundCloud) and reject video commands with an informational message
2. WHILE a Tenant has the Video_Addon active, THE Bot_Instance SHALL enable video streaming, Discord Activity, HLS transcoding, and visualizer features
3. WHILE a Tenant has the Premium_Addon active, THE Bot_Instance SHALL enable Tidal HiFi streaming, lossless audio output, and priority queue positioning where requested tracks are inserted at position 1 (next-up) ahead of non-priority tracks
4. IF a Tenant without the Video_Addon invokes a video command, THEN THE Bot_Instance SHALL respond with a message indicating the feature requires the Video add-on and SHALL NOT execute the command
5. IF a Tenant without the Premium_Addon requests a Tidal track, THEN THE Bot_Instance SHALL fall back to an available Base_Plan source (YouTube, Spotify free-tier, or SoundCloud) and respond with a message indicating that Tidal requires the Premium add-on
6. THE Subscription_Manager SHALL expose a feature-flag API that Bot_Instances query at startup and on subscription change events, with Bot_Instances caching the response and refreshing within 60 seconds of a subscription change event
7. IF the Subscription_Manager API is unreachable at startup or on a subscription change event, THEN THE Bot_Instance SHALL retain the last-known feature flags and log the failure, defaulting to Base_Plan restrictions if no cached flags exist

### Requirement 14: Data Migration from SQLite

**User Story:** As a platform operator, I want to migrate existing data from SQLite to PostgreSQL, so that the transition preserves all credentials and state without downtime.

#### Acceptance Criteria

1. THE Platform SHALL provide a migration script that reads all rows from the existing `hellodj.db` SQLite `credentials` table and inserts them into the PostgreSQL `credentials` table, preserving Fernet-encrypted `value` blobs byte-for-byte without re-encryption, and preserving the `key` and `updated_at` columns unchanged
2. THE Platform SHALL provide a migration script that reads `data/sessions.json` and inserts session records into the PostgreSQL `sessions` table, assigning each record the default Tenant ID value `"system"` where no tenant context exists in the source data
3. THE Platform SHALL provide a migration script that reads `data/playlists.json` and inserts playlist records into the PostgreSQL `playlists` table, assigning each record the default Tenant ID value `"system"` where no tenant context exists in the source data
4. WHEN the migration script encounters a record whose primary key already exists in the target PostgreSQL table, THE migration script SHALL skip that record without modifying the existing row and log a warning message to stdout indicating the skipped key
5. IF a source file (`hellodj.db`, `data/sessions.json`, or `data/playlists.json`) is missing or contains zero records, THEN THE migration script SHALL log a warning to stdout indicating the empty or missing source and continue migrating remaining sources without failing
6. WHEN the migration script completes, THE migration script SHALL output a summary to stdout listing the number of records migrated and the number of records skipped per source (credentials, sessions, playlists)
7. THE Platform SHALL support a rollback procedure that re-exports PostgreSQL data back to SQLite and JSON format, available for execution at any point within 24 hours after the migration completes, producing files in the same schema as the original sources

### Requirement 15: Init Container Adaptation

**User Story:** As a platform operator, I want the Lavalink config rendering to work with the new PostgreSQL credential store, so that the existing init container pattern continues functioning.

#### Acceptance Criteria

1. THE render_lavalink_config.py init container SHALL connect to PostgreSQL using the `HELLODJ_PG_URI` environment variable instead of SQLite to read credentials, issuing read-only queries against the credentials table (key TEXT, value BYTEA, updated_at TIMESTAMP)
2. THE render_lavalink_config.py init container SHALL use the same Fernet decryption logic (SHA-256 of `HELLODJ_DB_KEY` passphrase, base64-encoded as Fernet key) to decrypt credential values retrieved from PostgreSQL
3. THE render_lavalink_config.py init container SHALL render the same application.yml structure and write it to the same output path (`/out/application.yml`) as the previous SQLite-based renderer, such that a diff of the rendered output for identical credential data produces no differences
4. IF the PostgreSQL connection cannot be established within 10 seconds, or authentication fails, or the credentials table does not exist, THEN THE init container SHALL log an error message indicating the failure reason and exit with a non-zero status code
5. IF the `HELLODJ_PG_URI` environment variable is missing or empty, THEN THE init container SHALL exit with a non-zero status code and log an error message indicating the missing connection string

### Requirement 16: Web Player (Browser-Based Remote)

**User Story:** As a subscriber, I want to manage my bot's playback from a web browser, so that I can queue tracks, browse playlists, and control playback without needing Discord open.

#### Acceptance Criteria

1. THE Web_Portal SHALL provide a `/player` route (authenticated) displaying: current track (title, artist, album art, progress bar), queue list with drag-to-reorder, and playback controls (previous, pause/resume, skip, shuffle, repeat, volume)
2. THE Web_Portal SHALL provide a search interface within the web player allowing Tenants to search tracks by query across all enabled sources (YouTube, Spotify, SoundCloud, Tidal depending on subscription tier) and add results to the queue
3. THE Web_Portal SHALL provide a playlist browser within the web player showing all of the Tenant's saved playlists, with the ability to load a playlist into the queue or add individual tracks
4. THE Web_Portal SHALL provide a "Recently Played" section showing the most recent 50 tracks played by the Tenant's Bot_Instance(s), with the ability to re-queue any track
5. THE Web_Portal SHALL provide a "Popular Today" section showing the most-queued tracks across the platform (anonymized, no Tenant attribution) as discovery inspiration
6. WHEN a Tenant interacts with playback controls in the web player, THE Web_Portal SHALL send the command to the Bot_Instance via a real-time API (WebSocket or SSE) and reflect the updated state within 2 seconds
7. THE Web_Portal SHALL display real-time now-playing state including elapsed time, track duration, and queue length, updating at minimum every 5 seconds while the player page is open
8. IF the Tenant's Bot_Instance is offline or not in a voice channel, THEN THE Web_Portal SHALL display the player in a disabled state with a message indicating the bot is not currently active, and provide a "Connect" action if the bot is online but idle
9. THE Web_Portal SHALL provide a volume slider that adjusts the Bot_Instance volume in real-time (debounced to 1 update per 500ms maximum)

### Requirement 17: Playback Control API (Backend)

**User Story:** As a platform developer, I want a REST/WebSocket API for controlling bot playback, so that both the web player and the Discord `/remote` command can interact with the same backend state.

#### Acceptance Criteria

1. THE Platform SHALL expose a REST API at `/api/v1/player/{bot_instance_id}` requiring Tenant authentication, providing endpoints: `GET /state` (current track, queue, volume, repeat, shuffle), `POST /play` (search + queue), `POST /pause`, `POST /resume`, `POST /skip`, `POST /previous`, `POST /shuffle`, `POST /repeat`, `POST /volume`, `POST /queue/add`, `POST /queue/remove`, `POST /queue/move`, `DELETE /queue/clear`
2. THE Platform SHALL expose a WebSocket endpoint at `/ws/player/{bot_instance_id}` requiring Tenant authentication, pushing real-time state updates (track change, progress tick every 5s, queue modification, volume change) to connected web player clients
3. THE Platform SHALL validate that the authenticated Tenant owns the specified `bot_instance_id` before executing any command, returning HTTP 403 if the Tenant does not own the instance
4. WHEN a playback command is received via the API, THE Platform SHALL forward the command to the Bot_Instance process within 1 second and return the result (success or error with reason) to the caller
5. THE Platform SHALL rate-limit the playback control API to 60 requests per minute per Tenant per Bot_Instance, returning HTTP 429 with a `Retry-After` header when exceeded
6. THE Platform SHALL support multiple simultaneous WebSocket connections per Bot_Instance (e.g., multiple browser tabs), broadcasting state updates to all connected clients for that instance

### Requirement 18: Enhanced Discord Remote Control

**User Story:** As a user in Discord, I want an improved `/remote` command with playback controls, queue preview, and a top.gg upvote link, so that I can control the bot and support it without leaving Discord.

#### Acceptance Criteria

1. WHEN a user invokes `/remote`, THE Bot_Instance SHALL send an embed displaying: current track (title, artist, duration, progress), queue preview (next 5 tracks), volume level, repeat/shuffle state, and the requesting user's avatar as the embed author
2. THE Bot_Instance SHALL attach a persistent interactive view to the `/remote` embed containing buttons: Previous, Pause/Resume, Skip, Volume Down, Volume Up, Shuffle toggle, AutoPlay toggle, Like (add to playlist), Stop, and a "Dashboard" link button opening the web player URL
3. THE Bot_Instance SHALL include a "⬆️ Upvote on top.gg" link button in the `/remote` view that opens the HelloDJ top.gg page (`https://top.gg/bot/{bot_app_id}/vote`) in the user's browser
4. WHEN a user clicks a playback control button on the `/remote` embed, THE Bot_Instance SHALL execute the action, update the embed to reflect the new state, and acknowledge the interaction within 3 seconds
5. THE Bot_Instance SHALL update the `/remote` embed automatically when the track changes, refreshing the now-playing information and queue preview without requiring user interaction
6. IF the Bot_Instance is not currently playing audio, THEN THE `/remote` command SHALL display a "Not Playing" state with a message indicating the bot is idle, and provide a "Dashboard" link to queue tracks via the web player
7. THE `/remote` view SHALL persist (timeout=None) so that buttons remain functional indefinitely for the message, even across bot restarts (using fixed `custom_id` values registered in `setup_hook`)

### Requirement 19: Feature Comparison Landing Page

**User Story:** As a visitor, I want to see a detailed feature comparison across subscription tiers, so that I can make an informed decision about which plan to choose.

#### Acceptance Criteria

1. THE Web_Portal landing page SHALL include a pricing section displaying all plans (Trial, Base, Base+Video, Base+Premium, Full Bundle) as cards with monthly prices, key features listed, and a call-to-action button for each tier
2. THE Web_Portal landing page SHALL include a "Compare All Features" expandable section displaying a feature comparison matrix table with columns for each tier (Trial, Base, Video Addon, Premium Addon) and rows grouped by category
3. THE feature comparison categories SHALL include at minimum: General (bot instances, guilds per bot, queue size, playlist count), Audio (sources, filters, equalizer, crossfade), Video (streaming, Activity, visualizer, whiteboard), Premium (Tidal HiFi, lossless, priority queue), and Voice (wake word, STT, TTS, AI commands)
4. THE comparison table SHALL use checkmarks (✓) for boolean features, numeric values for limits, and "×" for unavailable features, styled consistently with the dark glassmorphism design system
5. THE Web_Portal SHALL include a monthly/yearly toggle on the pricing section; IF the yearly option is selected, THEN THE Web_Portal SHALL display the annual price with a percentage discount indicator (e.g., "-17%") compared to monthly billing
6. THE Web_Portal landing page SHALL include showcase sections with screenshots/mockups demonstrating: the web player interface, the Discord controller embed, the Activity visualizer, and the whiteboard features
7. THE Web_Portal landing page SHALL include a "Vote for us on top.gg" call-to-action linking to the HelloDJ top.gg page
