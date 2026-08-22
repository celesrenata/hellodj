# Design Document: Synced Lyrics Overlay

## Overview

This feature adds a synchronized lyrics overlay to the HelloDJ Discord Activity viewport. Lyrics are fetched server-side from LRCLIB.net (priority) with Genius API fallback, timing is computed server-side (LRC timestamps or beat-estimated distribution), and the overlay renders client-side via CSS/JS with zero server GPU cost.

Two control methods:
1. **Per-viewer toggle** — Button in Activity controls, persisted to localStorage
2. **Broadcast override** — `/lyrics overlay:on|off` slash command forces state for all viewers

Resolution priority chain: LRCLIB synced → LRCLIB plain (beat-estimated) → Genius plain (beat-estimated) → "No lyrics available"

### Key Design Decisions

1. **LyricsService is per-guild, in-process** — Follows the VisualizerManager pattern. One instance per guild, managed within the bot's asyncio event loop. No separate container.

2. **Overlay is entirely client-side** — Server delivers timed data once; all rendering, scrolling, and karaoke highlighting happens in the browser.

3. **Reuses existing track-start callback** — `player.py` already fires `_on_track_start_callback` with metadata (title, artist, duration, artwork). LyricsService subscribes to the same hook via a chained callback pattern.

4. **Beat-estimated timing is optional enhancement** — Works without AudioFeatureBus (even distribution fallback). Beat snapping improves sync quality when the bus has subscribers.

5. **Cache is per-instance LRU** — No persistence across pod restarts. Lyrics are cheap to re-fetch and tracks repeat naturally within a session.

6. **Audio independence is absolute** — LyricsService failures never propagate to Lavalink playback. All fetches are fire-and-forget with exception swallowing at the boundary.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     Discord Client (Activity iframe)                          │
│                                                                              │
│  ┌──────────────────┐  ┌───────────────────┐  ┌─────────────────────────┐  │
│  │  hls.js Player   │  │  LyricsOverlay    │  │  Whiteboard Overlay     │  │
│  │  / Visualizer    │  │  (CSS/JS)         │  │  (z:10)                 │  │
│  │  (z:1)           │  │  (z:5)            │  │                         │  │
│  └────────┬─────────┘  └────────┬──────────┘  └────────┬────────────────┘  │
│           │                      │                       │                   │
│           └──────────────────────┼───────────────────────┘                   │
│                                  │                                           │
│                    ┌─────────────┴──────────────┐                            │
│                    │  Frontend State Machine     │                            │
│                    │  + LyricsOverlay class      │                            │
│                    │  + Lyrics toggle button     │                            │
│                    └─────────────┬──────────────┘                            │
│                                  │                                           │
│                    ┌─────────────┴──────────────┐                            │
│                    │  WebSocket Client           │                            │
│                    └─────────────┬──────────────┘                            │
└──────────────────────────────────┼───────────────────────────────────────────┘
                                   │ ws://.../activity/ws/{guild_id}
                                   │
┌──────────────────────────────────┼───────────────────────────────────────────┐
│  Bot Pod (hellodj)               │                                           │
│                                  │                                           │
│  ┌───────────────────────────────┴──────────────────────────────────────┐   │
│  │                    WebSocket Hub (ws_hub.py)                           │   │
│  │  [Extended: lyrics_data, lyrics_overlay_enable/disable,               │   │
│  │   lyrics_unavailable messages + late-joiner lyrics state]             │   │
│  └──────┬──────────────────┬──────────────────────────────┬─────────────┘   │
│         │                  │                               │                 │
│         ▼                  ▼                               ▼                 │
│  ┌─────────────┐  ┌───────────────────┐  ┌──────────────────────────────┐  │
│  │ Lyrics      │  │ player.py         │  │ cogs/lyrics.py               │  │
│  │ Service     │  │                   │  │ (slash command)              │  │
│  │ (per-guild) │  │ on_track_start    │  │                              │  │
│  │             │◀─│ callback fires ──▶│  │ /lyrics overlay:on|off       │  │
│  │ ┌─────────┐ │  │ metadata dict     │  │ → broadcast via ws_hub      │  │
│  │ │ LRCLIB  │ │  └───────────────────┘  └──────────────────────────────┘  │
│  │ │Provider │ │                                                            │
│  │ └────┬────┘ │                                                            │
│  │      │      │                                                            │
│  │ ┌────▼────┐ │                                                            │
│  │ │ Genius  │ │                                                            │
│  │ │Provider │ │                                                            │
│  │ └────┬────┘ │                                                            │
│  │      │      │                                                            │
│  │ ┌────▼─────────────┐                                                    │
│  │ │ BeatTimingEngine │                                                     │
│  │ │ (optional AFB    │                                                     │
│  │ │  subscriber)     │                                                     │
│  │ └──────────────────┘                                                     │
│  └─────────────┘                                                            │
│                                                                              │
│  ┌──────────────────────┐                                                   │
│  │ AudioFeatureBus      │                                                   │
│  │ (subscriber-gated)   │                                                   │
│  │ - beat timestamps    │                                                   │
│  │ - BPM estimate       │                                                   │
│  └──────────────────────┘                                                   │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

