# Requirements Document

## Introduction

Rewrite the TidalSourceManager in LavasRC (version 4.8.3) to use the Tidal v2 API (`openapi.tidal.com/v2`) instead of the legacy v1 API (`api.tidal.com/v1`). The v1 API requires the deprecated `r_usr` scope which is no longer obtainable via modern OAuth flows. The v2 API uses JSON:API format, supports `client_credentials` and authorization-code PKCE flows, and works with modern OAuth scopes (`search.read`, `playback`, `collection.read`, etc.) that the user already possesses.

This change enables LavasRC's Tidal source to function with modern OAuth tokens obtained via the Tidal developer portal device flow, restoring Tidal search and track resolution within the Lavalink plugin pipeline.

## Glossary

- **LavasRC**: A Lavalink plugin (Java/Kotlin) that adds Spotify, Tidal, Deezer, and other metadata sources to Lavalink for track resolution
- **TidalSourceManager**: The Java class in LavasRC responsible for searching Tidal, loading tracks/albums/playlists, and resolving Tidal metadata to playable audio sources
- **Tidal_V2_API**: The Tidal REST API at `https://openapi.tidal.com/v2` using JSON:API specification (`application/vnd.api+json` content type)
- **Tidal_V1_API**: The legacy Tidal REST API at `https://api.tidal.com/v1` using plain JSON responses, requiring the deprecated `r_usr` scope
- **JSON_API**: The JSON:API specification (jsonapi.org) — a structured format where resources have `type`, `id`, `attributes`, and `relationships` fields, with included resources in a top-level `included` array
- **OAuth_Token**: A Bearer access token obtained via OAuth 2.0 client_credentials or authorization_code PKCE flow from `https://auth.tidal.com/v1/oauth2/token`
- **Track_Resolution**: The process of converting a Tidal track identifier or search result into metadata (title, artist, ISRC, duration) that LavasRC uses to find a playable stream via configured providers
- **Cursor_Pagination**: The v2 API's pagination mechanism using opaque cursor tokens rather than offset/limit parameters
- **Lavalink_Plugin**: A JAR file loaded by the Lavalink audio server that extends its source manager capabilities
- **application.yml**: The Lavalink configuration file where LavasRC plugin settings (client ID, client secret, token, country code) are declared

## Requirements

### Requirement 1: OAuth 2.0 Token Acquisition

**User Story:** As an operator deploying HelloDJ, I want LavasRC to authenticate with the Tidal v2 API using standard OAuth 2.0 client_credentials flow, so that modern OAuth tokens work without the deprecated `r_usr` scope.

#### Acceptance Criteria

1. WHEN the TidalSourceManager initializes, THE TidalSourceManager SHALL authenticate against `https://auth.tidal.com/v1/oauth2/token` using the client_credentials grant type with the configured clientId and clientSecret
2. WHEN the OAuth token response is received, THE TidalSourceManager SHALL extract the access_token and expires_in fields and cache the token for reuse until expiry
3. WHEN a cached token is within 60 seconds of expiry, THE TidalSourceManager SHALL proactively refresh the token before the next API call
4. IF the token endpoint returns an HTTP error, THEN THE TidalSourceManager SHALL log the error and retry once after a 1-second delay before failing the operation
5. WHEN a pre-configured static token is provided via the `token` field in application.yml, THE TidalSourceManager SHALL use that token directly as the Bearer token without performing the client_credentials flow

### Requirement 2: Track Search via v2 API

**User Story:** As a Discord user, I want to search for Tidal tracks through the bot, so that I can find and play music from Tidal's catalog.

#### Acceptance Criteria

1. WHEN a search query is submitted, THE TidalSourceManager SHALL send a GET request to `https://openapi.tidal.com/v2/searchresults/{query}` with the `include=tracks` query parameter and `Content-Type: application/vnd.api+json` accept header
2. WHEN the v2 search response is received, THE TidalSourceManager SHALL parse the JSON:API `included` array to extract track resources with type `tracks`
3. THE TidalSourceManager SHALL map each track resource's `attributes` (title, duration, isrc, trackNumber) and `relationships` (artists, albums) to LavasRC's internal AudioTrack metadata format
4. WHEN the search returns zero results, THE TidalSourceManager SHALL return an empty search result without raising an error
5. THE TidalSourceManager SHALL respect the configured `searchLimit` parameter by passing it as the `page[limit]` query parameter to the v2 API

### Requirement 3: Track Loading by ID

**User Story:** As a Discord user, I want to play a specific Tidal track by URL or ID, so that shared Tidal links resolve to playable audio.

#### Acceptance Criteria

1. WHEN a Tidal track URL (matching `tidal.com/track/{id}` or `tidal.com/browse/track/{id}`) is provided, THE TidalSourceManager SHALL extract the track ID and send a GET request to `https://openapi.tidal.com/v2/tracks/{id}` with appropriate includes
2. WHEN the v2 track response is received, THE TidalSourceManager SHALL parse the JSON:API `data` object and map its attributes to LavasRC's AudioTrack metadata
3. THE TidalSourceManager SHALL resolve artist names from the `relationships.artists` data by fetching included artist resources or following relationship links
4. IF the track ID does not exist, THEN THE TidalSourceManager SHALL return a "no matches" result rather than throwing an exception

### Requirement 4: Album Loading

**User Story:** As a Discord user, I want to load all tracks from a Tidal album link, so that I can queue an entire album for playback.

#### Acceptance Criteria

