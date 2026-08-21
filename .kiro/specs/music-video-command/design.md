# Design Document: Music Video Command

## Overview

The Music Video Command feature adds a `MusicVideoResolver` module that classifies user input (URLs or text searches) into a source type, resolves the input to a `VideoSource`, and routes it through the existing Video Activity pipeline (ActivityStreamer → HLSTranscodePipeline → Activity iframe). The module handles YouTube, YouTube Music, Tidal video, Tidal track-to-video, Spotify track-to-video, and plain text searches with source priority logic that favors native Tidal videos over YouTube when available.

The resolver lives at `bot/video/music_video_resolver.py` as a standalone module that composes the existing `YouTubeResolver` and `TidalResolver` with a new `MusicVideoQueryClassifier` and `SpotifyMetadataExtractor`. It integrates with the `/play music_video` subcommand in the Music cog.

## Architecture

```mermaid
flowchart TD
    A["/play music_video <query>"] --> B[MusicVideoQueryClassifier]
    B --> C{Classification}
    C -->|youtube_direct| D[YouTubeResolver.resolve]
    C -->|youtube_music| E[Extract video ID → YouTubeResolver.resolve]
    C -->|tidal_video| F[TidalResolver.resolve_url]
    C -->|tidal_track| G[Tidal Track-to-Video Lookup]
    C -->|spotify_track| H[SpotifyMetadataExtractor]
    C -->|text_search| I[YouTube Search with "official music video" suffix]
    
    G -->|video exists| F
    G -->|no video| J[Fallback YouTube Search]
    H --> J
    
    D --> K[VideoSource]
    E --> K
    F --> K
    I --> K
    J --> K
    
    K --> L{Active Session?}
    L -->|No| M[Launch Activity + Play]
    L -->|Yes| N[Enqueue to Session]
```

The architecture layers:

1. **Classification Layer** — `MusicVideoQueryClassifier`: Pure function, no I/O. Regex-based URL pattern matching to determine source type.
2. **Metadata Layer** — `SpotifyMetadataExtractor`: Async HTTP calls to Spotify Web API for track metadata extraction.
3. **Resolution Layer** — `MusicVideoResolver`: Orchestrates classification → metadata extraction → sub-resolver dispatch → fallback logic.
4. **Integration Layer** — Wires into the existing VideoCog command handler pattern (voice check → defer → resolve → session route).

## Components and Interfaces

### MusicVideoQueryClassifier

Pure, synchronous classifier. No I/O, no async.

```python
from dataclasses import dataclass
from enum import Enum
from typing import Literal

class MusicVideoSourceType(Enum):
    YOUTUBE_DIRECT = "youtube_direct"
    YOUTUBE_MUSIC = "youtube_music"
    TIDAL_VIDEO = "tidal_video"
    TIDAL_TRACK = "tidal_track"
    SPOTIFY_TRACK = "spotify_track"
    TEXT_SEARCH = "text_search"

@dataclass(frozen=True)
class MusicVideoClassification:
    source_type: MusicVideoSourceType
    original_query: str
    extracted_id: str | None = None  # video/track ID when extractable from URL

def classify_music_video_query(query: str) -> MusicVideoClassification:
    """Classify a user query into exactly one MusicVideoSourceType.
    
    Classification rules (in priority order):
    1. youtube.com or youtu.be domain → youtube_direct
    2. music.youtube.com domain → youtube_music (extract video ID)
    3. tidal.com with /video/ or /browse/video/ path → tidal_video
    4. tidal.com with /track/ or /browse/track/ path → tidal_track
    5. open.spotify.com/track/ path → spotify_track
    6. No URL scheme detected → text_search
    """
```

**URL Pattern Matching:**

| Pattern | Source Type | ID Extraction |
|---------|------------|---------------|
| `youtube.com/watch?v=X` | youtube_direct | None (full URL passed) |
| `youtu.be/X` | youtube_direct | None (full URL passed) |
| `music.youtube.com/watch?v=X` | youtube_music | video ID from `v` param |
| `tidal.com/browse/video/123` | tidal_video | None (full URL passed) |
| `tidal.com/video/123` | tidal_video | None (full URL passed) |
| `tidal.com/browse/track/123` | tidal_track | track ID from path |
| `tidal.com/track/123` | tidal_track | track ID from path |
| `open.spotify.com/track/X` | spotify_track | track ID from path |
| plain text (no `://`) | text_search | None |

### SpotifyMetadataExtractor

Async component that calls the Spotify Web API to get track metadata (artist, title) from a track ID.

```python
@dataclass(frozen=True)
class TrackMetadata:
    artist: str
    title: str
    isrc: str | None = None

class SpotifyMetadataExtractor:
    """Extract artist/title from Spotify track URLs via Spotify Web API."""
    
    async def extract(self, track_id: str) -> TrackMetadata:
        """Fetch track metadata from Spotify API.
        
        Uses client_credentials flow with cfg("spotify.client_id") 
        and cfg("spotify.client_secret").
        
        Raises:
            SpotifyMetadataError: On auth failure, track not found, or network error.
        """
```

