# Requirements Document

## Introduction

The current web-ui "admin" panel is effectively a user panel: the Dashboard, Config, and Guilds pages present global/self-service views, and the only true admin capabilities are listing accounts and toggling role/enabled/delete (`pages.py`, `admin_directory.py`). This feature introduces a real **administrative control plane** that lets platform administrators govern what each user is allowed to do with the bot on a per-user basis, meter AI usage cost with a markup over Amazon Bedrock pricing, and enforce per-user limits on bot instances and guilds.

Entitlements are authored in the admin panel and stored on the shared `hellodj-core` DynamoDB table. They are read at runtime by both the web-ui (to gate the self-service UI) and the bot (to actually enforce the capability inside Discord), so disabling a feature for a user takes real effect rather than being cosmetic. New users receive a conservative default entitlement set (secure by default). AI cost tracking is a metered tally with an optional per-user cap that warns rather than hard-blocks.

This spec covers the admin panel UI, the per-user entitlement data model, the enforcement points, and the AI cost metering. It builds on the existing Cognito-backed `AdminDirectory`, the `ConfigStore`/`CoreTable` data access, and the multi-tenant guild/source work; it does not re-implement authentication or the guild-source OAuth flows.

## Glossary

- **Administrator**: An authenticated account in the Cognito `admins` group. Governs all accounts and platform entitlements.
- **User**: A standard (Discord-linked) account whose capabilities are governed by entitlements.
- **Entitlement**: A per-user record of allowed capabilities and limits (feature flags + numeric quotas) stored on `hellodj-core`.
- **Entitlement flag**: A boolean capability toggle (e.g. "video activities allowed").
- **Quota / limit**: A numeric cap on a capability (e.g. max bots per guild, AI spend cap).
- **Default entitlement set**: The conservative capability set applied to a user who has no explicit entitlement record (secure-by-default).
- **AI usage**: An invocation of an AI/LLM feature attributable to a user, incurring a Bedrock cost.
- **Bedrock unit cost**: The Amazon Bedrock price (per input/output token or per request) for a model, which changes over time.
- **Markup**: A multiplier applied on top of Bedrock unit cost when billing/tallying a user (baseline 100% markup = 2× Bedrock cost).
- **Effective cost**: `bedrock_cost × (1 + markup)`, the amount charged against a user's AI tally.
- **Bot instance**: One running bot presence attached to a guild for a user; users may be limited to N per guild.
- **Enforcement point**: A place in the web-ui or bot where an entitlement is checked before allowing an action.
- **CoreTable**: The shared `hellodj_platform_logic.data_access.CoreTable` repository over the `hellodj-core` single table.

## Requirements

### Requirement 1: Distinct administrative control plane

**User Story:** As an administrator, I want a dedicated admin section that is clearly separate from the user-facing Dashboard/Config/Guilds pages, so that I can govern the platform rather than only manage my own account.

#### Acceptance Criteria

1. WHEN an administrator navigates to the admin section THEN the web-ui SHALL present administrative capabilities (user governance, entitlements, usage metering) distinct from the self-service Dashboard, Config, and Guilds pages.
2. IF the authenticated account is not in the Cognito `admins` group THEN the web-ui SHALL NOT render, route to, or otherwise present any admin entitlement capability, and SHALL redirect the request to the dashboard before any admin content is produced.
3. IF the redirect for an unauthorized request fails or is bypassed (e.g. direct URL manipulation) THEN the web-ui SHALL apply an explicit fallback that denies access — rendering an error page or forcing logout — rather than serving admin content.
4. WHERE the current session belongs to an administrator THE web-ui SHALL show the admin navigation entry; WHERE it does not THE web-ui SHALL omit it.

### Requirement 2: Per-user entitlement management view

**User Story:** As an administrator, I want to select a user and see all their entitlement flags and quotas in one place, so that I can review and change what that user can do.

#### Acceptance Criteria

1. WHEN an administrator opens a user's entitlement view THEN the web-ui SHALL display the user's identity (username, email) and the current value of every entitlement flag and quota defined by this spec.
2. WHEN a user has no stored entitlement record THEN the web-ui SHALL display the default entitlement set and indicate the values are defaults (not explicitly set).
3. WHEN an administrator changes any entitlement flag or quota and saves THEN the web-ui SHALL persist the change to the user's entitlement record on `hellodj-core` and reflect the saved values on the next render.
4. IF any failure occurs during the save operation (persistence error, validation error, or network timeout) THEN the web-ui SHALL surface an error notice and SHALL NOT report the change as saved.

### Requirement 3: Allowed sources per user

**User Story:** As an administrator, I want to control which playback sources each user may use, so that I can restrict access to specific providers per account.

#### Acceptance Criteria

