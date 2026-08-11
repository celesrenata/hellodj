# Hello DJ — Voice Activation Architecture

## Overview

This document defines the architecture for adding **full voice interaction** to the Hello DJ Discord music bot. Users say **"Hello DJ"** as a wake word, then speak a command — the bot detects the wake word, transcribes the command, classifies intent, executes the action, and speaks a response back through the voice channel.

---

## Wake Word Model Tuning Guide

Before diving into architecture, here is analysis of the trained wake word model parameters and how to tune if results are not ideal.

### Current Training Config (from `my_custom_model.yml`)

| Parameter | Value | Impact |
|-----------|-------|--------|
| `model_type` | dnn | Fully-connected DNN (not CNN/RNN) — fast inference |
| `layer_size` | 512 | Large hidden layer — high capacity, higher FP risk |
| `n_blocks` | 6 | 6 stacked blocks — deep model, more accurate but slower |
| `n_samples` | 200k | Plenty of positive samples |
| `steps` | 300k | Well-trained |
| `augmentation_rounds` | 8 | Heavy RIR+noise augmentation |
| `target_false_positives_per_hour` | 0.1 | Very strict — ~1 FP per 10 hours |
| `max_negative_weight` | 1000 | Strong negative class weighting |
| `batch_n_per_class` positive | 200 | Good positive batch diversity |
| `batch_n_per_class` adversarial_negative | 200 | Good adversarial coverage |

### Self-Audio Isolation — Not a Problem

Discord's voice protocol **naturally separates input and output audio**. The bot's own music playback (via Lavalink/wavelink) is **never reflected back** to the voice receiver:

- `voice_client.receive()` only delivers Opus frames from **other users' SSRCs** — the bot's own outgoing audio is not echoed
- Lavalink sends audio frames through a separate pipeline (the voice websocket send path), which the receive callback never sees
- The wake word model only processes audio from the receiver, so it will **never hear the bot's own music**

This eliminates the self-triggering risk entirely. No mitigation needed.

### Tuning Knobs

