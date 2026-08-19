# Implementation Plan: LavasRC Tidal v2 API Migration

## Overview

Rewrite the TidalSourceManager in the celesrenata/LavaSrc fork to use the Tidal v2 API (`openapi.tidal.com/v2`) with JSON:API response format, replacing the legacy v1 API. Implementation is broken into discrete components: internal data model, JSON:API parser, token manager, API client, and finally rewiring the source manager. Property-based tests validate correctness properties from the design; unit tests cover error handling and edge cases.

## Tasks

- [x] 1. Create internal data model and URL parser
  - [x] 1.1 Create TidalTrackInfo value class
    - Create `main/java/com/github/topi314/lavasrc/tidal/TidalTrackInfo.java`
    - Implement fields: id, title, artistName, albumName, albumArtUrl, isrc, durationMs, trackNumber, uri
    - Implement `toAudioTrackInfo()` method that converts to Lavaplayer's AudioTrackInfo
    - Construct the canonical URI as `https://tidal.com/track/{id}`
    - _Requirements: 2.3, 3.2, 10.1_

  - [x] 1.2 Implement URL pattern matching and extraction
    - Add URL regex patterns for track, album, and playlist (with and without `/browse/` prefix)
    - Support patterns: `tidal.com/track/{id}`, `tidal.com/browse/track/{id}`, `tidal.com/album/{id}`, `tidal.com/browse/album/{id}`, `tidal.com/playlist/{uuid}`, `tidal.com/browse/playlist/{uuid}`
    - Support `tdsearch:{query}` and `tdsearch:isrc:{code}` prefixes
    - Return extracted resource type and ID
    - _Requirements: 3.1, 4.1, 5.1, 10.2_

  - [ ]* 1.3 Write property test for URL pattern extraction (Property 2)
    - **Property 2: URL Pattern Extraction Round-Trip**
    - Generate random numeric IDs and UUIDs, embed in all supported URL variants
    - Verify extracted type+ID reconstructs an equivalent reference
    - Create `main/test/java/com/github/topi314/lavasrc/tidal/TidalUrlParserPropertyTest.java`
    - **Validates: Requirements 3.1, 4.1, 5.1**

- [x] 2. Implement JSON:API parser
  - [x] 2.1 Create JsonApiParser utility class
    - Create `main/java/com/github/topi314/lavasrc/tidal/JsonApiParser.java`
    - Implement `parseTrack(JsonNode resource, JsonNode included)` → TidalTrackInfo
    - Implement `parseTracks(JsonNode dataArray, JsonNode included)` → List<TidalTrackInfo>
    - Implement `resolveRelationship(String type, String id, JsonNode included)` → Optional<JsonNode>
    - Implement `getNextPageUrl(JsonNode document)` → Optional<String>
    - Parse ISO 8601 duration (`PT3M45S`) to milliseconds using `java.time.Duration.parse()`
    - Resolve artist names from `relationships.artists` via the `included` array
    - Resolve album name and artwork from `relationships.albums` via the `included` array
    - Skip missing relationships gracefully (return empty, no errors)
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [ ]* 2.2 Write property test for relationship resolution (Property 3)
    - **Property 3: JSON:API Relationship Resolution**
    - Generate random `included` arrays with various types and IDs
    - For references present in included: verify correct resource resolved
    - For references NOT in included: verify empty returned without error
    - Create `main/test/java/com/github/topi314/lavasrc/tidal/JsonApiParserPropertyTest.java`
    - **Validates: Requirements 6.2, 6.3**

  - [ ]* 2.3 Write property test for track metadata mapping (Property 4)
    - **Property 4: Track Metadata Mapping Completeness**
    - Generate random valid track JSON resources with attributes and relationships
    - Verify title, parsed duration (ms), isrc, author, and URI are all correctly mapped
    - Add to `JsonApiParserPropertyTest.java`
    - **Validates: Requirements 2.3, 3.2, 10.1**

  - [ ]* 2.4 Write property test for pagination completeness (Property 5)
    - **Property 5: Pagination Completeness Up To Limit**
    - Generate random paginated sequences with N total items, arbitrary page sizes, and limit L
    - Verify exactly min(N, L) tracks are accumulated
    - Add to `JsonApiParserPropertyTest.java`
    - **Validates: Requirements 4.3, 5.3**

  - [ ]* 2.5 Write property test for track parsing round-trip (Property 6)
    - **Property 6: Track Parsing Round-Trip**
    - Generate random valid track JSON documents
    - Parse into TidalTrackInfo, verify stored field values equal original JSON attributes
    - Add to `JsonApiParserPropertyTest.java`
    - **Validates: Requirements 6.5**