1. WHEN an administrator toggles a source (YouTube, YouTube Music, SoundCloud, Spotify, Tidal) for a user THEN the entitlement record SHALL store the allowed/disallowed state for that source for that user.
2. WHEN a user attempts playback from a source THEN the enforcing component SHALL allow it only IF that source is enabled in the user's effective entitlements.
3. WHERE a source is enabled in a user's effective entitlements THE enforcing component SHALL permit playback from that source automatically without additional per-request approval.
4. IF a source is disabled for a user THEN the bot SHALL decline playback requests for that source with a message stating the source is not permitted for that user.

### Requirement 4: Allowed custom bot identity per user

**User Story:** As an administrator, I want to control whether each user may customize the bot's avatar and name, so that identity personalization is a gated capability.

#### Acceptance Criteria

1. WHEN an administrator toggles "custom avatar allowed" for a user THEN the web-ui SHALL flip the flag to its opposite state and the entitlement record SHALL store the new value for that user.
2. WHEN an administrator toggles "custom name allowed" for a user THEN the web-ui SHALL flip the flag to its opposite state and the entitlement record SHALL store the new value for that user.
3. IF "custom avatar allowed" is disabled for a user THEN the enforcing component SHALL reject that user's attempts to set a custom bot avatar.
4. IF "custom name allowed" is disabled for a user THEN the enforcing component SHALL reject that user's attempts to set a custom bot name.

### Requirement 5: Allowed high-bitrate audio per user

**User Story:** As an administrator, I want to control whether each user may stream audio above 96 kbps, so that higher-quality audio is a gated capability.

#### Acceptance Criteria

1. WHEN an administrator toggles "audio above 96 kbps allowed" for a user THEN the entitlement record SHALL store that flag for that user.
2. IF the flag is disabled for a user THEN the enforcing component SHALL cap that user's audio bitrate at 96 kbps or lower at stream initiation; a stream already in progress MAY continue at its current bitrate until it restarts.
3. WHERE the flag is enabled for a user THE enforcing component SHALL permit audio bitrates above 96 kbps for that user.

### Requirement 6: Allowed video activities per user

**User Story:** As an administrator, I want to control whether each user may use Discord video activities, so that the video streaming feature is a gated capability.

#### Acceptance Criteria

1. WHEN an administrator toggles "video activities allowed" for a user THEN the entitlement record SHALL store that flag for that user.
2. IF the flag is disabled for a user THEN the bot SHALL decline that user's requests to start a video activity with a message stating the capability is not permitted.
3. WHEN a user requests a video activity THEN the bot SHALL always return a response (permit or explicit decline) rather than failing silently.
4. WHERE the flag is enabled for a user THE bot SHALL permit that user to start video activities.

### Requirement 7: Allowed visualizations per user

**User Story:** As an administrator, I want to control whether each user may use audio visualizations, so that the visualizer feature is a gated capability.

#### Acceptance Criteria

1. WHEN an administrator toggles "visualizations allowed" for a user THEN the entitlement record SHALL store that flag for that user.
2. IF the flag is disabled for a user THEN the bot SHALL decline that user's requests to start a visualizer with a message stating the capability is not permitted.
3. WHERE the flag is enabled for a user THE bot SHALL permit that user to start visualizations.

### Requirement 8: Allowed wake-word / voice activation per user

**User Story:** As an administrator, I want to control whether each user may use wake-word voice activation, so that the voice pipeline is a gated capability.

#### Acceptance Criteria

1. WHEN an administrator toggles "wake-word/voice activation allowed" for a user THEN the entitlement record SHALL store that flag for that user.
2. IF the flag is disabled for a user THEN the bot SHALL NOT act on that user's voice commands regardless of input method (wake-word, push-to-talk, or otherwise).
3. WHERE the flag is enabled for a user THE bot SHALL require a wake-word activation before processing any voice input from that user.

### Requirement 9: Allowed AI integration per user

**User Story:** As an administrator, I want to control whether each user may use AI integration, so that AI features (and their cost) are a gated capability.

#### Acceptance Criteria

1. WHEN an administrator toggles "AI integration allowed" for a user THEN the entitlement record SHALL store that flag for that user.
2. IF the flag is disabled for a user THEN the bot SHALL decline that user's AI-backed requests without incurring an AI cost for that user.
3. IF an AI request from a user is not explicitly declined when it should be THEN the bot SHALL treat it as an error and block it entirely rather than allowing it to proceed.
4. WHERE the flag is enabled for a user THE bot SHALL permit AI requests, SHALL meter their cost immediately when the request is permitted (per Requirement 10), and SHALL not defer metering until the AI service completes.

### Requirement 10: AI usage cost metering with markup

**User Story:** As an administrator, I want a per-user tally of AI usage cost, marked up over Bedrock's prices, so that I can see and bill each user's AI consumption.

#### Acceptance Criteria

