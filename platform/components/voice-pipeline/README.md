# voice-pipeline

The `voice-pipeline` component of the HelloDJ AWS platform.

## Responsibility

- **Local wake word detection** (ONNX, tiny CPU model) — the **only on-box AI**.
- **Speech-to-text** via **Amazon Transcribe** (streaming) / Bedrock.
- **Intent recognition** via **Amazon Bedrock** (`InvokeModel`).
- **Text-to-speech** via **Amazon Polly**.
- Consumes Discord voice (**opus**) via `discord-bot-core` — this component
  never touches discord.py; bot-core hands it decoded PCM per speaker.
- Dispatches recognized actions to the `playback-orchestrator` over a typed
  HTTP/JSON client.

All managed-AI access (Bedrock, Transcribe, Polly) uses the pod's **IAM task
role** via the boto3 default credential chain — there are **no static keys**.

### Explicitly removed (per design)

The legacy self-hosted AI is **not** present in this component:

- Kokoro TTS
- faster-whisper / CTranslate2 STT
- self-hosted LLM
- Speaches

Moving STT/intent/TTS to managed AWS AI deletes the heavy ARM64 build
dependencies (PyTorch, CTranslate2, self-hosted model runtimes) from the fleet
and the compatibility gate. The **wake word ONNX runtime is the only remaining
ARM64 dependency** to verify in the gate.

_Requirements: 4.5, 6.3, 15.1, 18.4_

## Package layout

```
voice_pipeline/
├── __init__.py             # package version + docstring
├── config.py               # environment-driven runtime settings (no secrets)
├── aws_clients.py          # boto3 client factory (IAM task role; injectable)
├── wakeword.py             # local ONNX wake word detection (lazy onnxruntime)
├── stt.py                  # speech-to-text via Amazon Transcribe / Bedrock
├── intent.py               # intent recognition via Amazon Bedrock InvokeModel
├── tts.py                  # text-to-speech via Amazon Polly
├── orchestrator_client.py  # typed HTTP/JSON action-dispatch client
├── pipeline.py             # wakeword -> STT -> intent -> action -> TTS
└── main.py                 # entry point / construction seam
```

## Interfaces

- **discord-bot-core** — receives decoded opus/PCM voice frames per speaker.
- **Amazon Bedrock / Transcribe / Polly** — over the AWS SDK using an IAM task
  role (no static keys); clients are injectable for tests.
- **playback-orchestrator** — internal HTTP/JSON (typed client in
  `voice_pipeline.orchestrator_client`).

This is an independently deployable, independently versioned component
(Requirements 15.1). It is its own Nix-built image with its own semantic
version and its own CI/CD path.

## Development

```bash
# From the platform root:
uvx ruff@0.6.9 check components/voice-pipeline
python3 tools/check_line_count.py components/voice-pipeline

# onnxruntime / boto3 / numpy may not be installed in every environment; the
# modules import those lazily so syntax can be verified without the runtime
# deps:
python3 -m py_compile components/voice-pipeline/voice_pipeline/*.py
```