1. WHEN a Tidal album URL (matching `tidal.com/album/{id}` or `tidal.com/browse/album/{id}`) is provided, THE TidalSourceManager SHALL send a GET request to `https://openapi.tidal.com/v2/albums/{id}` with `include=items` or the appropriate relationship endpoint for album tracks
2. WHEN the v2 album response is received, THE TidalSourceManager SHALL extract all track resources from the album's items relationship
3. WHILE the album's track list has additional pages (cursor-based pagination), THE TidalSourceManager SHALL follow pagination cursors to load all tracks up to the configured albumLoadLimit
4. IF the album ID does not exist, THEN THE TidalSourceManager SHALL return a "no matches" result

### Requirement 5: Playlist Loading

**User Story:** As a Discord user, I want to load Tidal playlists by URL, so that shared playlists can be queued.

#### Acceptance Criteria

1. WHEN a Tidal playlist URL (matching `tidal.com/playlist/{uuid}` or `tidal.com/browse/playlist/{uuid}`) is provided, THE TidalSourceManager SHALL send a GET request to the v2 playlist items endpoint
2. WHEN the v2 playlist response is received, THE TidalSourceManager SHALL extract all track resources from the playlist items
3. WHILE the playlist has additional pages, THE TidalSourceManager SHALL follow pagination cursors to load tracks up to the configured playlistLoadLimit
4. IF the playlist UUID does not exist or is private, THEN THE TidalSourceManager SHALL return a "no matches" result

### Requirement 6: JSON:API Response Parsing

**User Story:** As a developer maintaining LavasRC, I want a robust JSON:API parser for Tidal v2 responses, so that the code correctly handles the structured response format.

#### Acceptance Criteria

1. THE JSON_API_Parser SHALL extract the primary resource from the `data` field (single object for item lookups, array for collections)
2. THE JSON_API_Parser SHALL resolve `relationships` references by matching `type` and `id` against the top-level `included` array
3. WHEN a relationship references a resource not present in the `included` array, THE JSON_API_Parser SHALL skip that relationship without raising an error
4. THE JSON_API_Parser SHALL handle pagination metadata from the `links` or `meta` fields to determine if additional pages exist
5. FOR ALL valid JSON:API track responses, parsing then formatting back to internal metadata then re-serializing SHALL produce equivalent track metadata (round-trip property)

### Requirement 7: v2 API Request Headers and Content Negotiation

**User Story:** As an operator, I want LavasRC to send correctly formatted requests to the Tidal v2 API, so that requests are accepted and responses are properly interpreted.

#### Acceptance Criteria

1. THE TidalSourceManager SHALL include the `Authorization: Bearer {token}` header on every v2 API request
2. THE TidalSourceManager SHALL include the `Accept: application/vnd.api+json` header on every v2 API request
3. THE TidalSourceManager SHALL include the `Content-Type: application/vnd.api+json` header on any request with a body
4. WHEN the countryCode configuration is set, THE TidalSourceManager SHALL include it as a query parameter (e.g., `filter[countryCode]=US`) on search and catalog requests

### Requirement 8: Error Handling and Resilience

**User Story:** As an operator, I want LavasRC to handle Tidal API errors gracefully, so that transient failures do not crash the plugin or pollute logs excessively.

#### Acceptance Criteria

1. IF the Tidal v2 API returns HTTP 401, THEN THE TidalSourceManager SHALL invalidate the cached token, re-authenticate, and retry the request once
2. IF the Tidal v2 API returns HTTP 429 (rate limited), THEN THE TidalSourceManager SHALL respect the Retry-After header and delay before retrying
3. IF the Tidal v2 API returns HTTP 5xx, THEN THE TidalSourceManager SHALL retry the request once after a 2-second delay
4. IF all retry attempts fail, THEN THE TidalSourceManager SHALL log the failure at WARN level and return a load-failed result to Lavalink
5. WHILE the Tidal source is enabled but credentials are missing or invalid, THE TidalSourceManager SHALL log a single warning at startup and disable Tidal operations without affecting other LavasRC sources

### Requirement 9: Configuration Backward Compatibility

**User Story:** As an operator, I want the plugin configuration to remain backward-compatible with existing application.yml settings, so that upgrading the plugin JAR does not require config changes.

#### Acceptance Criteria

1. THE TidalSourceManager SHALL read the same configuration keys as the current version: `clientId`, `clientSecret`, `token`, `countryCode`, `searchLimit`
2. WHEN only a static `token` is configured (without clientId/clientSecret), THE TidalSourceManager SHALL use the token as a Bearer token directly against the v2 API
3. WHEN both clientId/clientSecret and token are configured, THE TidalSourceManager SHALL prefer the client_credentials flow and ignore the static token
4. THE TidalSourceManager SHALL default countryCode to "US" and searchLimit to 6 when not explicitly configured

### Requirement 10: ISRC-Based Track Matching

**User Story:** As a developer, I want LavasRC to extract ISRC codes from Tidal tracks, so that cross-platform track matching (Spotify→Tidal) works accurately.

#### Acceptance Criteria

1. WHEN a Tidal track is loaded, THE TidalSourceManager SHALL extract the ISRC code from the track's attributes and include it in the AudioTrack metadata
2. WHEN an ISRC-based search is requested (e.g., `tdsearch:isrc:{code}`), THE TidalSourceManager SHALL use the v2 API search with an ISRC filter to find the matching track
3. IF the ISRC field is absent from the track attributes, THEN THE TidalSourceManager SHALL proceed without ISRC metadata rather than failing the load

