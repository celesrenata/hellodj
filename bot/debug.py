"""HelloDJ — Unified debug/diagnostic framework.

Provides structured, levelled debug logging across the entire bot. Each subsystem
registers a debug namespace (e.g. "player", "voice", "source", "queue") and
all debug output is switchable at runtime via environment variables.

Environment variables
---------------------
HELLODJ_DEBUG=1              — master switch; enables all debug output at DEBUG level
HELLODJ_DEBUG_MODULES=...    — comma-separated list of subsystems to enable
                               (e.g. "player,source,voice,queue,session,api")
                               Empty or "*" = all modules
HELLODJ_DEBUG_LEVEL=DEBUG    — minimum level for debug output (DEBUG, INFO, WARNING)
HELLODJ_DEBUG_TRACE=1        — enable ultra-verbose tracing (function entry/exit)

Usage::

    from debug import get_debug_logger, trace, debug_context

    dbg = get_debug_logger("player")  # namespace = "player"
    dbg.info("resolve track=%r provider=%r", title, provider)
    dbg.detail("search returned %d results: %r", len(results), results[:3])

    @trace  # logs function entry/exit with args
    async def _resolve_and_play(player, guild_id, entry):
        ...

    with debug_context("crossfade", guild_id=123):
        ...  # all debug output inside gets tagged with context
"""

from __future__ import annotations

import functools
import logging
import os
import time
from contextlib import contextmanager
from typing import Any

# ── Configuration ──────────────────────────────────────────────────────────

ENABLED = os.getenv("HELLODJ_DEBUG", "1") == "1"
TRACE_ENABLED = os.getenv("HELLODJ_DEBUG_TRACE", "0") == "1"
DEBUG_LEVEL = getattr(logging, os.getenv("HELLODJ_DEBUG_LEVEL", "DEBUG").upper(), logging.DEBUG)
DEBUG_MODULES_RAW = os.getenv("HELLODJ_DEBUG_MODULES", "*")
DEBUG_MODULES: set[str] = (
    set() if DEBUG_MODULES_RAW in ("*", "") else
    {m.strip().lower() for m in DEBUG_MODULES_RAW.split(",") if m.strip()}
)

# ── Debug Logger ───────────────────────────────────────────────────────────


class DebugLogger:
    """Structured debug logger for a specific subsystem.

    All output goes through the standard logging module so it integrates with
    the existing rotating-file + console handler setup.
    """

    def __init__(self, namespace: str):
        self.namespace = namespace
        self._logger = logging.getLogger(f"hellodj.debug.{namespace}")
        self._enabled = ENABLED and (not DEBUG_MODULES or namespace.lower() in DEBUG_MODULES)
        self._context: dict[str, Any] = {}

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _fmt(self, msg: str) -> str:
        prefix = f"[DBG:{self.namespace}]"
        if self._context:
            ctx = " ".join(f"{k}={v}" for k, v in self._context.items())
            return f"{prefix}({ctx}) {msg}"
        return f"{prefix} {msg}"

    def debug(self, msg: str, *args, **kwargs) -> None:
        if not self._enabled:
            return
        self._logger.debug(self._fmt(msg), *args, **kwargs)

    def info(self, msg: str, *args, **kwargs) -> None:
        if not self._enabled:
            return
        self._logger.info(self._fmt(msg), *args, **kwargs)

    def warning(self, msg: str, *args, **kwargs) -> None:
        self._logger.warning(self._fmt(msg), *args, **kwargs)

    def error(self, msg: str, *args, **kwargs) -> None:
        self._logger.error(self._fmt(msg), *args, **kwargs)

    def detail(self, msg: str, *args, **kwargs) -> None:
        """Ultra-verbose output (only when HELLODJ_DEBUG_TRACE=1)."""
        if not (self._enabled and TRACE_ENABLED):
            return
        self._logger.debug(self._fmt(f"[DETAIL] {msg}"), *args, **kwargs)

    def set_context(self, **kwargs) -> None:
        """Set per-call context tags (guild_id, user_id, etc.)."""
        self._context.update(kwargs)

    def clear_context(self) -> None:
        self._context.clear()

    def event(self, event_name: str, **data) -> None:
        """Log a structured event with key=value data."""
        if not self._enabled:
            return
        pairs = " ".join(f"{k}={v!r}" for k, v in data.items())
        self._logger.info(self._fmt(f"EVENT {event_name} {pairs}"))

    def timing(self, label: str, start_time: float) -> None:
        """Log elapsed time since start_time."""
        if not self._enabled:
            return
        elapsed_ms = (time.monotonic() - start_time) * 1000
        self._logger.info(self._fmt(f"TIMING {label} elapsed={elapsed_ms:.1f}ms"))


# ── Module registry ────────────────────────────────────────────────────────

_loggers: dict[str, DebugLogger] = {}


def get_debug_logger(namespace: str) -> DebugLogger:
    """Get or create a debug logger for a subsystem namespace."""
    if namespace not in _loggers:
        _loggers[namespace] = DebugLogger(namespace)
    return _loggers[namespace]


# ── Decorators ─────────────────────────────────────────────────────────────

def trace(func):
    """Decorator: log function entry/exit with arguments (TRACE mode only).

    Works on both sync and async functions.
    """
    if not (ENABLED and TRACE_ENABLED):
        return func

    namespace = func.__module__.split(".")[-1] if func.__module__ else "unknown"
    dbg = get_debug_logger(namespace)

    if functools._iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            call_id = f"{func.__qualname__}#{id(args):x}"
            dbg.detail("ENTER %s args=%r kwargs=%r", call_id, args[:3], kwargs)
            t0 = time.monotonic()
            try:
                result = await func(*args, **kwargs)
                dbg.detail("EXIT %s elapsed=%.1fms result_type=%s",
                           call_id, (time.monotonic() - t0) * 1000, type(result).__name__)
                return result
            except Exception as exc:
                dbg.detail("RAISE %s elapsed=%.1fms exc=%s",
                           call_id, (time.monotonic() - t0) * 1000, exc)
                raise
        return async_wrapper
    else:
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            call_id = f"{func.__qualname__}#{id(args):x}"
            dbg.detail("ENTER %s args=%r kwargs=%r", call_id, args[:3], kwargs)
            t0 = time.monotonic()
            try:
                result = func(*args, **kwargs)
                dbg.detail("EXIT %s elapsed=%.1fms result_type=%s",
                           call_id, (time.monotonic() - t0) * 1000, type(result).__name__)
                return result
            except Exception as exc:
                dbg.detail("RAISE %s elapsed=%.1fms exc=%s",
                           call_id, (time.monotonic() - t0) * 1000, exc)
                raise
        return sync_wrapper


@contextmanager
def debug_context(namespace: str, **kwargs):
    """Context manager that sets debug context for the duration."""
    dbg = get_debug_logger(namespace)
    dbg.set_context(**kwargs)
    try:
        yield dbg
    finally:
        dbg.clear_context()


# ── Startup banner ─────────────────────────────────────────────────────────

def log_debug_config() -> None:
    """Log the debug configuration at startup."""
    root = logging.getLogger("hellodj.debug")
    root.info(
        "HelloDJ debug framework: enabled=%s trace=%s level=%s modules=%s",
        ENABLED, TRACE_ENABLED, logging.getLevelName(DEBUG_LEVEL),
        DEBUG_MODULES_RAW,
    )