| Symptom | Adjust | Suggested Values |
|---------|--------|-----------------|
| **Too many FPs** (triggers on random speech/music) | Increase `max_negative_weight` | Try 1500–2000 |
| **Too many FPs** | Decrease `target_false_positives_per_hour` | Try 0.05 |
| **Too many FPs** | Add music/noise as `custom_negative_phrases` | Path to music WAV files |
| **Too many FPs** | Increase inference threshold at runtime | 0.5 → 0.7–0.9 |
| **Misses wake word** (low recall) | Increase `layer_size` | Try 1024 |
| **Misses wake word** | Increase `n_blocks` | Try 8–10 |
| **Misses wake word** | Increase positive `batch_n_per_class` | Try 400 |
| **Too slow** (can't run every 80ms) | Decrease `layer_size` | Try 256 |
| **Too slow** | Decrease `n_blocks` | Try 3–4 |
| **Too slow** | Switch to TFLite runtime | TFLite on CPU is faster than ONNX |

### Recommended "Ideal" Baseline for Discord

For a balanced Discord voice activation model:

```yaml
# Balanced config
layer_size: 512          # Good capacity
n_blocks: 6              # Good depth
max_negative_weight: 1500  # Slightly stronger FP penalty
target_false_positives_per_hour: 0.05  # Stricter
custom_negative_phrases: ["/path/to/music_clips"]  # Add music as negatives
```

If the model still triggers on music, the **self-audio gate** (runtime threshold adjustment) is the most practical fix — no retraining needed.

---

## 1. New Files & Modules

The voice system lives in a new `bot/voice/` package with a single cog wrapper.

```
bot/
├── voice/
│   ├── __init__.py
│   ├── audio_pipeline.py    # Opus→PCM capture, per-user ring buffers, mel extraction
│   ├── wakeword.py          # ONNX wake word inference wrapper
│   ├── stt.py               # faster-whisper transcription
│   ├── intent.py            # Intent classification (keyword + LLM fallback)
│   ├── tts.py               # TTS engine (kokoro / speaches)
│   ├── query_handler.py     # General query → LLM with MCP tool calls
│   └── voice_commands.py    # Orchestrator: wake→STT→intent→action→TTS
├── cogs/
│   └── voice.py             # Discord cog: /voice toggle, event wiring
```

### Module responsibilities

| Module | Responsibility |
|-------|---------------|
| `audio_pipeline.py` | Capture Opus frames from each voice channel, decode to PCM, maintain per-SSRC ring buffers, compute mel-spectrogram features for wake word |
| `wakeword.py` | Load `Hello_DJ.onnx`, run inference on mel features, emit detection events |
| `stt.py` | On wake word, capture the speaker's audio until silence, run faster-whisper, return transcript |
| `intent.py` | Classify transcript: music command, admin command, or general query |
| `tts.py` | Generate PCM audio from text response using kokoro/speaches, play through voice client |
| `query_handler.py` | Route general queries to LLM with MCP tools (weather, news, stocks, astronomy) |
| `voice_commands.py` | State machine orchestrating the full voice interaction cycle |
| `cogs/voice.py` | Slash command to enable/disable voice per guild, wire listeners |

---

## 2. Data Flow

```
┌──────────────────────────────────────────────────────────────────────┐
│                    VOICE INTERACTION PIPELINE                         │
│                                                                      │
│  Voice Channel                                                      │
│  ┌─────────┐     Opus frames      ┌────────────┐                    │
│  │  User A  │ ──────────────────→  │  Audio     │                    │
│  │  User B  │ ──────────────────→  │  Pipeline  │                    │
│  └─────────┘                       └────┬───────┘                    │
│        │                                 │                           │
│        │                                 │ PCM chunks               │
│        │                                 ▼                           │
│        │                       ┌─────────────────┐                  │
│        │                       │   Per-SSRC       │                  │
│        │                       │   Ring Buffer    │                  │
│        │                       │  (1.28s / 16 ts) │                  │
│        │                       └────┬────────────┘                  │
│        │                             │                               │
│        │                             │ mel features (16×96)          │
│        │                             ▼                               │
│        │                       ┌─────────────────┐  ≥0.5            │
│        │                       │   Wake Word      │ ─────→ START    │
│        │                       │   ONNX Model     │                 │
│        │                       └─────────────────┘                  │
│        │                                                             │
│        │  ┌─── WAKE WORD DETECTED (User A) ──────────────────┐      │
│        │  │                                                    │      │
│        │  │  1. Capture User A's audio from wake word point    │      │
│        │  │  2. Wait for silence (500ms threshold)             │      │
│        │  │  3. Run faster-whisper on captured audio           │      │
│        │  │  4. Classify intent                                │      │
│        │  │                                                    │      │
│        │  │  ┌── Music ───────────┐                            │      │
│        │  │  │ Parse song query   │                            │      │
│        │  │  │ Queue via player   │                            │      │
│        │  │  └────────────────────┘                            │      │
│        │  │                                                    │      │
│        │  │  ┌── Admin ───────────┐                            │      │
│        │  │  │ Check permissions  │                            │      │
│        │  │  │ Confirmation flow  │                            │      │
│        │  │  │ Execute action     │                            │      │
│        │  │  └────────────────────┘                            │      │
│        │  │                                                    │      │
│        │  │  ┌── General Query ───┐                            │      │
│        │  │  │ LLM + MCP tools    │                            │      │
│        │  │  │ Format response    │                            │      │
│        │  │  └────────────────────┘                            │      │
│        │  │                                                    │      │
│        │  │  5. Generate TTS response text                     │      │
│        │  │  6. TTS → PCM audio                                │      │
│        │  │  7. Send PCM frames to voice channel               │      │
│        │  └────────────────────────────────────────────────────┘      │
│                                                                      │
│  Voice Channel                                                       │
│  ┌─────────┐     TTS PCM audio      ┌────────────┐                  │
│  │  All     │ ←────────────────────  │  TTS       │                  │
│  │  Users   │                        │  Engine    │                  │
│  └─────────┘                        └────────────┘                  │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. Audio Pipeline Design

### 3.1 Capture Strategy

Discord voice uses Opus 48000Hz frames (20ms per frame). The pipeline:

1. **Access voice client**: After wavelink connects, retrieve `guild.voice_client` from discord.py
2. **Start receiver**: Call `voice_client.receive()` to start receiving Opus frames
3. **Decode Opus**: Each 20ms frame → PCM 16-bit signed, 48000Hz
4. **Downsample**: 48000Hz → 16000Hz (mono) — wake word model expects 16kHz
5. **Per-user buffering**: Maintain a dict `{ssrc: deque}` with 1.28s ring buffers (64 × 20ms frames = 1280 samples at 16kHz)
6. **Mel extraction**: Compute 96 mel bins over each 80ms window (1280 samples at 16kHz), producing 16 × 96 features for the 1.28s context

### 3.2 SSRC→User Mapping

Discord's voice protocol identifies each speaker by SSRC (Synchronization Source). We map SSRC → user ID:

- On `voice_client.receive()`, each frame includes an SSRC
- When a user starts speaking, Discord sends a `Speaking` event with SSRC
- We cache `{ssrc: user_id}` by correlating `on_voice_receive` data with `on_voice_state_update`

### 3.3 Wake Word Inference

```python
# Every 80ms (or every 4 Opus frames), run inference:
for ssrc, buffer in user_buffers.items():
    if len(buffer) >= 16:  # 16 × 80ms windows = 1.28s
        mel = compute_mel(buffer)  # shape [1, 16, 96]
        prob = wakeword_model.predict(mel)  # float32[1,1]
        if prob >= 0.5:
            on_wake_detected(ssrc, user_id, buffer)
            # Reset this user's buffer to avoid re-detection
```

### 3.4 Wake Word Model Interface

```python
class WakeWordModel:
    def __init__(self, model_path: str):
        self.session = onnxruntime.InferenceSession(model_path)
        self.input_name = self.session.get_inputs()[0].name
        self.output_name = self.session.get_outputs()[0].name

    def predict(self, mel: np.ndarray) -> float:
        # mel shape: (1, 16, 96)
        result = self.session.run([self.output_name], {self.input_name: mel})
        return float(result[0][0][0])  # sigmoid probability
```

### 3.5 STT Capture

On wake word detection:
1. Note the wake word timestamp
2. Continue capturing audio from the same SSRC
3. After 500ms of silence from that SSRC, finalize the clip
4. Run faster-whisper on the captured PCM (16kHz mono)
5. Return transcript string

**Silence detection**: RMS energy threshold — if below threshold for 5 consecutive frames (100ms), consider silence.

---

## 4. Intent Classification

### 4.1 Strategy: Keyword Router + LLM Fallback

A lightweight two-tier classifier:

**Tier 1 — Keyword Router** (runs first, O(1), no model required):

| Category | Keywords / Patterns |
|----------|---------------------|
| **Music** | `play`, `skip`, `pause`, `resume`, `stop`, `queue`, `shuffle`, `repeat`, `next`, `add`, `remove`, `join`, `leave`, `volume`, `lyrics`, plus song title extraction |
| **Admin** | `mute`, `kick`, `ban`, `timeout`, `ticket`, `revoke`, `restart`, `shutdown` |
| **General** | `weather`, `news`, `stocks`, `stock`, `astronomy`, `space`, `star`, `planet`, `time`, `date`, or anything not matching above |

**Tier 2 — LLM Router** (fallback when keyword router is ambiguous):
- If keywords are absent or ambiguous, send transcript to a small LLM (e.g., `llama-3.2-1b` or GPT-4o-mini) with a system prompt asking for intent classification
- LLM returns: `{"intent": "music|admin|general", "query": "..."}`

### 4.2 Music Command Parsing

For music intents, extract:
- **Play**: `"play [song name]"` or `"play [artist]"` → extract query, call `player.enqueue_and_start`
- **Skip/Next**: No args needed
- **Pause/Stop**: No args needed
- **Queue/List**: No args needed
- **Add**: `"add [song]"` → extract query, call `player.add_track`
- **Remove**: `"remove track [number]"` → parse index
- **Shuffle**: No args needed
- **Repeat**: `"repeat on"`, `"repeat off"`, `"repeat single"`, `"repeat queue"`
- **Join/Leave**: Channel management

### 4.3 Admin Command Parsing

Admin commands require:
1. **Permission check**: Same as text admin cog (`_is_admin`)
2. **User extraction**: Parse `"mute @user"`, `"kick @user"`, `"ban @user"` — extract Discord username/tag
3. **Confirmation flow**:
   - Bot says: `"You requested to kick @user. Please say 'confirm' to proceed."`
   - Wait for user's verbal confirmation (STT the reply)
   - If "confirm" or "yes" or "proceed" → execute
   - If "cancel" or "no" or "stop" → abort
   - Timeout after 30s → abort
4. **Ticket creation**: Parse reason, create ticket in admin channel, @mention the ticket handler role

---

## 5. LLM & MCP Integration

### 5.1 Model Choice

| Component | Model | Rationale |
|-----------|-------|-----------|
| **STT** | `faster-whisper base` | Good balance of accuracy vs speed (~1.5GB VRAM or CPU-friendly) |
| **Intent LLM** (optional) | `llama-3.2-1b` or `gpt-4o-mini` | Tiny enough to run locally or cheap API |
| **General Query LLM** | `gpt-4o-mini` (API) or local `qwen2.5-7b` | Needs tool-calling for MCPs |

### 5.2 MCP Tool Definitions

The general query LLM gets these tools:

```json
{
  "weather": {
    "description": "Get current weather and forecast for a location",
    "parameters": { "location": "string" }
  },
  "news": {
    "description": "Get latest news headlines",
    "parameters": { "topic": "string (optional)" }
  },
  "stocks": {
    "description": "Get stock price and market data",
    "parameters": { "symbol": "string" }
  },
  "astronomy": {
    "description": "Get astronomy data (ISS passes, moon phase, planets)",
    "parameters": { "query": "string" }
  }
}
```

### 5.3 API Structure

The `query_handler.py` module implements:
1. Tool definitions → LLM system prompt
2. LLM chat completion with function-calling
3. Tool execution (API calls to weather/news/stocks/astronomy APIs)
4. Response formatting (concise, voice-friendly — 1-2 sentences max)

### 5.4 API Keys

```
WEATHER_API_KEY=...     # weatherapi.com or open-meteo (free)
NEWS_API_KEY=...        # newsapi.org
STOCKS_API_KEY=...      # polygon.io or finnhub or yfinance (free)
```

---

## 6. Admin Voice Command System

### 6.1 Identity Verification

Verification is done via Discord's permission system — same as the text-based admin cog:

```python
def _is_admin(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    if member.guild_permissions.moderate_members:
        return True  # Allow mods for mute/kick/timeout
    return False
```

### 6.2 Confirmation Flow

For destructive actions (kick, ban, timeout, revoke):

```
User: "Hello DJ, kick @bad_user"
Bot:  [TTS] "You requested to kick @bad_user. Please say 'confirm' to proceed."
User: "confirm"
Bot:  [TTS] "Kicked @bad_user."  (or "Action cancelled." if not confirmed)
```

Implementation:
1. Parse the target user from transcript (username or tag)
2. Resolve `discord.Member` from guild (search by name, nickname, or mention)
3. Ask for confirmation via TTS
4. Set a timeout task for 30s
5. Capture next voice input from the same user
6. STT the confirmation audio
7. Check if transcript matches confirmation keywords
8. Execute or abort

### 6.3 Ticket Creation

```
User: "Hello DJ, create a ticket for @support please help with deployment"
Bot:  [TTS] "Ticket created for @support. Reason: please help with deployment."
       [Text] Creates a ticket channel or sends to ticket webhook
```

---

## 7. TTS Response System

### 7.1 Engine Choice

**Primary**: `kokoro` — fast, lightweight, multilingual TTS
**Fallback**: `speaches` — alternative if kokoro has issues

Both output raw PCM/WAV audio. We use PCM for streaming to Discord.

### 7.2 Playback Strategy

When the bot needs to speak:

1. **Pause music** (if playing) via `player_obj.pause()`
2. **Generate TTS** from response text → PCM audio (mono, 16kHz or 48kHz)
3. **Send PCM frames** to voice client using `voice_client.send_audio_frame()`:
   - Split PCM into 20ms frames (matching Opus frame size)
   - Encode each frame as Opus using `discord.opus.Encoder`
   - Send via `voice_client.send_audio_frame(opus_frame)`
4. **Wait for completion** (track duration)
5. **Resume music** (if paused)

### 7.3 TTS Response Format

Responses are kept short for voice:
- Music commands: `"Playing [song] by [artist]"` or `"Added [song] to queue"`
- Admin commands: `"Kicked @user"` or `"Muted @user for 10 minutes"`
- General queries: 1-2 sentence summary
- Errors: `"I couldn't find that song"` or `"You don't have permission for that"`

---

## 8. Dependencies (requirements.txt additions)

```txt
# Voice activation
faster-whisper>=1.0.0      # STT
onnxruntime>=1.18.0        # Wake word inference (CPU)
numpy>=1.26.0              # Audio processing
soundfile>=0.12.0          # WAV file I/O
pydub>=0.26.0              # Audio resampling / manipulation
kokoro>=1.0.0              # TTS engine
openai>=1.0.0              # LLM API calls
aiohttp>=3.9.0             # HTTP for MCP API calls (already listed)
librosa>=0.10.0            # Mel-spectrogram extraction
```

---

## 9. Docker Changes

### 9.1 bot/Dockerfile additions

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# System dependencies (FFmpeg + libsndfile for audio processing)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY bot.py player.py session.py storage.py blacklist.py ./
COPY cogs/ ./cogs/
COPY voice/ ./voice/

# Copy wake word model
COPY Hello_DJ.onnx /app/models/Hello_DJ.onnx

# Create data directory
RUN mkdir -p /app/data /app/models

CMD ["python", "bot.py"]
```

### 9.2 docker-compose.yml changes

Add volume mount for the wake word model and model cache:

```yaml
services:
  bot:
    # ... existing config ...
    volumes:
      - ./bot:/app
      - hellodj-data:/app/data
      - hellodj-models:/app/models   # Cache for STT/TTS models
    environment:
      # ... existing vars ...
      WAKE_WORD_MODEL_PATH: /app/models/Hello_DJ.onnx
      STT_MODEL_SIZE: base
      TTS_ENGINE: kokoro
      LLM_API_URL: ...
      LLM_API_KEY: ${LLM_API_KEY}

volumes:
  hellodj-models:    # New volume for model cache
```

### 9.3 Kubernetes changes (kube/deployment.yaml)

Add model cache volume and new env vars:

```yaml
# Additional env vars in the bot container
- name: WAKE_WORD_MODEL_PATH
  value: /app/models/Hello_DJ.onnx
- name: STT_MODEL_SIZE
  value: base
- name: TTS_ENGINE
  value: kokoro
- name: LLM_API_URL
  valueFrom:
    secretKeyRef:
      name: hellodj-secret
      key: LLM_API_URL
      optional: true
- name: LLM_API_KEY
  valueFrom:
    secretKeyRef:
      name: hellodj-secret
      key: LLM_API_KEY
      optional: true
# ... MCP API keys similarly ...

# Wake word model mount (ConfigMap or PV)
- name: wake-word-model
  configMap:
    name: wake-word-config  # Or mount from PV
    items:
      - key: Hello_DJ.onnx
        path: Hello_DJ.onnx

# Model cache volume
- name: hellodj-models
  persistentVolumeClaim:
    claimName: hellodj-models-pvc

volumeMounts:
  - mountPath: /app/models
    name: wake-word-model
    readOnly: true
  - mountPath: /app/model-cache
    name: hellodj-models
```

Resource estimates (add to bot container):
- CPU: +0.5 for wake word + STT inference
- Memory: +2Gi for model loading (faster-whisper base ~1.5GB, kokoro ~200MB)
- New total: CPU 2.5, Memory 3Gi

---

## 10. Configuration

### 10.1 Environment Variables (.env)

```ini
# Voice activation
WAKE_WORD_MODEL_PATH=/app/models/Hello_DJ.onnx
STT_MODEL_SIZE=base
TTS_ENGINE=kokoro
VOICE_ENABLED=true

# LLM
LLM_API_URL=https://api.openai.com/v1
LLM_API_KEY=sk-...

# MCP API keys (optional — general queries only)
WEATHER_API_KEY=...
NEWS_API_KEY=...
STOCKS_API_KEY=...
ASTRONOMY_API_KEY=...
```

### 10.2 Guild Config (stored in session data or separate config)

Per-guild voice settings, storable in a new `voice_config.json` or extended session:

```json
{
  "voice_enabled": true,
  "wake_word_only": false,       # If true, skip STT/intent — just wake word
  "admin_voice_enabled": true,
  "general_queries_enabled": true,
  "tts_enabled": true,
  "tts_voice": "default"
}
```

---

## 11. Implementation Order

| Phase | Files | Description |
|-------|-------|-------------|
| **1** | `voice/__init__.py`, `voice/wakeword.py` | Wake word ONNX wrapper + unit tests |
| **2** | `voice/audio_pipeline.py` | Opus capture, ring buffers, mel extraction |
| **3** | `voice/stt.py` | faster-whisper integration |
| **4** | `voice/intent.py` | Keyword router + optional LLM classifier |
| **5** | `voice/tts.py` | kokoro TTS wrapper + voice client PCM sender |
| **6** | `voice/voice_commands.py` | Orchestrator state machine |
| **7** | `cogs/voice.py` | Cog wiring: slash commands, event listeners |
| **8** | `voice/query_handler.py` | LLM + MCP tool integration |
| **9** | Docker/Kubernetes | Model volumes, resource updates, new env vars |
| **10** | Integration test | End-to-end: wake → STT → intent → action → TTS |

---

## 12. Design Rationale

### Why keyword router + LLM fallback for intent?

- Music commands are formulaic ("play X", "skip", "pause") — regex is fast, deterministic, and requires no GPU
- Admin commands are also formulaic ("kick @user", "mute @user")
- General queries are open-ended — needs LLM flexibility
- A keyword router handles ~80% of traffic with zero latency; LLM fallback handles ambiguous/novel queries

### Why per-SSRC ring buffers?

- Wake word detection must run continuously for all speakers
- Ring buffers are O(1) for append/pop, minimal memory (~1.28s × 16kHz × 2 bytes × 2 channels = ~82KB per user)
- We only need to run inference every 80ms (not every frame), keeping CPU usage low

### Why pause music for TTS?

- Mixing TTS over music requires audio mixing (volume ducking) which is complex
- Pausing is simple, reliable, and clear to users
- The music player already has pause/resume infrastructure

### Why "lazy" (take audio from wake word speaker)?

- Simplest approach: no beamforming, no multi-speaker separation
- After wake word, we only capture audio from the SSRC that triggered the wake word
- This avoids the complexity of speaker diarization
