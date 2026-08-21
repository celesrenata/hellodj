"""Ollama HTTP client for intent extraction via gemma4."""

import asyncio
import json
import logging
from typing import Any

import aiohttp

from config import cfg

log = logging.getLogger(__name__)

# Defaults matching Requirement 9
_DEFAULT_URL = "http://localhost:11434"
_DEFAULT_MODEL = "gemma4"
_REQUEST_TIMEOUT = 10.0  # seconds (Requirement 3.5)


class OllamaClient:
    """Async HTTP client for the Ollama /api/chat endpoint."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    @property
    def url(self) -> str:
        """Read endpoint from credential store on each call (Req 9.1)."""
        return cfg("ollama.url") or _DEFAULT_URL

    @property
    def model(self) -> str:
        """Read model name from credential store on each call (Req 9.2)."""
        return cfg("ollama.model") or _DEFAULT_MODEL

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def chat(
        self,
        system_prompt: str,
        user_message: str,
    ) -> dict[str, Any] | None:
        """Send a chat request to Ollama.

        Returns the parsed JSON response dict, or None on failure.
        Raises asyncio.TimeoutError if the 10s deadline is exceeded.
        """
        session = await self._ensure_session()
        endpoint = f"{self.url.rstrip('/')}/api/chat"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "stream": False,
            "format": "json",  # request JSON mode from Ollama
        }

        timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT)
        async with session.post(endpoint, json=payload, timeout=timeout) as resp:
            if resp.status != 200:
                log.warning(
                    "Ollama returned HTTP %d from %s", resp.status, endpoint
                )
                return None
            data = await resp.json()
            return data

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
