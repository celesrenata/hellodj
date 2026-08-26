"""HelloDJ voice-pipeline component.

Local wake word detection (ONNX, tiny CPU model) is the only on-box AI in this
component. Speech-to-text, intent/LLM reasoning, and text-to-speech are
delegated to managed AWS AI services — Amazon Bedrock (with Amazon
Transcribe/Polly where they fit the flow better) — reached over the AWS SDK
using the pod's IAM task role (no static keys).

All self-hosted AI from the legacy platform (Kokoro TTS, faster-whisper /
CTranslate2 STT, self-hosted LLM / Speaches) is intentionally absent.

The pipeline consumes Discord voice (opus) via the discord-bot-core component
and dispatches recognized actions to the playback-orchestrator over a typed
HTTP/JSON client.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