Authentication uses the client credentials OAuth2 flow (no user login needed). Access tokens are cached with TTL based on the `expires_in` response field.

### MusicVideoResolver

The main orchestrator that wires classification, metadata extraction, and sub-resolvers together.

```python
class MusicVideoResolverError(Exception):
    """Raised when music video resolution fails."""
    def __init__(self, message: str, user_message: str | None = None):
        super().__init__(message)
        self.user_message = user_message or message

class MusicVideoResolver:
    """Resolve music video queries to VideoSource objects."""
    
    async def resolve(self, query: str) -> VideoSource:
        """Classify, resolve, and return a VideoSource for the given query.
        
        Resolution flow:
        1. Classify the query
        2. Dispatch to appropriate sub-resolver
        3. Apply fallback logic on failure
        4. Return VideoSource ready for ActivityStreamer
        
        Raises:
            MusicVideoResolverError: When all resolution paths fail.
        """
```

**Resolution dispatch per source type:**

| Source Type | Primary Path | Fallback |
|-------------|-------------|----------|
| youtube_direct | `YouTubeResolver.resolve(url)` | Error to user |
| youtube_music | `YouTubeResolver.resolve(youtube_url)` | Error to user |
| tidal_video | `TidalResolver.resolve_url(url)` | YouTube search (on recoverable error) |
| tidal_track | Tidal video search → `TidalResolver` | YouTube search "{artist} - {title} official music video" |
| spotify_track | `SpotifyMetadataExtractor` → YouTube search | Error to user (if Spotify API fails) |
| text_search | YouTube search "{query} official music video" | Error to user |

### Tidal Track-to-Video Lookup

When a Tidal track URL is provided, the resolver needs to determine if a native music video exists:

```python
async def _resolve_tidal_track(self, track_id: str) -> VideoSource:
    """Check if a Tidal track has a native video, falling back to YouTube search.
    
    Flow:
    1. Fetch track metadata from Tidal API (GET /v1/tracks/{id})
    2. Search Tidal videos for "{artist} - {title}" (GET /v1/search/videos?query=X&limit=1)
    3. If video found: resolve via TidalResolver
    4. If no video: YouTube search "{artist} - {title} official music video"
    """
```

The Tidal API does not have a direct track-to-video mapping. The lookup uses the track's artist + title as a search query against the Tidal video catalog. A match is accepted if the search returns a result with a reasonably similar title (fuzzy match not required — top result is trusted).

### Integration with PlaybackRouter / VideoCog

The `MusicVideoResolver` integrates at the command handler level, following the same pattern as `video_play`:

```python
# In the Music cog or Video cog
@app_commands.command(name="music_video")
async def play_music_video(self, interaction: discord.Interaction, query: str):
    # 1. Voice channel check (Req 8.4)
    # 2. Defer response
    # 3. MusicVideoResolver.resolve(query)
    # 4. Route to ActivityStreamer (launch or enqueue)
```

The command handler reuses the existing ActivityStreamer registry and launch pattern from `video_play` — it's the same pipeline, just with a different resolver front-end.

## Data Models

### MusicVideoSourceType (Enum)

```python
class MusicVideoSourceType(Enum):
    YOUTUBE_DIRECT = "youtube_direct"
    YOUTUBE_MUSIC = "youtube_music"
    TIDAL_VIDEO = "tidal_video"
    TIDAL_TRACK = "tidal_track"
    SPOTIFY_TRACK = "spotify_track"
    TEXT_SEARCH = "text_search"
```

### MusicVideoClassification (Dataclass)

```python
@dataclass(frozen=True)
class MusicVideoClassification:
    source_type: MusicVideoSourceType
    original_query: str
    extracted_id: str | None = None
```

### TrackMetadata (Dataclass)

```python
@dataclass(frozen=True)
class TrackMetadata:
    artist: str
    title: str
    isrc: str | None = None
```

### VideoSource (existing)

The output of all resolution paths. Already defined in `bot/video/__init__.py`:

```python
@dataclass
class VideoSource:
    source_type: Literal["youtube", "upload", "url", "tidal"]
    file_path: str
    title: str
    duration_seconds: float
    metadata: dict = field(default_factory=dict)
    audio_url: str | None = None
    cleanup_on_finish: bool = False
```

No changes needed to VideoSource — the music video resolver produces standard VideoSource objects consumed by ActivityStreamer.



## Correctness Properties

*A property is a characteristic or behavior that should hold true across all valid executions of a system — essentially, a formal statement about what the system should do. Properties serve as the bridge between human-readable specifications and machine-verifiable correctness guarantees.*

### Property 1: URL classification correctness

*For any* URL belonging to a recognized provider (YouTube, YouTube Music, Tidal video, Tidal track, Spotify track), the classifier SHALL return the correct `MusicVideoSourceType` corresponding to that provider's domain and path pattern.

**Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5**

### Property 2: Non-URL input classification

*For any* input string that does not contain a URL scheme (`://`), the classifier SHALL return `MusicVideoSourceType.TEXT_SEARCH`.

**Validates: Requirements 1.6**

