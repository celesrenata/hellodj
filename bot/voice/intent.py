"""Intent classification: keyword router + LLM fallback.

Classifies transcribed voice commands into three categories:
- music — song playback commands
- admin — moderation commands (mute, kick, ban, timeout, ticket)
- general — everything else (weather, news, stocks, astronomy, etc.)
"""

import logging
import os
import re

from debug import get_debug_logger

log = logging.getLogger(__name__)
dbg = get_debug_logger("intent")

# ── keyword patterns ──────────────────────────────────────────────────────

MUSIC_KEYWORDS = {
    "play", "skip", "pause", "resume", "stop", "queue", "shuffle",
    "repeat", "next", "add", "remove", "delete", "move", "join",
    "leave", "volume", "lyrics", "song", "music", "playlist",
    "continue", "start",
}

ADMIN_KEYWORDS = {
    "mute", "kick", "ban", "timeout", "ticket", "revoke",
    "restart", "shutdown", "kill",
}

# Phrases that explicitly indicate general queries
GENERAL_INDICATORS = {
    "weather", "temperature", "forecast",
    "news", "headlines",
    "stock", "stocks", "price", "market",
    "astronomy", "space", "planet", "star", "moon",
    "iss", "satellite",
    "time", "date", "today",
    "tell me", "what is", "what's", "how", "why",
    "search", "look up", "find",
}


def classify_intent(transcript: str) -> dict:
    """Classify a voice transcript into (intent, query).

    Returns a dict with keys:
        intent: "music" | "admin" | "general"
        query: str — the extracted command/query text
        subcommand: str | None — specific action (e.g. "play", "skip")
        args: dict — parsed arguments (e.g. {"song": "..."})
    """
    transcript = transcript.strip().lower()
    dbg.event("classify_start", transcript=transcript[:100])

    # ── 1. Admin check (most important — security-sensitive) ──────
    admin_match = _match_admin(transcript)
    if admin_match:
        return admin_match

    # ── 2. Music command check ───────────────────────────────────
    music_match = _match_music(transcript)
    if music_match:
        return music_match

    # ── 3. General query check ──────────────────────────────────
    if _is_general_query(transcript):
        return {
            "intent": "general",
            "query": transcript,
            "subcommand": None,
            "args": {},
        }

    # ── 4. Default: general query ────────────────────────────────
    return {
        "intent": "general",
        "query": transcript,
        "subcommand": None,
        "args": {},
    }


def _match_admin(transcript: str) -> dict | None:
    """Try to match an admin command.

    Patterns:
      - "mute @user [duration]"
      - "kick @user [reason]"
      - "ban @user [reason]"
      - "timeout @user [minutes]"
      - "ticket [reason]"
      - "revoke @user"
      - "restart" / "shutdown"
    """
    # Simple keyword detection — actual parsing happens in voice_commands.py
    words = set(transcript.split())
    admin_hits = words & ADMIN_KEYWORDS
    if not admin_hits:
        return None

    # Determine subcommand
    admin_words = list(admin_hits)
    subcommand = admin_words[0]  # e.g. "mute", "kick"

    return {
        "intent": "admin",
        "query": transcript,
        "subcommand": subcommand,
        "args": {"raw": transcript},
    }


def _match_music(transcript: str) -> dict | None:
    """Try to match a music command.

    Extracts the song query for "play" and "add" commands.
    """
    words = transcript.split()
    music_hits = [w for w in words if w in MUSIC_KEYWORDS]

    if not music_hits:
        return None

    # Determine primary subcommand (first music keyword)
    subcommand = music_hits[0]

    # Extract arguments
    args = {}
    if subcommand in ("play", "add"):
        # Everything after the keyword is the song query
        cmd_idx = transcript.index(subcommand)
        query = transcript[cmd_idx + len(subcommand):].strip()
        if query:
            args["song"] = query

    elif subcommand == "remove":
        # "remove track [number]" or "remove [number]"
        match = re.search(r"(?:track|item|song)?\s*(\d+)", transcript)
        if match:
            args["index"] = int(match.group(1))

    elif subcommand == "repeat":
        match = re.search(r"(on|off|single|queue|one|all)", transcript)
        if match:
            args["mode"] = match.group(1)

    return {
        "intent": "music",
        "query": transcript,
        "subcommand": subcommand,
        "args": args,
    }


def _is_general_query(transcript: str) -> bool:
    """Check if transcript looks like a general query."""
    words = transcript.split()
    if not words:
        return False

    # Check general indicators
    for indicator in GENERAL_INDICATORS:
        if indicator in transcript:
            return True

    # If first word is a question word, treat as general
    question_words = {"what", "how", "why", "when", "where", "who", "tell", "show"}
    if words[0].lower() in question_words:
        return True

    return False


def intent_to_string(intent: dict) -> str:
    """Human-readable intent summary (for logging)."""
    return (
        f"intent={intent['intent']}, "
        f"sub={intent.get('subcommand', '—')}, "
        f"query={intent['query'][:80]}"
    )