- [x] 3. Checkpoint - Ensure parser tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 4. Implement OAuth token manager
  - [x] 4.1 Create TidalTokenManager class
    - Create `main/java/com/github/topi314/lavasrc/tidal/TidalTokenManager.java`
    - Implement `client_credentials` flow against `https://auth.tidal.com/v1/oauth2/token`
    - Cache token with expiry tracking; proactively refresh within 60 seconds of expiry
    - Support static token mode (use token directly when no clientId/clientSecret)
    - Prefer client_credentials when both static token and credentials are configured
    - Implement `getToken()` (synchronized, returns valid Bearer token)
    - Implement `invalidateToken()` (force re-auth on 401 responses)
    - Retry once after 1s on auth endpoint errors; throw `TidalAuthException` on final failure
    - Log WARN and set disabled flag if credentials missing at startup
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 9.2, 9.3_

  - [ ]* 4.2 Write property test for token lifecycle (Property 1)
    - **Property 1: Token Lifecycle Correctness**
    - Generate random token strings and expiry durations (> 60s)
    - Mock clock to advance time; verify cached token returned until within 60s of expiry
    - Verify refresh triggered at threshold
    - Create `main/test/java/com/github/topi314/lavasrc/tidal/TidalTokenManagerPropertyTest.java`
    - **Validates: Requirements 1.2, 1.3**

  - [ ]* 4.3 Write unit tests for token manager
    - Test static token mode (returns token without HTTP call)
    - Test client_credentials precedence over static token when both configured
    - Test retry behavior on auth endpoint error
    - Test token invalidation triggers re-auth
    - Create `main/test/java/com/github/topi314/lavasrc/tidal/TidalTokenManagerTest.java`
    - _Requirements: 1.1, 1.4, 1.5, 9.2, 9.3_

- [x] 5. Implement v2 API client
  - [x] 5.1 Create TidalV2ApiClient class
    - Create `main/java/com/github/topi314/lavasrc/tidal/TidalV2ApiClient.java`
    - Set `BASE_URL = "https://openapi.tidal.com/v2"` and `JSONAPI_MEDIA_TYPE = "application/vnd.api+json"`
    - Implement `searchTracks(String query, int limit)` → GET `/v2/searchresults/{query}?include=tracks&page[limit]={limit}&filter[countryCode]={cc}`
    - Implement `getTrack(String trackId)` → GET `/v2/tracks/{id}?include=artists,albums`
    - Implement `getAlbum(String albumId)` → GET `/v2/albums/{id}?include=items`
    - Implement `getPlaylist(String playlistUuid)` → GET `/v2/playlists/{uuid}?include=items`
    - Implement `getAlbumTracks(String albumId, int maxTracks)` with cursor pagination
    - Implement `getPlaylistTracks(String playlistUuid, int maxTracks)` with cursor pagination
    - All requests include `Authorization: Bearer {token}` and `Accept: application/vnd.api+json`
    - Include `filter[countryCode]` on search/catalog requests when configured
    - URL-encode query strings properly (including unicode and special characters)
    - _Requirements: 2.1, 2.5, 3.1, 4.1, 4.3, 5.1, 5.3, 7.1, 7.2, 7.3, 7.4_

  - [x] 5.2 Implement error handling and retry logic in TidalV2ApiClient
    - On HTTP 401: invalidate token via TidalTokenManager, re-auth, retry request once
    - On HTTP 429: read `Retry-After` header, sleep, retry once
    - On HTTP 5xx: sleep 2s, retry once
    - On HTTP 404: return null/empty (no-matches) without retry
    - On final failure (all retries exhausted): log WARN, throw `TidalApiException`
    - _Requirements: 8.1, 8.2, 8.3, 8.4_

  - [ ]* 5.3 Write property test for search request formation (Property 7)
    - **Property 7: Search Request Formation**
    - Generate random query strings (unicode, special chars, ISRC-prefixed)
    - Verify URL encoding is correct, `include=tracks` present, `page[limit]` matches config, `filter[countryCode]` matches config
    - Create `main/test/java/com/github/topi314/lavasrc/tidal/TidalSearchPropertyTest.java`
    - **Validates: Requirements 2.1, 2.5, 7.4, 10.2**

  - [ ]* 5.4 Write unit tests for API client error handling
    - Mock OkHttp responses for 401, 429, 5xx, 404 scenarios
    - Verify retry behavior and token invalidation
    - Verify Retry-After header is respected
    - Verify 404 returns no-matches without exception
    - Create `main/test/java/com/github/topi314/lavasrc/tidal/TidalV2ApiClientTest.java`
    - _Requirements: 8.1, 8.2, 8.3, 8.4_

