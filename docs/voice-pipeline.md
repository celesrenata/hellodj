# Voice Activation Pipeline

## Overview

HelloDJ supports hands-free voice control via a custom "Hello DJ" wake word model. When the wake word is detected, the bot transcribes speech, extracts structured intents via LLM, executes commands, and responds via TTS.

## Pipeline Stages

```
Opus Frames (20ms each, from Discord voice)
  │
  ▼
PipelineSink → VoiceCommandOrchestrator.on_voice_receive()
  │
  ▼
AudioPipeline (decode Opus → PCM → mel-spectrogram buffer)
  │
  ▼
[80ms tick] WakeWordModel.predict(mel[1, 16, 96])
  │
  ├─ probability < 0.5 → discard, shift window
  │
  └─ probability ≥ 0.5 → WAKE WORD DETECTED
      │
      ▼
  Accumulate speech (2-10 seconds of post-wake audio)
      │
      ▼
  STT Engine (faster-whisper local / Speaches / AWS Transcribe)
      │
      ▼
  Transcript text: "play Bad Guy by Billie Eilish on Spotify"
      │
      ▼
  LLM Intent Extraction (Ollama gemma4, 10s timeout)
      │   Falls back to keyword classifier on failure
      ▼
  Command_Objects: [{"action": "play", "source": "spotify", "query": "Bad Guy by Billie Eilish"}]
      │
      ▼
  Command Execution (dispatch to player.py / admin / query)
      │
      ▼
  TTS Response (Speaches / Kokoro / AWS Polly) → voice channel
```

## Components

### Wake Word Model (`voice/wakeword.py`)

- Custom ONNX model trained on "Hello DJ" utterances
- Input: float32[1, 16, 96] — 16 time-steps × 96 mel frequency bins
- Output: float32[1, 1] — sigmoid probability (≥0.5 = detected)
- Runs on CPU (CPUExecutionProvider) every 80ms
- Model path: `/app/models/Hello_DJ.onnx` (mounted via NFS PVC)
- False positive rate tunable via threshold (default 0.5)

### Audio Pipeline (`voice/audio_pipeline.py`)

- Receives raw Opus frames from discord.ext.voice_recv
- Decodes to 16kHz mono PCM
- Computes mel-spectrogram (96 bins) in sliding window
- Maintains circular buffer of 16 time-steps (1.28s of audio)
- Feeds mel slices to wake word model

### STT Engines (`voice/stt.py`)

| Engine | Config Key | Notes |
|--------|-----------|-------|
| `local` | `stt.engine=local` | faster-whisper, runs on-device (GPU or CPU) |
| `speaches` | `stt.engine=speaches` | Remote Whisper via Speaches service |
| `bedrock` | `stt.engine=bedrock` | AWS Transcribe via Bedrock |

Model size controlled by `stt.model_size` (default: "base").

### LLM Intent Extraction (`voice/llm_intent.py`)

Uses Ollama with gemma4 model to extract structured JSON from transcripts:

```json
[
  {
    "action": "play",
    "source": "spotify",
    "query": "Bad Guy by Billie Eilish",
    "arguments": {}
  }
]
```

**Recognized Actions:** play, skip, pause, resume, stop, shuffle, remove, repeat, queue, join, leave, load_playlist, save_playlist, mute, kick, ban, timeout, revoke, restart, shutdown

**Admin Actions** (require elevated permissions): mute, kick, ban, timeout, revoke, restart, shutdown

**Fallback:** On LLM timeout (10s) or failure, falls back to keyword-based classifier (`voice/intent.py`).

### Query Handler (`voice/query_handler.py`)

Handles non-music queries via LLM with MCP-style tool calling:

| Tool | Source | API Key Required |
|------|--------|-----------------|
| `get_weather` | Open-Meteo (free) | No |
| `get_news` | newsapi.org | Yes (`news.api_key`) |
| `get_stock` | Finnhub | Yes (`stocks.api_key`) |
| `get_astronomy` | wheretheiss.at | No |

Fast path: Common queries (weather, news, stock, astronomy) are routed directly to tools without LLM overhead.

### TTS Engines (`voice/tts.py`)

| Engine | Config Key | Notes |
|--------|-----------|-------|
| `speaches` | `tts.engine=speaches` | Speaches service (kokoro voices) |
| `kokoro` | `tts.engine=kokoro` | Direct Kokoro endpoint |
| `polly` | `tts.engine=polly` | AWS Polly |

Default voice: `af_heart` (Speaches). Responses are synthesized and played back in the voice channel via the hybrid player.

### Voice Command Orchestrator (`voice/voice_commands.py`)

Coordinates the full pipeline:
1. Manages per-user audio state (prevent cross-talk)
2. Detects speech end (silence threshold)
3. Dispatches recognized intents to appropriate handlers
4. Handles TTS response playback
5. Metrics recording (wake word detections, STT/TTS usage)

## Configuration

| Key | Env Var | Default | Purpose |
|-----|---------|---------|---------|
| `voice.enabled` | `VOICE_ENABLED` | false | Master switch for auto-listen |
| `voice.wakeword_model` | `WAKE_WORD_MODEL_PATH` | /app/models/Hello_DJ.onnx | Model path |
| `stt.engine` | `STT_ENGINE` | local | STT backend selection |
| `stt.model_size` | `STT_MODEL_SIZE` | base | Whisper model size |
| `tts.engine` | `TTS_ENGINE` | speaches | TTS backend selection |
| `tts.voice` | `TTS_VOICE` | af_heart | TTS voice name |
| `tts.speaches_endpoint` | `TTS_SPEACHES_ENDPOINT` | (cluster URL) | Speaches service URL |
| `llm.api_url` | `LLM_API_URL` | https://api.openai.com/v1 | LLM endpoint |
| `llm.api_key` | `LLM_API_KEY` | — | LLM API key |
| `llm.model` | `LLM_MODEL` | gpt-4o-mini | LLM model name |

## Slash Commands

| Command | Description |
|---------|-------------|
| `/voice enable\|disable` | Toggle voice activation for guild |
| `/wakeword on\|off` | Toggle wake word listening (Manage Guild) |
| `/voice_status` | Show voice activation status |

## Requirements

- `discord-ext-voice-recv>=0.5.2a179` — Audio frame reception
- `onnxruntime>=1.18.0` — ONNX inference
- `faster-whisper>=1.0.0` — Local STT (optional)
- `kokoro>=0.9.4` — Local TTS (optional)
- `numpy>=1.26.0` — Array operations
- `soundfile>=0.12.0` — Audio file I/O