1. WHEN a user completes an AI request THEN the system SHALL compute the Bedrock unit cost for that request and record an effective cost of `bedrock_cost × (1 + markup)` against that user's AI tally.
2. WHERE no markup is explicitly configured THE system SHALL apply a default markup of 100% (effective cost = 2× Bedrock cost).
3. WHEN Bedrock pricing changes THEN the system SHALL use the updated per-model unit prices for cost computation without requiring a code change to the enforcement logic (prices are configuration/data, not hard-coded constants).
4. WHEN an administrator opens a user's entitlement view THEN the web-ui SHALL display that user's accumulated AI cost tally.
5. WHERE a per-user AI spend cap is configured AND the user's tally is equal to or greater than the cap THE system SHALL flag the user as over-cap and surface a warning, but SHALL NOT hard-block AI requests solely due to the cap.
6. WHEN an administrator resets a user's AI tally THEN the system SHALL set the accumulated cost for that user back to zero and record the reset.

### Requirement 11: Multiple bots per guild limit per user

**User Story:** As an administrator, I want to control whether a user may run more than one bot instance per guild and set the maximum, so that per-guild bot fan-out is a gated, quantified capability.

#### Acceptance Criteria

1. WHEN an administrator sets a user's "max bots per guild" quota THEN the entitlement record SHALL store that numeric limit for that user.
2. WHEN a user attempts to add a bot instance to a guild AND the count of that user's active bot instances in that guild is already at the user's "max bots per guild" limit THEN the enforcing component SHALL reject the request with a message stating the per-guild bot limit is reached.
3. WHERE the "max bots per guild" quota is marked disabled BUT the stored numeric value is greater than 1 THE enforcing component SHALL apply the stored numeric value as the per-guild limit (the stored value takes effect regardless of the disabled marker).
4. WHERE "max bots per guild" is set to 1 for a user THE enforcing component SHALL permit at most one bot instance per guild for that user.

### Requirement 12: Multiple guilds limit per user

**User Story:** As an administrator, I want to control whether a user may operate in more than one guild and set the maximum total bots across guilds, so that a user's overall footprint is bounded.

#### Acceptance Criteria

1. WHEN an administrator sets a user's "max guilds" quota THEN the entitlement record SHALL store that numeric limit for that user.
2. WHEN an administrator submits a quota value (max guilds or max bots per guild) THEN the web-ui SHALL require the value to be at least 1 and SHALL reject a value of 0 or below with a validation error.
3. WHEN a user attempts to activate the bot in an additional guild AND the count of the user's active guilds is already at the user's "max guilds" limit THEN the enforcing component SHALL reject the request with a message stating the guild limit is reached.
4. WHERE bots span multiple guilds THE per-guild bot counts (Requirement 11) SHALL each count toward the user's per-guild limit, and the number of distinct active guilds SHALL count toward the user's "max guilds" limit.

### Requirement 13: Secure default entitlements

**User Story:** As an administrator, I want new users to start with conservative defaults, so that no user gains a gated capability until it is explicitly granted.

#### Acceptance Criteria

1. WHEN a user has no stored entitlement record THEN the enforcing component SHALL apply the default entitlement set rather than treating the user as fully permitted.
2. WHERE the default entitlement set is applied THE gated capabilities (custom identity, high-bitrate audio, video activities, visualizations, wake-word, AI integration, multi-bot, multi-guild beyond the baseline) SHALL default to their most restrictive permitted state, and custom identity (avatar and name) SHALL specifically default to restricted (not allowed).
3. WHEN an administrator explicitly sets any entitlement for a user THEN that explicit value SHALL take precedence over the default for that field.

### Requirement 14: Runtime enforcement by the bot

**User Story:** As an administrator, I want the bot to actually enforce entitlements at runtime, so that disabling a capability in the admin panel takes real effect in Discord rather than being cosmetic.

#### Acceptance Criteria

1. WHEN the bot handles a user action governed by an entitlement THEN the bot SHALL resolve that user's effective entitlements (explicit record merged over defaults) before permitting the action.
2. WHEN an administrator changes a user's entitlement THEN the bot SHALL observe the updated value for subsequent actions without requiring a redeploy; the bot MAY cache entitlements for performance PROVIDED it refreshes them on a bounded periodic interval so changes take effect within that interval.
3. IF the bot cannot resolve a user's entitlements (datastore unavailable) THEN the bot SHALL fail safe by applying the default (restrictive) entitlement set rather than granting all capabilities.

### Requirement 15: Auditability of entitlement changes

**User Story:** As an administrator, I want entitlement changes to be traceable, so that I can see who changed what and when.

#### Acceptance Criteria

1. WHEN an administrator changes a user's entitlement flag, quota, markup, or resets an AI tally THEN the system SHALL record which administrator made the change, the affected user, the field changed, and the time of the change.
2. IF the audit record cannot be written (recording system fails or is unavailable) THEN the system SHALL block the entitlement change until the record succeeds, rather than applying the change without an audit entry.
3. WHEN an administrator views a user's entitlement history THEN the web-ui SHALL display the recorded changes in reverse-chronological order.