- [x] 6. Checkpoint - Ensure all component tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Rewire TidalSourceManager to use v2 components
  - [x] 7.1 Refactor TidalSourceManager initialization
    - Modify `main/java/com/github/topi314/lavasrc/tidal/TidalSourceManager.java`
    - Instantiate TidalTokenManager, TidalV2ApiClient, and JsonApiParser in constructor
    - Read configuration: clientId, clientSecret, token, countryCode (default "US"), searchLimit (default 6)
    - If credentials missing/invalid: log WARN once, disable Tidal operations, do not affect other sources
    - Remove all v1 API code paths and references to `api.tidal.com/v1`
    - _Requirements: 9.1, 9.4, 8.5_

  - [x] 7.2 Implement search via v2 API in TidalSourceManager
    - Replace existing search logic with call to `TidalV2ApiClient.searchTracks()`
    - Parse response through `JsonApiParser.parseTracks()` to get List<TidalTrackInfo>
    - Convert to List<AudioTrack> using `toAudioTrackInfo()`
    - Return empty result (not error) when search yields zero results
    - Handle ISRC-based search (`tdsearch:isrc:{code}`) by passing ISRC filter to v2 API
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 10.2_

  - [x] 7.3 Implement track/album/playlist loading via v2 API
    - Replace track loading: extract ID from URL → `TidalV2ApiClient.getTrack()` → parse → AudioTrack
    - Replace album loading: extract ID → `TidalV2ApiClient.getAlbumTracks()` → parse → List<AudioTrack>
    - Replace playlist loading: extract UUID → `TidalV2ApiClient.getPlaylistTracks()` → parse → List<AudioTrack>
    - Return "no matches" for 404 / nonexistent resources (no exceptions)
    - Respect albumLoadLimit and playlistLoadLimit configuration
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 4.1, 4.2, 4.3, 4.4, 5.1, 5.2, 5.3, 5.4_

  - [x] 7.4 Ensure ISRC extraction in track metadata
    - When loading/searching tracks, extract ISRC from track attributes and include in AudioTrack metadata
    - Proceed gracefully when ISRC is absent (do not fail the load)
    - _Requirements: 10.1, 10.3_

  - [ ]* 7.5 Write unit tests for TidalSourceManager integration
    - Test search flow end-to-end with mocked API client
    - Test track/album/playlist loading with mocked responses
    - Test 404 → no-matches behavior
    - Test disabled state (missing credentials) returns empty without error
    - Test default config values (countryCode="US", searchLimit=6)
    - Create `main/test/java/com/github/topi314/lavasrc/tidal/TidalSourceManagerTest.java`
    - _Requirements: 2.4, 3.4, 4.4, 5.4, 8.5, 9.1, 9.4_

- [x] 8. Build and package plugin JAR
  - [x] 8.1 Build the LavasRC plugin JAR
    - Run `./gradlew :plugin:shadowJar` in the celesrenata/LavaSrc fork
    - Verify the JAR compiles without errors
    - Run `./gradlew :main:test` to execute all unit and property tests
    - Copy built JAR to `kube/lavalink/plugins/lavasrc-plugin-4.8.3.jar` in the hellodj repo
    - _Requirements: 1.1, 2.1, 3.1, 4.1, 5.1, 6.1, 7.1, 8.1, 9.1, 10.1_

- [x] 9. Final checkpoint - Ensure all tests pass and JAR builds
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation
- Property tests validate universal correctness properties from the design document
- Unit tests validate specific examples and edge cases
- The LavasRC fork is at `celesrenata/LavaSrc` — source path is `main/java/com/github/topi314/lavasrc/tidal/`
- Tests use jqwik for property-based testing (JUnit 5 platform)
- The built JAR is deployed by copying to `kube/lavalink/plugins/lavasrc-plugin-4.8.3.jar` and rebuilding the custom Lavalink image (handled outside this task list)
- Deployment (docker build, kubectl apply, rollout restart) is handled separately after the code is verified

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["1.3", "2.1"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4", "2.5", "4.1"] },
    { "id": 3, "tasks": ["4.2", "4.3", "5.1"] },
    { "id": 4, "tasks": ["5.2", "5.3", "5.4"] },
    { "id": 5, "tasks": ["7.1"] },
    { "id": 6, "tasks": ["7.2", "7.3", "7.4"] },
    { "id": 7, "tasks": ["7.5"] },
    { "id": 8, "tasks": ["8.1"] }
  ]
}
```