### Component Relationships

| Component | Role | Location |
|-----------|------|----------|
| LyricsService | Orchestrator: fetch, cache, timing, broadcast | `bot/video/lyrics_service.py` |
| LRCLIBProvider | LRCLIB.net API client + LRC parser | `bot/video/lrclib_provider.py` |
| GeniusProvider | Genius API plain text fetch (refactored from cog) | `bot/video/genius_provider.py` |
| BeatTimingEngine | Distributes lines to beats or evenly | `bot/video/beat_timing.py` |
| WebSocket Hub | Broadcasts lyrics payloads to Activity clients | `bot/video/ws_hub.py` (extended) |
| LyricsOverlay | Client-side rendering, scroll, karaoke | `activity_frontend/app.js` (extended) |
| `/lyrics` cog | Slash command with overlay parameter | `bot/cogs/lyrics.py` (extended) |

## Components and Interfaces

### Lyrics Source Resolution Strategy

Priority chain executed by `LyricsService.fetch_lyrics(artist, title, duration_s)`:

```
┌─────────────────────────────────────────────────────────────────┐
│ 1. LRCLIB.net: GET /api/get?artist_name&track_name&duration     │
│    ├── syncedLyrics field present? ──── YES → parse LRC → DONE  │
│    │                                           (lrc_synced or    │
│    │                                            lrc_word)        │
│    ├── plainLyrics field present? ───── YES → beat-estimated    │
│    │                                           timing → DONE     │
│    └── No result / timeout / error ─── fall through ↓           │
├─────────────────────────────────────────────────────────────────┤
│ 2. Genius API: search + scrape                                   │
│    ├── Plain text returned? ─────────── YES → beat-estimated    │
│    │                                           timing → DONE     │
│    └── No result / error ────────────── fall through ↓          │
├─────────────────────────────────────────────────────────────────┤
│ 3. No lyrics found                                               │
│    └── Broadcast lyrics_unavailable → client shows dismissible  │
│        "No lyrics available" (auto-dismiss 3s)                   │
└─────────────────────────────────────────────────────────────────┘
```

The entire chain is awaited with a combined timeout of 10 seconds (5s LRCLIB + 5s Genius). If a provider times out, execution moves to the next stage immediately.

### LRCLIB.net API Integration

### Endpoint

```
GET https://lrclib.net/api/get?artist_name={artist}&track_name={title}&duration={seconds}
```

### Request Configuration

| Parameter | Value |
|-----------|-------|
| Timeout | 5 seconds |
| User-Agent | `HelloDJ/1.0 (https://hellodj.celestium.life)` |
| Rate limiting | None required (LRCLIB is free, but respects User-Agent etiquette) |

### Response Handling

```python
# Successful response (200 OK):
{
    "id": 12345,
    "trackName": "Song Title",
    "artistName": "Artist",
    "albumName": "Album",
    "duration": 225.0,
    "instrumental": false,
    "plainLyrics": "Line one\nLine two\n...",
    "syncedLyrics": "[00:12.34]Line one\n[00:15.67]Line two\n..."
}

# No match: 404 Not Found → fall through to Genius
```

Decision tree:
1. `syncedLyrics` non-null → parse LRC format
2. `syncedLyrics` null, `plainLyrics` non-null → pass to BeatTimingEngine
3. `instrumental: true` → broadcast lyrics_unavailable (instrumental track)
4. 404 / timeout / error → fall through to GeniusProvider

### LRC Parsing

Standard LRC format: `[mm:ss.xx]Text`

```python
import re

_LRC_LINE_RE = re.compile(r"\[(\d{2}):(\d{2})\.(\d{2,3})\](.*)")
_LRC_WORD_RE = re.compile(r"<(\d{2}):(\d{2})\.(\d{2,3})>(\S+)")

def parse_lrc(lrc_text: str) -> list[TimedLine]:
    """Parse LRC string into timed lines.

    Handles both line-level and word-level timestamps:
    - Line-level: [00:12.34]Full line of lyrics
    - Word-level: [00:12.34]<00:12.34>word <00:12.80>word <00:13.10>word
    """
    lines = []
    for raw_line in lrc_text.strip().split("\n"):
        match = _LRC_LINE_RE.match(raw_line.strip())
        if not match:
            continue
        mm, ss, xx = int(match.group(1)), int(match.group(2)), int(match.group(3))
        # Handle both 2-digit (centiseconds) and 3-digit (milliseconds) formats
        ms = (mm * 60 + ss) * 1000 + (xx * 10 if len(match.group(3)) == 2 else xx)
        text = match.group(4).strip()

        # Check for word-level timestamps
        words = None
        word_matches = _LRC_WORD_RE.findall(text)
        if word_matches:
            words = []
            for w_mm, w_ss, w_xx, word_text in word_matches:
                w_ms = (int(w_mm) * 60 + int(w_ss)) * 1000 + (
                    int(w_xx) * 10 if len(w_xx) == 2 else int(w_xx)
                )
                words.append(TimedWord(time_ms=w_ms, text=word_text))
            # Clean text: remove word timestamps for display
            text = re.sub(r"<\d{2}:\d{2}\.\d{2,3}>", "", text).strip()

        lines.append(TimedLine(time_ms=ms, text=text, words=words))
    return lines
```

