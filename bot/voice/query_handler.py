"""General query handling: LLM with MCP-style tool integration.

Routes non-music, non-admin voice queries to an LLM with access to
weather, news, stocks, and astronomy tools.
"""

import json
import logging
import os

import aiohttp

log = logging.getLogger(__name__)

# ── MCP tool definitions (sent to LLM as system prompt) ───────────────────

MCP_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather and forecast for a location",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City name or coordinates",
                    }
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_news",
            "description": "Get latest news headlines, optionally filtered by topic",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "News topic or category (e.g. technology, sports, world)",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stock",
            "description": "Get stock price and market data for a ticker symbol",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Stock ticker symbol (e.g. AAPL, TSLA, MSFT)",
                    }
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_astronomy",
            "description": "Get astronomy data (ISS passes, moon phase, planet positions)",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Astronomy question (e.g. 'ISS passes tonight', 'moon phase')",
                    }
                },
                "required": ["query"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are HelloDJ's voice assistant. You answer questions concisely — 1-2 sentences max — because your answers will be spoken aloud via TTS. Use the available tools to fetch real-time data when needed. If you cannot answer with the tools, give a brief honest answer. Keep responses voice-friendly and natural."""


# ── Tool implementations ─────────────────────────────────────────────────

async def _call_weather(location: str) -> str:
    """Fetch weather from open-meteo (free, no API key needed)."""
    # Open-Meteo: geocode the location first
    url = f"https://api.open-meteo.com/v1/forecast"
    params = {
        "name": location,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
        "timezone": "auto",
    }
    try:
        async with aiohttp.ClientSession() as session:
            # First get coordinates
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search"
            async with session.get(geo_url, params={"name": location, "count": 1}) as resp:
                geo = await resp.json()
                if not geo.get("results"):
                    return f"I couldn't find a location named {location}."

                lat = geo["results"][0]["latitude"]
                lon = geo["results"][0]["longitude"]
                city = geo["results"][0]["name"]

            # Get weather
            params["latitude"] = lat
            params["longitude"] = lon
            async with session.get(url, params=params) as resp:
                data = await resp.json()
                daily = data.get("daily", {})
                if not daily:
                    return f"I couldn't get weather data for {city}."

                max_t = daily["temperature_2m_max"][0]
                min_t = daily["temperature_2m_min"][0]
                precip = daily.get("precipitation_sum", [0])[0]
                return (
                    f"In {city}, today's high is {max_t}°C, low is {min_t}°C "
                    f"with {precip}mm precipitation."
                )
    except Exception as exc:
        log.warning("Weather API error: %s", exc)
        return "I couldn't fetch the weather right now."


async def _call_news(topic: str | None = None) -> str:
    """Fetch news headlines."""
    api_key = os.getenv("NEWS_API_KEY", "")
    if not api_key:
        return "News is not configured — no API key set."

    url = "https://newsapi.org/v2/top-headlines"
    params = {"apiKey": api_key, "pageSize": 3}
    if topic:
        params["category"] = topic

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                data = await resp.json()
                articles = data.get("articles", [])
                if not articles:
                    return "No news headlines available right now."
                headlines = " | ".join(a["title"] for a in articles[:3])
                return f"Top headlines: {headlines}"
    except Exception as exc:
        log.warning("News API error: %s", exc)
        return "I couldn't fetch the news right now."


async def _call_stock(symbol: str) -> str:
    """Fetch stock price. Uses yfinance-style free endpoint."""
    # Using finnhub-free or polygon-free — yfinance is a local Python lib
    # For simplicity, use a free API
    api_key = os.getenv("STOCKS_API_KEY", "")
    if not api_key:
        return "Stock data is not configured — no API key set."

    url = f"https://finnhub.io/api/v1/quote"
    params = {"symbol": symbol, "token": api_key}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                data = await resp.json()
                if "c" not in data:
                    return f"I couldn't find data for {symbol}."

                current = data["c"]
                change = data["d"]
                percent = data["dp"]
                return (
                    f"{symbol} is at ${current:.2f}, "
                    f"{'up' if change >= 0 else 'down'} ${abs(change):.2f} "
                    f"({percent:.2f}%) today."
                )
    except Exception as exc:
        log.warning("Stock API error: %s", exc)
        return "I couldn't fetch stock data right now."


async def _call_astronomy(query: str) -> str:
    """Fetch astronomy data. Uses free APIs."""
    query_lower = query.lower()

    if "iss" in query_lower or "space station" in query_lower:
        # Get ISS position
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://api.wheretheiss.at/v1/satellites/25544") as resp:
                    data = await resp.json()
                    lat = data["latitude"]
                    lon = data["longitude"]
                    return (
                        f"The ISS is currently at latitude {lat:.2f}, "
                        f"longitude {lon:.2f} — passing overhead."
                    )
        except Exception as exc:
            log.warning("ISS API error: %s", exc)
            return "I couldn't get ISS position right now."

    if "moon" in query_lower:
        # Simple moon phase calculation
        import datetime
        # Approximate: use a known new moon date (2024-01-11) as reference
        ref = datetime.date(2024, 1, 11)
        days = (datetime.date.today() - ref).days
        phase = (days % 29.53) / 29.53
        phase_names = ["New Moon", "Waxing Crescent", "First Quarter",
                       "Waxing Gibbous", "Full Moon", "Waning Gibbous",
                       "Last Quarter", "Waning Crescent"]
        idx = int(phase * 8) % 8
        return f"The moon is currently in {phase_names[idx]} phase."

    return "I can look up ISS passes and moon phase. What would you like to know?"


# ── Tool dispatcher ──────────────────────────────────────────────────────

TOOL_DISPATCH = {
    "get_weather": _call_weather,
    "get_news": _call_news,
    "get_stock": _call_stock,
    "get_astronomy": _call_astronomy,
}


# ── LLM integration ──────────────────────────────────────────────────────

class QueryHandler:
    """Handles general queries via LLM with tool-calling."""

    def __init__(self):
        self._api_url = os.getenv("LLM_API_URL", "")
        self._api_key = os.getenv("LLM_API_KEY", "")

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    async def handle_query(self, query: str) -> str:
        """Process a general query and return a TTS-friendly response.

        Uses LLM function-calling to determine which tools to invoke.
        """
        if not self._api_key:
            return "I'm not connected to an AI service right now."

        # Use a simple pattern-based routing to avoid LLM latency
        # for common queries (fast path)
        fast_response = await self._fast_path(query)
        if fast_response:
            return fast_response

        # Full LLM path with tool calling
        return await self._llm_path(query)

    async def _fast_path(self, query: str) -> str | None:
        """Try to handle common queries without LLM overhead."""
        query_lower = query.lower()

        # Weather
        for kw in ("weather", "temperature", "forecast"):
            if kw in query_lower:
                # Extract location
                location = query_lower.replace(kw, "").strip()
                location = location.removeprefix("in").removeprefix("for").removeprefix("at").strip()
                if not location:
                    location = "London"
                return await _call_weather(location)

        # News
        if "news" in query_lower:
            topic = None
            for t in ("technology", "sports", "world", "business", "science"):
                if t in query_lower:
                    topic = t
                    break
            return await _call_news(topic)

        # Stocks
        if "stock" in query_lower or "price" in query_lower:
            # Try to extract ticker symbol
            words = query_lower.split()
            for w in words:
                if w.isupper() and len(w) <= 5 and w.isalpha():
                    return await _call_stock(w)
            return "Please specify a stock ticker symbol like AAPL or TSLA."

        # Astronomy
        if any(kw in query_lower for kw in ("astronomy", "space", "planet", "moon", "iss")):
            return await _call_astronomy(query_lower)

        return None

    async def _llm_path(self, query: str) -> str:
        """Full LLM chat completion with function-calling."""
        url = self._api_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        body = {
            "model": os.getenv("LLM_MODEL", "gpt-4o-mini"),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            "tools": MCP_TOOLS,
            "tool_choice": "auto",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=body) as resp:
                    result = await resp.json()

            choice = result.get("choices", [{}])[0]
            message = choice.get("message", {})

            # Check for tool calls
            tool_calls = message.get("tool_calls")
            if tool_calls:
                # Execute tool
                tc = tool_calls[0]
                func_name = tc["function"]["name"]
                arguments = json.loads(tc["function"]["arguments"])
                dispatcher = TOOL_DISPATCH.get(func_name)
                if dispatcher:
                    if func_name == "get_weather":
                        tool_result = await dispatcher(arguments["location"])
                    elif func_name == "get_news":
                        tool_result = await dispatcher(arguments.get("topic"))
                    elif func_name == "get_stock":
                        tool_result = await dispatcher(arguments["symbol"])
                    elif func_name == "get_astronomy":
                        tool_result = await dispatcher(arguments["query"])
                    else:
                        tool_result = "Unknown tool."

                    # Feed tool result back to LLM for final response
                    body["messages"].append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": tool_calls,
                    })
                    body["messages"].append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tool_result,
                    })
                    async with session.post(url, headers=headers, json=body) as resp:
                        result2 = await resp.json()
                    choice2 = result2.get("choices", [{}])[0]
                    return choice2.get("message", {}).get("content", "").strip()

            # Direct response (no tool call)
            return message.get("content", "").strip()

        except Exception as exc:
            log.warning("LLM query failed: %s", exc)
            return "I'm having trouble processing that right now."
