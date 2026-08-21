"""LLM-powered intent extraction using Ollama gemma4.

Replaces the keyword-based classifier with structured JSON extraction.
Falls back to keyword classification on any failure.
"""

import asyncio
import json
import logging
import re
from typing import Any

from .ollama_client import OllamaClient
from .intent import classify_intent as keyword_classify
from .schema_validator import validate_command_objects, strip_json_fences

log = logging.getLogger(__name__)

# Recognized actions (Requirement 3.3, 10.4)
RECOGNIZED_ACTIONS = frozenset({
    "play", "skip", "pause", "resume", "stop", "shuffle",
    "remove", "repeat", "queue", "join", "leave",
    "load_playlist", "save_playlist",
    # Admin actions
    "mute", "kick", "ban", "timeout", "revoke", "restart", "shutdown",
})

ADMIN_ACTIONS = frozenset({
    "mute", "kick", "ban", "timeout", "revoke", "restart", "shutdown",
})

# Source mapping (Requirement 4.2)
SOURCE_MAP = {
    "spotify": "spotify",
    "tidal": "tidal",
    "youtube": "youtube",
    "youtube music": "youtube_music",
    "soundcloud": "soundcloud",
    "deezer": "deezer",
}

SYSTEM_PROMPT = '''You are a voice command parser for a Discord music bot called HelloDJ.
Extract commands from the user's spoken transcript and return ONLY valid JSON.

Return a JSON array of command objects. Each command object has:
- "action": one of: play, skip, pause, resume, stop, shuffle, remove, repeat, queue, join, leave, load_playlist, save_playlist, mute, kick, ban, timeout, revoke, restart, shutdown
- "source": the music source if specified (spotify, tidal, youtube, youtube_music, soundcloud, deezer), or null
- "query": the search query or target name (e.g., song title, playlist name, user name), or null
- "arguments": an object with additional args (e.g., {"index": 3}, {"mode": "single"}, {"name": "chill vibes"}), or {}

Rules:
- If the user says "on spotify" or "on tidal" etc., set source to the normalized name and do NOT include "on spotify" in the query.
- Source names map: "youtube music" → "youtube_music"
- Multiple commands in one utterance should produce multiple objects in order.
- For "remove", extract the track number as {"index": N} in arguments.
- For "repeat", extract mode as {"mode": "off"|"single"|"queue"} in arguments.
- For "load_playlist"/"save_playlist", put the playlist name in arguments as {"name": "..."}.
- For admin commands (mute/kick/ban/timeout), put the target user name in query.
- Return ONLY the JSON array, no explanation.'''


class LLMIntentExtractor:
    """Extracts intents from transcripts using Ollama gemma4."""

    def __init__(self):
        self._client = OllamaClient()

    async def extract(self, transcript: str) -> list[dict[str, Any]]:
        """Extract Command_Objects from a transcript.

        Returns a list of validated Command_Objects.
        Falls back to keyword classification on any failure.
        """
        # Requirement 3.7: empty transcript → empty array
        if not transcript or not transcript.strip():
            return []

        try:
            response = await asyncio.wait_for(
                self._client.chat(SYSTEM_PROMPT, transcript),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            log.warning("Ollama intent extraction timed out (10s)")
            return self._fallback(transcript)
        except Exception as exc:
            log.warning("Ollama request failed: %s", exc)
            return self._fallback(transcript)

        if response is None:
            return self._fallback(transcript)

        # Extract the message content from Ollama response
        content = response.get("message", {}).get("content", "")
        if not content:
            log.warning("Ollama returned empty content")
            return self._fallback(transcript)

        # Strip markdown fences (Requirement 11.3)
        content = strip_json_fences(content)

        # Parse JSON
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            log.warning("Ollama response is not valid JSON: %s", content[:200])
            return self._fallback(transcript)

        # Validate schema (Requirements 11.1, 11.2, 11.4)
        if not isinstance(parsed, list):
            log.warning("Ollama response is not a JSON array")
            return self._fallback(transcript)

        if not parsed:
            log.warning("Ollama returned empty array")
            return self._fallback(transcript)

        # Validate individual Command_Objects
        valid_commands = validate_command_objects(parsed)

        # Filter unrecognized actions (Requirement 3.8)
        filtered = []
        for cmd in valid_commands:
            if cmd["action"] in RECOGNIZED_ACTIONS:
                # Normalize source (Requirement 4.5)
                if cmd.get("source") and cmd["source"] not in SOURCE_MAP.values():
                    log.warning("Unrecognized source '%s', setting to null", cmd["source"])
                    cmd["source"] = None
                filtered.append(cmd)
            else:
                log.warning("Discarding unrecognized action: %s", cmd["action"])

        if not filtered:
            return self._fallback(transcript)

        # Enforce max 10 commands (Requirement 5.1)
        return filtered[:10]

    def _fallback(self, transcript: str) -> list[dict[str, Any]]:
        """Fall back to keyword-based classification."""
        log.info("Falling back to keyword classifier for: %s", transcript[:80])
        intent = keyword_classify(transcript)
        return self._intent_to_commands(intent)

    def _intent_to_commands(self, intent: dict) -> list[dict[str, Any]]:
        """Convert legacy intent dict to Command_Object format."""
        action = intent.get("subcommand") or intent.get("intent", "")
        if action == "music":
            action = intent.get("subcommand", "play")
        elif action == "general":
            return []  # General queries handled separately

        cmd = {
            "action": action,
            "source": None,
            "query": intent.get("args", {}).get("song") or intent.get("query"),
            "arguments": intent.get("args", {}),
        }
        return [cmd]

    async def close(self) -> None:
        await self._client.close()