### Cache Strategy

- In-memory LRU dict, keyed by `f"{artist.lower().strip()}:{title.lower().strip()}"`
- Max 50 entries per guild (covers typical session rotation)
- Eviction: LRU on insert when at capacity
- No TTL (lyrics don't change; eviction handles memory)
- Cache miss triggers fetch; cache hit returns immediately

### Beat-Estimated Timing Algorithm

For plain text lyrics without timestamps (from LRCLIB plainLyrics or Genius):

```python
async def compute_beat_timing(
    plain_text: str,
    duration_s: float,
    audio_bus: AudioFeatureBus | None = None,
) -> list[TimedLine]:
    """Distribute plain text lines across song duration.

    Algorithm:
    1. Split into non-empty lines
    2. Weight each line by character count / total characters
    3. Compute cumulative start times: line_start = cumulative_weight * duration
    4. If AudioFeatureBus available with beat data:
       - Snap each line_start to nearest beat within ±500ms
    5. Return array of TimedLine (same format as LRC parsed)
    """
    # Step 1: Split and filter
    lines = [line.strip() for line in plain_text.split("\n") if line.strip()]
    if not lines:
        return []

    duration_ms = int(duration_s * 1000)

    # Step 2: Compute weights
    total_chars = sum(len(line) for line in lines)
    if total_chars == 0:
        # All lines are whitespace-only after strip — shouldn't happen but guard
        weights = [1.0 / len(lines)] * len(lines)
    else:
        weights = [len(line) / total_chars for line in lines]

    # Step 3: Cumulative start times
    cumulative = 0.0
    start_times = []
    for weight in weights:
        start_times.append(int(cumulative * duration_ms))
        cumulative += weight

    # Step 4: Beat snapping (optional)
    if audio_bus and audio_bus.subscriber_count > 0:
        beat_timestamps = await _get_beat_timestamps(audio_bus, duration_ms)
        if beat_timestamps:
            start_times = _snap_to_beats(start_times, beat_timestamps, tolerance_ms=500)

    # Step 5: Build TimedLine array
    return [
        TimedLine(time_ms=start_times[i], text=lines[i], words=None)
        for i in range(len(lines))
    ]


def _snap_to_beats(
    line_starts: list[int],
    beat_timestamps: list[int],
    tolerance_ms: int = 500,
) -> list[int]:
    """Snap each line start to the nearest beat within tolerance.

    Uses binary search for efficiency.
    """
    import bisect
    snapped = []
    for start in line_starts:
        idx = bisect.bisect_left(beat_timestamps, start)
        # Check nearest beat on either side
        candidates = []
        if idx < len(beat_timestamps):
            candidates.append(beat_timestamps[idx])
        if idx > 0:
            candidates.append(beat_timestamps[idx - 1])
        # Pick closest within tolerance
        best = start
        min_dist = tolerance_ms + 1
        for candidate in candidates:
            dist = abs(candidate - start)
            if dist < min_dist:
                min_dist = dist
                best = candidate
        snapped.append(best if min_dist <= tolerance_ms else start)
    return snapped
```

### AudioFeatureBus Integration

The BeatTimingEngine subscribes to `AudioFeatureBus` only when computing timing for a track with plain lyrics. It collects beat timestamps during the first few seconds of playback (or from cached beat data if the bus already has history). If the bus has zero subscribers or is unavailable, the algorithm uses even distribution without beat snapping — no degradation in functionality, just less musical alignment.

### WebSocket Protocol Extensions

### New Message Types

| Direction | Type | Trigger |
|-----------|------|---------|
| Server → Client | `lyrics_data` | Song starts + overlay enabled; late-joiner connect |
| Server → Client | `lyrics_overlay_enable` | `/lyrics overlay:on` command |
| Server → Client | `lyrics_overlay_disable` | `/lyrics overlay:off` command |
| Server → Client | `lyrics_unavailable` | No lyrics found for current track |

No client → server lyrics messages. The local toggle is purely frontend state; the broadcast override is via slash command → bot → ws_hub.

### `lyrics_data` Payload Schema

```json
{
    "type": "lyrics_data",
    "track_id": "artist:title",
    "sync_type": "lrc_synced",
    "duration_s": 225.0,
    "lines": [
        {
            "time_ms": 12340,
            "text": "First line of lyrics",
            "words": null
        },
        {
            "time_ms": 15670,
            "text": "Second line with karaoke",
            "words": [
                {"time_ms": 15670, "text": "Second"},
                {"time_ms": 15900, "text": "line"},
                {"time_ms": 16100, "text": "with"},
                {"time_ms": 16400, "text": "karaoke"}
            ]
        }
    ]
}
```

`sync_type` values:
- `"lrc_synced"` — Line-level LRC timestamps from LRCLIB
- `"lrc_word"` — Word-level LRC timestamps from LRCLIB
- `"beat_estimated"` — Computed timing from plain text

### `lyrics_overlay_enable` / `lyrics_overlay_disable`

```json
{"type": "lyrics_overlay_enable"}
{"type": "lyrics_overlay_disable"}
```

### `lyrics_unavailable`

```json
{
    "type": "lyrics_unavailable",
    "track_id": "artist:title",
    "reason": "not_found"
}
```

### Late-Joiner State Sync

When a client connects to a guild where lyrics overlay is enabled:

```python
# In ws_hub.py handle_ws(), after countdown/state handling:
lyrics_svc = self._lyrics_services.get(guild_id)
if lyrics_svc and lyrics_svc.enabled:
    if lyrics_svc.current_lyrics:
        await ws.send_json({
            "type": "lyrics_data",
            **lyrics_svc.current_lyrics.to_dict(),
        })
    # Client uses current playback position (from state message) to
    # determine which lyric line is active — no separate position sync needed
```

### Frontend LyricsOverlay Component

```javascript
class LyricsOverlay {
    /**
     * Client-side lyrics renderer.
     *
     * Responsibilities:
     * - Render 3-line display (prev, current, next)
     * - Animate line transitions on timeupdate
     * - Apply karaoke word-level highlight when word data available
     * - Handle enable/disable from local toggle and broadcast override
     */

    constructor(container) {
        this.container = container;
        this.el = null;           // .lyrics-overlay DOM element
        this.lines = [];          // [{time_ms, text, words}]
        this.syncType = null;     // 'lrc_synced' | 'lrc_word' | 'beat_estimated'
        this.currentIndex = -1;
        this.enabled = false;
        this.forcedState = null;  // null | true | false (broadcast override)
        this._build();
    }

    _build() {
        this.el = document.createElement('div');
        this.el.className = 'lyrics-overlay';
        this.el.innerHTML = `
            <div class="lyrics-line prev"></div>
            <div class="lyrics-line current"></div>
            <div class="lyrics-line next"></div>
        `;
        this.container.appendChild(this.el);
        this.el.style.display = 'none';
    }

    enable() {
        this.enabled = true;
        if (this.lines.length > 0) {
            this.el.style.display = '';
        }
        localStorage.setItem('hellodj_lyrics_enabled', 'true');
    }

    disable() {
        this.enabled = false;
        this.el.style.display = 'none';
        localStorage.setItem('hellodj_lyrics_enabled', 'false');
    }

    forceEnable() {
        this.forcedState = true;
        this.el.style.display = this.lines.length > 0 ? '' : 'none';
    }

    forceDisable() {
        this.forcedState = false;
        this.el.style.display = 'none';
    }

    clearForce() {
        this.forcedState = null;
        // Revert to local preference
        if (this.enabled && this.lines.length > 0) {
            this.el.style.display = '';
        } else {
            this.el.style.display = 'none';
        }
    }

    get isVisible() {
        if (this.forcedState !== null) return this.forcedState;
        return this.enabled;
    }

    setLyricsData(payload) {
        this.lines = payload.lines || [];
        this.syncType = payload.sync_type;
        this.currentIndex = -1;
        if (this.isVisible && this.lines.length > 0) {
            this.el.style.display = '';
        }
    }

    clearLyrics() {
        this.lines = [];
        this.syncType = null;
        this.currentIndex = -1;
        this._clearDisplay();
    }

    updatePosition(currentTimeMs) {
        if (!this.isVisible || this.lines.length === 0) return;

        // Binary search for current line
        const idx = this._findLineIndex(currentTimeMs);
        if (idx === this.currentIndex) return;

        this.currentIndex = idx;
        this._renderLines(idx);

        // Word-level karaoke
        if (this.syncType === 'lrc_word' && this.lines[idx]?.words) {
            this._renderKaraoke(this.lines[idx].words, currentTimeMs);
        }
    }

    _findLineIndex(timeMs) {
        // Find the last line whose time_ms <= timeMs
        let lo = 0, hi = this.lines.length - 1, result = -1;
        while (lo <= hi) {
            const mid = (lo + hi) >> 1;
            if (this.lines[mid].time_ms <= timeMs) {
                result = mid;
                lo = mid + 1;
            } else {
                hi = mid - 1;
            }
        }
        return result;
    }

    _renderLines(idx) {
        const prev = this.el.querySelector('.lyrics-line.prev');
        const curr = this.el.querySelector('.lyrics-line.current');
        const next = this.el.querySelector('.lyrics-line.next');

        prev.textContent = idx > 0 ? this.lines[idx - 1].text : '';
        curr.textContent = idx >= 0 ? this.lines[idx].text : '';
        next.textContent = idx < this.lines.length - 1 ? this.lines[idx + 1].text : '';

        // Trigger transition animation
        curr.classList.add('animate');
        requestAnimationFrame(() => curr.classList.remove('animate'));
    }

    _renderKaraoke(words, currentTimeMs) {
        const curr = this.el.querySelector('.lyrics-line.current');
        curr.innerHTML = words.map(w => {
            const active = currentTimeMs >= w.time_ms;
            return `<span class="lyrics-word ${active ? 'active' : ''}">${w.text}</span>`;
        }).join(' ');
    }

    _clearDisplay() {
        this.el.querySelectorAll('.lyrics-line').forEach(el => el.textContent = '');
        this.el.style.display = 'none';
    }

    showUnavailable() {
        if (!this.isVisible) return;
        const curr = this.el.querySelector('.lyrics-line.current');
        curr.textContent = 'No lyrics available';
        this.el.style.display = '';
        setTimeout(() => {
            if (this.lines.length === 0) {
                this.el.style.display = 'none';
                curr.textContent = '';
            }
        }, 3000);
    }
}
```

### CSS Structure

```css
.lyrics-overlay {
    position: absolute;
    bottom: 80px;
    left: 0;
    right: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    pointer-events: none;
    z-index: 5;  /* video(1) < lyrics(5) < whiteboard(10) < controls(20) */
    padding: 0 20px;
    font-family: 'Inter', sans-serif;
    text-shadow: 0 1px 4px rgba(0, 0, 0, 0.8);
}

.lyrics-line {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 90%;
    text-align: center;
    transition: opacity 0.3s ease, transform 0.3s ease;
    line-height: 1.4;
}

.lyrics-line.prev,
.lyrics-line.next {
    opacity: 0.4;
    font-size: 0.9em;
    color: #ccc;
}

.lyrics-line.current {
    opacity: 1.0;
    font-size: 1.3em;
    color: #fff;
    font-weight: 500;
}

.lyrics-line.current.animate {
    animation: lyrics-slide-up 0.3s ease-out;
}

@keyframes lyrics-slide-up {
    from { transform: translateY(8px); opacity: 0.7; }
    to   { transform: translateY(0); opacity: 1; }
}

.lyrics-word {
    transition: color 0.15s ease;
    color: rgba(255, 255, 255, 0.5);
}

.lyrics-word.active {
    color: #fff;
}

/* Responsive: single-line mode for narrow viewports */
@media (max-width: 400px) {
    .lyrics-overlay .lyrics-line.prev,
    .lyrics-overlay .lyrics-line.next {
        display: none;
    }
    .lyrics-overlay .lyrics-line.current {
        font-size: 1.0em;
    }
}
```

### Toggle Button

Added to the Activity controls bar (adjacent to existing whiteboard toggle):

```html
<button id="lyrics-toggle" class="control-btn" title="Toggle lyrics">
    <svg><!-- musical note icon --></svg>
</button>
```

```javascript
// In app.js initialization:
const lyricsToggle = document.getElementById('lyrics-toggle');
const savedPref = localStorage.getItem('hellodj_lyrics_enabled');
if (savedPref === 'true') lyricsOverlay.enable();

lyricsToggle.addEventListener('click', () => {
    if (lyricsOverlay.enabled) {
        lyricsOverlay.disable();
        lyricsToggle.classList.remove('active');
    } else {
        lyricsOverlay.enable();
        lyricsToggle.classList.add('active');
    }
});
```

### Playback Integration

The `updatePosition` method is called on the hls.js `timeupdate` event:

```javascript
video.addEventListener('timeupdate', () => {
    const currentMs = video.currentTime * 1000;
    lyricsOverlay.updatePosition(currentMs);
});
```

On seek: `timeupdate` fires naturally, which calls `updatePosition` with the new position. The binary search in `_findLineIndex` recalculates the correct line.

On pause: `timeupdate` stops firing, so lyrics freeze at the current line.

### /lyrics Command Extension

Extend `cogs/lyrics.py` with an optional `overlay` parameter:

```python
@app_commands.command(name="lyrics", description="Fetch lyrics for the current song")
@app_commands.describe(overlay="Toggle lyrics overlay for all Activity viewers")
async def lyrics(
    self,
    interaction: discord.Interaction,
    overlay: Literal["on", "off"] | None = None,
):
    state = player.get_state(interaction.guild.id)
    current = state.get("current")

    if overlay is not None:
        # Broadcast overlay control — requires a playing track for "on"
        if overlay == "on":
            if not current:
                await interaction.response.send_message(
                    "Nothing is playing right now.", ephemeral=True
                )
                return
            # Enable overlay + trigger lyrics fetch if needed
            lyrics_svc = get_lyrics_service(interaction.guild.id)
            lyrics_svc.enabled = True
            await lyrics_svc.fetch_and_broadcast()
            await ws_hub.broadcast_from_bot(
                interaction.guild.id, {"type": "lyrics_overlay_enable"}
            )
            await interaction.response.send_message(
                "🎤 Lyrics overlay enabled for all viewers.", ephemeral=True
            )
        else:  # overlay == "off"
            lyrics_svc = get_lyrics_service(interaction.guild.id)
            lyrics_svc.enabled = False
            await ws_hub.broadcast_from_bot(
                interaction.guild.id, {"type": "lyrics_overlay_disable"}
            )
            await interaction.response.send_message(
                "Lyrics overlay disabled.", ephemeral=True
            )
        return

    # Default behavior: embed lyrics in chat (unchanged)
    if not current:
        await interaction.response.send_message("Nothing is playing right now.")
        return
    # ... existing embed behavior ...
```

### GeniusProvider Refactoring

The existing `Lyrics._fetch_lyrics()` and `_extract_from_html()` methods are extracted into a shared module at `bot/video/genius_provider.py`. The cog calls the shared module; `LyricsService` also calls it for the fallback chain.

```python
# bot/video/genius_provider.py
async def fetch_genius_lyrics(title: str, artist: str, access_token: str) -> str | None:
    """Fetch plain text lyrics from Genius API.

    Returns plain text lyrics or None if not found.
    Extracted from cogs/lyrics.py for shared use by LyricsService.
    """
    ...
```

### LyricsService Lifecycle

```python
# bot/video/lyrics_service.py

class LyricsService:
    """Per-guild lyrics orchestrator.

    Manages:
    - Lyrics fetch resolution (LRCLIB → Genius → unavailable)
    - In-memory LRU cache (max 50 entries)
    - WebSocket broadcast of lyrics payloads
    - Overlay enabled/disabled state
    - Track change auto-fetch when overlay is enabled
    """

    def __init__(self, guild_id: int, ws_hub: WebSocketHub) -> None:
        self.guild_id = guild_id
        self.enabled: bool = False
        self.current_lyrics: TimedLyrics | None = None
        self.current_track_key: str = ""
        self._ws_hub = ws_hub
        self._cache: OrderedDict[str, TimedLyrics] = OrderedDict()
        self._cache_max = 50
        self._fetch_task: asyncio.Task | None = None
        self._lrclib = LRCLIBProvider()
        self._genius: GeniusProvider | None = None  # lazy init with token

    async def on_track_change(self, guild_id: int, metadata: dict) -> None:
        """Called via player.py track_start callback.

        If overlay is enabled, auto-fetches lyrics for the new track.
        If overlay is disabled, just update metadata for when it's next enabled.
        """
        artist = metadata.get("artist", "")
        title = metadata.get("title", "")
        duration_ms = metadata.get("duration_ms", 0)
        self.current_track_key = f"{artist.lower().strip()}:{title.lower().strip()}"

        if not self.enabled:
            return

        await self.fetch_and_broadcast(artist, title, duration_ms)

    async def fetch_and_broadcast(
        self,
        artist: str | None = None,
        title: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Fetch lyrics for current track and broadcast to clients.

        Uses cache if available. Non-blocking (spawns task if not cached).
        """
        # Use current track metadata if args not provided
        if artist is None or title is None:
            # Pull from player state
            state = player.get_state(self.guild_id)
            entry = state.get("current", {})
            artist = artist or entry.get("author", "")
            title = title or entry.get("title", "")
            duration_ms = duration_ms or entry.get("duration", 0)

        track_key = f"{artist.lower().strip()}:{title.lower().strip()}"
        duration_s = (duration_ms or 0) / 1000.0

        # Check cache
        if track_key in self._cache:
            self._cache.move_to_end(track_key)
            self.current_lyrics = self._cache[track_key]
            await self._broadcast_lyrics()
            return

        # Async fetch — non-blocking
        if self._fetch_task and not self._fetch_task.done():
            self._fetch_task.cancel()
        self._fetch_task = asyncio.create_task(
            self._do_fetch(artist, title, duration_s, track_key)
        )

    async def _do_fetch(
        self, artist: str, title: str, duration_s: float, track_key: str
    ) -> None:
        """Execute the resolution chain. Exceptions swallowed for audio safety."""
        try:
            # Guard: no timing without duration
            if duration_s <= 0 or duration_s > 86400:
                await self._broadcast_unavailable(track_key)
                return

            lyrics = await self._resolve(artist, title, duration_s)
            if lyrics:
                self._cache_put(track_key, lyrics)
                self.current_lyrics = lyrics
                await self._broadcast_lyrics()
            else:
                self.current_lyrics = None
                await self._broadcast_unavailable(track_key)
        except Exception:
            log.debug("LyricsService fetch failed for %s", track_key, exc_info=True)
            await self._broadcast_unavailable(track_key)

    async def _resolve(
        self, artist: str, title: str, duration_s: float
    ) -> TimedLyrics | None:
        """Resolution chain: LRCLIB → Genius → None."""
        # 1. LRCLIB
        result = await self._lrclib.fetch(artist, title, duration_s)
        if result:
            return result

        # 2. Genius (plain text → beat-estimated timing)
        if self._genius:
            plain_text = await self._genius.fetch(title, artist)
            if plain_text:
                lines = await compute_beat_timing(plain_text, duration_s)
                if lines:
                    return TimedLyrics(
                        track_id=f"{artist}:{title}",
                        sync_type="beat_estimated",
                        duration_s=duration_s,
                        lines=lines,
                    )

        # 3. Not found
        return None

    def _cache_put(self, key: str, lyrics: TimedLyrics) -> None:
        """Insert into LRU cache, evicting oldest if at capacity."""
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            if len(self._cache) >= self._cache_max:
                self._cache.popitem(last=False)
            self._cache[key] = lyrics

    async def _broadcast_lyrics(self) -> None:
        """Send lyrics_data to all connected clients."""
        if not self.current_lyrics:
            return
        await self._ws_hub.broadcast_from_bot(
            self.guild_id, self.current_lyrics.to_ws_message()
        )

    async def _broadcast_unavailable(self, track_key: str) -> None:
        """Send lyrics_unavailable to all connected clients."""
        await self._ws_hub.broadcast_from_bot(
            self.guild_id,
            {"type": "lyrics_unavailable", "track_id": track_key, "reason": "not_found"},
        )
```

### Registration Pattern

```python
# In bot.py or activity module setup:
_lyrics_services: dict[int, LyricsService] = {}

def get_lyrics_service(guild_id: int) -> LyricsService:
    if guild_id not in _lyrics_services:
        _lyrics_services[guild_id] = LyricsService(guild_id, ws_hub)
    return _lyrics_services[guild_id]

# Chain with existing track_start callback:
_original_callback = player._on_track_start_callback

async def _lyrics_track_start(guild_id: int, metadata: dict) -> None:
    # Forward to original callback (VisualizerManager) first
    if _original_callback:
        await _original_callback(guild_id, metadata)
    # Then handle lyrics
    svc = get_lyrics_service(guild_id)
    await svc.on_track_change(guild_id, metadata)

player.set_on_track_start_callback(_lyrics_track_start)
```

## Data Models

```python
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class TimedWord:
    """A single word with its start timestamp."""
    time_ms: int
    text: str

    def to_dict(self) -> dict:
        return {"time_ms": self.time_ms, "text": self.text}


@dataclass
class TimedLine:
    """A lyric line with timestamp and optional word-level data."""
    time_ms: int
    text: str
    words: list[TimedWord] | None = None

    def to_dict(self) -> dict:
        d = {"time_ms": self.time_ms, "text": self.text}
        d["words"] = [w.to_dict() for w in self.words] if self.words else None
        return d


@dataclass
class TimedLyrics:
    """Complete timed lyrics payload for a track."""
    track_id: str
    sync_type: Literal["lrc_synced", "lrc_word", "beat_estimated"]
    duration_s: float
    lines: list[TimedLine] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "track_id": self.track_id,
            "sync_type": self.sync_type,
            "duration_s": self.duration_s,
            "lines": [line.to_dict() for line in self.lines],
        }

    def to_ws_message(self) -> dict:
        """Format as a WebSocket message."""
        return {"type": "lyrics_data", **self.to_dict()}


@dataclass
class LyricsState:
    """Per-guild lyrics overlay state (tracked by LyricsService)."""
    enabled: bool = False
    current_lyrics: TimedLyrics | None = None
    current_track_key: str = ""
```

## File Structure

### New Files

| File | Purpose |
|------|---------|
| `bot/video/lyrics_service.py` | LyricsService class (orchestrator, cache, broadcast) |
| `bot/video/lrclib_provider.py` | LRCLIB.net API client + LRC parser |
| `bot/video/genius_provider.py` | Genius API shared module (extracted from cog) |
| `bot/video/beat_timing.py` | Beat-estimated timing algorithm |
| `bot/video/lyrics_models.py` | TimedWord, TimedLine, TimedLyrics, LyricsState dataclasses |

### Modified Files

| File | Changes |
|------|---------|
| `bot/cogs/lyrics.py` | Add `overlay` parameter, delegate `_fetch_lyrics` to `genius_provider.py` |
| `bot/video/ws_hub.py` | Late-joiner lyrics state sync, `_lyrics_services` reference |
| `bot/video/activity_frontend/app.js` | LyricsOverlay class, toggle button handler, WS message routing |
| `bot/video/activity_frontend/style.css` | `.lyrics-overlay`, `.lyrics-line`, `.lyrics-word` styles |
| `bot/video/activity_frontend/index.html` | Lyrics toggle button in controls bar |
| `bot/player.py` | No changes needed (callback already exists; chaining handled at registration) |

## Correctness Properties

### Property 1: LRC round-trip
For any valid LRC string, `format_lrc(parse_lrc(s))` produces a string that when parsed again yields an equivalent `list[TimedLine]` (timestamps match within 10ms due to format precision).

**Validates: Requirements 8.6**

### Property 2: Beat timing monotonicity
The `compute_beat_timing()` output always has strictly non-decreasing `time_ms` values: for all i, `lines[i].time_ms <= lines[i+1].time_ms`.

**Validates: Requirements 2.1**

### Property 3: Beat timing bounds
All computed `time_ms` values fall within `[0, duration_ms]`. No line starts before the song or after it ends.

**Validates: Requirements 2.1, 2.5**

### Property 4: Cache LRU invariant
The cache never exceeds `_cache_max` entries. After any `_cache_put`, `len(self._cache) <= self._cache_max`.

**Validates: Requirements 1.1**

### Property 5: Overlay z-order
The lyrics overlay z-index (5) is always between video/visualizer (1) and whiteboard (10). This is enforced by CSS constants, not runtime computation.

**Validates: Requirements 7.1**

### Property 6: Binary search correctness
`_findLineIndex(timeMs)` returns the largest index `i` where `lines[i].time_ms <= timeMs`, or -1 if no line has started yet.

**Validates: Requirements 6.6**

### Property 7: Audio independence
No exception raised within LyricsService propagates to the caller in `on_track_start`. The callback boundary in `player.py` swallows all exceptions.

**Validates: Requirements 9.5**

### Property 8: Force override precedence
When `forcedState` is non-null, it takes precedence over the local `enabled` toggle in determining visibility.

**Validates: Requirements 4.4, 4.5**

## Error Handling

| Failure Mode | Behavior | Impact |
|---|---|---|
| LRCLIB timeout (5s) | Log debug, fall through to Genius | Slightly slower resolution |
| LRCLIB HTTP error (4xx/5xx) | Log warning, fall through to Genius | No degradation |
| Genius fetch failure | Broadcast `lyrics_unavailable` | Client shows "No lyrics available" |
| LRC parse error (malformed) | Treat as plain text, use beat-estimated | May have imperfect timing |
| AudioFeatureBus unavailable | Use even distribution (no beat snapping) | Less musical, still functional |
| Track has no duration / duration > 24h | Skip timing, broadcast `lyrics_unavailable` | No overlay for live streams |
| WebSocket send failure | Log warning, skip that client | Silent degradation for one viewer |
| Unhandled exception in fetch chain | Catch-all: log, broadcast `lyrics_unavailable` | Never crashes audio |
| aiohttp ClientError (DNS, connection) | Treated same as timeout per provider | Falls through cleanly |

### Audio Independence Guarantee

The LyricsService:
- Never imports from or writes to Lavalink/wavelink state
- All interactions with player.py are read-only (get_state for metadata)
- The track_start callback is wrapped in try/except at the boundary in player.py
- A lyrics fetch task failure is caught and logged, never re-raised
- LyricsService runs in the same event loop but shares NO mutable state with playback

## Testing Strategy

### Phased Implementation

### Phase 1: Core Pipeline (MVP)
- `lrclib_provider.py` — LRCLIB.net API client + LRC line-level parser
- `lyrics_models.py` — Data model classes
- `beat_timing.py` — Even distribution timing (no beat snapping yet)
- `lyrics_service.py` — Orchestrator with LRCLIB-only resolution + cache
- `ws_hub.py` extension — `lyrics_data` broadcast, late-joiner sync
- Frontend `LyricsOverlay` class — 3-line display, `updatePosition`, CSS
- Frontend WS message routing for `lyrics_data`, `lyrics_unavailable`

### Phase 2: Full Resolution + Karaoke
- `genius_provider.py` — Extracted shared module
- `cogs/lyrics.py` update — Delegate to shared provider
- LRC word-level parsing in `lrclib_provider.py`
- Frontend `_renderKaraoke()` — Word-level progressive highlight
- `beat_timing.py` — AudioFeatureBus beat-snap integration

### Phase 3: Controls + Commands
- `/lyrics overlay:on|off` — Command extension
- `lyrics_overlay_enable`/`lyrics_overlay_disable` WS messages
- Frontend toggle button + localStorage persistence
- Frontend `forceEnable()`/`forceDisable()` handling
- Responsive single-line mode (< 400px viewport)

### Dependency Order

```
lyrics_models.py (no deps)
  → lrclib_provider.py (depends on models)
  → genius_provider.py (depends on aiohttp, config)
  → beat_timing.py (depends on models, optional AudioFeatureBus)
  → lyrics_service.py (depends on all above + ws_hub)
  → ws_hub.py extension (depends on lyrics_service)
  → cogs/lyrics.py update (depends on lyrics_service, genius_provider)
  → Frontend (depends on WS protocol being defined)
```