### Property 3: Classification totality and uniqueness

*For any* arbitrary input string (including empty strings, malformed URLs, and random bytes), the classifier SHALL return exactly one valid `MusicVideoSourceType` value without raising an exception.

**Validates: Requirements 1.7**

### Property 4: YouTube Music video ID round-trip

*For any* valid video ID string, constructing a `music.youtube.com/watch?v={id}` URL and then extracting the video ID from that URL SHALL yield the original video ID.

**Validates: Requirements 3.1, 3.2**

### Property 5: Tidal track ID round-trip

*For any* numeric track ID, constructing a Tidal track URL (`tidal.com/browse/track/{id}` or `tidal.com/track/{id}`) and extracting the track ID SHALL yield the original numeric ID.

**Validates: Requirements 5.1**

### Property 6: Metadata fallback search query format

*For any* artist name and track title obtained from track metadata (Tidal or Spotify), the fallback YouTube search query SHALL be exactly `"{artist} - {title} official music video"`.

**Validates: Requirements 5.4, 6.2**

### Property 7: Text search query format

*For any* non-empty text query classified as `text_search`, the YouTube search query passed to the resolver SHALL be exactly `"{query} official music video"`.

**Validates: Requirements 7.1**

## Error Handling

### Error Categories

| Error Source | Error Type | User Message | Recovery |
|---|---|---|---|
| Classifier | Invalid/empty query | "Please provide a URL or search query." | None — user must retry |
| YouTubeResolver | Video unavailable | "YouTube video is unavailable (removed/private)." | None |
| YouTubeResolver | Network timeout | "YouTube is temporarily unavailable. Try again shortly." | None |
| TidalResolver | Non-recoverable | "Tidal video is unavailable." | None |
| TidalResolver | Recoverable | (silent) | Fallback to YouTube search |
| SpotifyMetadataExtractor | Auth/API failure | "Could not retrieve track info from Spotify." | None |
| YouTube search | No results | "No music video found for that query." | None |
| ActivityStreamer | Launch failure | "Failed to launch video Activity." | Cleanup VideoSource |
| Any resolver | Unexpected exception | "An unexpected error occurred." | Log full traceback |

### Error Flow

```python
try:
    source = await resolver.resolve(query)
except MusicVideoResolverError as exc:
    await interaction.followup.send(f"❌ {exc.user_message}", ephemeral=True)
    return
except Exception as exc:
    log.error("Unexpected error in /play music_video: %s", exc, exc_info=True)
    await interaction.followup.send(
        "❌ An unexpected error occurred while resolving the music video.",
        ephemeral=True,
    )
    return
```

### Timeout Strategy

- Spotify API calls: 10s timeout
- Tidal API calls: 15s timeout (matches existing `_API_REQUEST_TIMEOUT`)
- YouTube resolution (yt-dlp): 600s timeout (matches existing `_YTDLP_DOWNLOAD_TIMEOUT_SECONDS`)
- Overall command response: Discord interaction token expires at 15 minutes; all paths complete well before this

### Cleanup on Failure

When a VideoSource is resolved but Activity launch fails, the resolved file must be cleaned up if `cleanup_on_finish=True`. The command handler calls `os.unlink(source.file_path)` in the error path, matching the existing pattern in `video_play`.

## Testing Strategy

### Property-Based Tests (Hypothesis)

Property-based tests cover the pure logic layer — classification and query construction. These are fast, in-memory, and suitable for 100+ iterations.

**Library:** `hypothesis` (already in project — see `.hypothesis/` directory)

**Configuration:**
- Minimum 100 examples per property (Hypothesis default is 100)
- Each test tagged with property reference comment

**Tests:**

| Property | Test Description | Generator Strategy |
|---|---|---|
| 1 | URL classification | Generate URLs with provider-specific domains + random paths/IDs |
| 2 | Non-URL classification | Generate strings without `://` (text, `st.text()`) |
| 3 | Classification totality | `st.text()` — arbitrary strings including edge cases |
| 4 | YouTube Music ID round-trip | Generate valid video ID strings (alphanumeric + `-_`, 11 chars) |
| 5 | Tidal track ID round-trip | `st.integers(min_value=1, max_value=10**10)` |
| 6 | Metadata fallback query | Generate `(artist: st.text(min_size=1), title: st.text(min_size=1))` pairs |
| 7 | Text search query format | `st.text(min_size=1, alphabet=st.characters(blacklist_categories=('Cs',)))` filtered to exclude `://` |

### Unit Tests (pytest)

Example-based tests cover integration wiring, error paths, and mock-dependent behavior:

- YouTube direct resolution dispatches to YouTubeResolver
- YouTube Music URL with no video ID returns error
- Tidal recoverable error triggers YouTube fallback
- Tidal track with native video resolves via TidalResolver
- Spotify API failure produces correct user error message
- Command without voice channel returns error before resolution
- Active session enqueues and reports position
- Activity launch failure triggers file cleanup

### File Layout

```
tests/
  test_music_video_resolver.py      # Property + unit tests for resolver logic
  test_music_video_classifier.py    # Property tests for classification (pure)
```
