"""HelloDJ — Metrics tracking for AI token/call usage.

JSON-backed usage tracking for LLM / STT / TTS / wake-word activity, following
the ``storage.py`` pattern (asyncio-locked, atomically written). Records are
appended with an epoch timestamp and rolled up into daily/weekly/monthly
aggregates so the dashboard can show trends without scanning every event.

Retention is controlled by ``METRICS_RETENTION_DAYS`` (default 30). Records
older than the retention window are pruned during rollup.
"""

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR", "data")
METRICS_FILE = os.path.join(DATA_DIR, "metrics.json")
RETENTION_DAYS = int(os.environ.get("METRICS_RETENTION_DAYS", "30"))

# Rollup is expensive enough to throttle: run at most once per ROLLUP_INTERVAL_S.
ROLLUP_INTERVAL_S = 60 * 60  # once an hour


class MetricsStore:
    """Async-safe metrics accumulator persisted to a single JSON file.

    The in-memory store mirrors the on-disk shape so reads are cheap; all
    mutations are serialized behind ``_lock`` and written atomically.
    """

    def __init__(self):
        self._lock = asyncio.Lock()
        self._data = {"llm": [], "stt": [], "tts": [], "wakeword": [], "rollups": {}}
        self._last_rollup = 0.0
        self._loaded = False

    # ── persistence ─────────────────────────────────────────────────────

    def _save(self) -> None:
        """Atomically persist the in-memory store. Call while holding ``_lock``."""
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = f"{METRICS_FILE}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, METRICS_FILE)

    def load(self) -> None:
        """Load metrics from disk into memory. Safe to call once at startup."""
        os.makedirs(DATA_DIR, exist_ok=True)
        if not os.path.exists(METRICS_FILE):
            self._data = {"llm": [], "stt": [], "tts": [], "wakeword": [], "rollups": {}}
            return
        try:
            with open(METRICS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            self._data = {
                "llm": loaded.get("llm", []),
                "stt": loaded.get("stt", []),
                "tts": loaded.get("tts", []),
                "wakeword": loaded.get("wakeword", []),
                "rollups": loaded.get("rollups", {}),
            }
        except (json.JSONDecodeError, OSError) as exc:
            log.error("HelloDJ could not read %s (%s); starting empty.", METRICS_FILE, exc)
            self._data = {"llm": [], "stt": [], "tts": [], "wakeword": [], "rollups": {}}
        self._last_rollup = 0.0
        self._loaded = True

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _now(self) -> float:
        return time.time()

    # ── rollups ─────────────────────────────────────────────────────────

    @staticmethod
    def _day_key(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")

    @staticmethod
    def _week_key(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%G-W%V")

    @staticmethod
    def _month_key(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%Y-%m")

    def _add_to_rollup(self, ts: float, kind: str, extra: dict) -> None:
        """Increment the daily/weekly/monthly counters for one record."""
        rollups = self._data["rollups"]
        for period, key in (
            ("daily", self._day_key(ts)),
            ("weekly", self._week_key(ts)),
            ("monthly", self._month_key(ts)),
        ):
            bucket = rollups.setdefault(period, {}).setdefault(key, {
                "calls": 0,
                "tokens": 0,
                "duration_ms": 0,
                "chars": 0,
                "wakewords": 0,
            })
            bucket["calls"] += 1
            if kind == "llm":
                bucket["tokens"] += extra.get("input_tokens", 0) + extra.get("output_tokens", 0)
            elif kind == "stt":
                bucket["duration_ms"] += extra.get("duration_ms", 0)
            elif kind == "tts":
                bucket["chars"] += extra.get("chars", 0)
            elif kind == "wakeword":
                bucket["wakewords"] += 1

    def _rollup_and_prune(self) -> None:
        """Regenerate rollups from the raw records and prune old events.

        Call while holding ``_lock``. Rollup is idempotent: it rebuilds the
        rollup buckets from the (pruned) raw lists, so a missed write cannot
        desync the aggregates.
        """
        cutoff = self._now() - (RETENTION_DAYS * 86400)

        # Prune records older than the retention window.
        for kind in ("llm", "stt", "tts", "wakeword"):
            self._data[kind] = [
                r for r in self._data[kind]
                if r.get("ts", 0) >= cutoff
            ]

        # Rebuild rollup buckets from the surviving records.
        rollups = {"daily": {}, "weekly": {}, "monthly": {}}
        for kind, extra in (
            ("llm", {}),
            ("stt", {}),
            ("tts", {}),
            ("wakeword", {}),
        ):
            for rec in self._data[kind]:
                self._add_to_rollup(rec.get("ts", 0), kind, rec)

        self._data["rollups"] = rollups

    async def _maybe_rollup(self) -> None:
        """Throttled rollup+prune, invoked opportunistically on writes."""
        now = self._now()
        if now - self._last_rollup < ROLLUP_INTERVAL_S:
            return
        self._rollup_and_prune()
        self._last_rollup = now

    # ── recorders ────────────────────────────────────────────────────────

    async def record_llm_call(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: float,
        engine: str = "openai",
    ) -> None:
        """Record one LLM chat-completion call."""
        try:
            await self._ensure_loaded()
            async with self._lock:
                self._data["llm"].append({
                    "ts": self._now(),
                    "model": model or "",
                    "engine": engine,
                    "input_tokens": max(input_tokens or 0, 0),
                    "output_tokens": max(output_tokens or 0, 0),
                    "latency_ms": latency_ms or 0,
                })
                await self._maybe_rollup()
                self._save()
        except Exception as exc:
            log.warning("Could not record LLM metrics: %s", exc)

    async def record_stt_call(self, engine: str, duration_ms: float) -> None:
        """Record one STT (speech-to-text) call."""
        try:
            await self._ensure_loaded()
            async with self._lock:
                self._data["stt"].append({
                    "ts": self._now(),
                    "engine": engine or "local",
                    "duration_ms": max(duration_ms or 0, 0),
                })
                await self._maybe_rollup()
                self._save()
        except Exception as exc:
            log.warning("Could not record STT metrics: %s", exc)

    async def record_tts_call(self, engine: str, chars: int) -> None:
        """Record one TTS (text-to-speech) synthesis call."""
        try:
            await self._ensure_loaded()
            async with self._lock:
                self._data["tts"].append({
                    "ts": self._now(),
                    "engine": engine or "kokoro",
                    "chars": max(chars or 0, 0),
                })
                await self._maybe_rollup()
                self._save()
        except Exception as exc:
            log.warning("Could not record TTS metrics: %s", exc)

    async def record_wakeword(self) -> None:
        """Record one wake-word detection."""
        try:
            await self._ensure_loaded()
            async with self._lock:
                self._data["wakeword"].append({
                    "ts": self._now(),
                })
                await self._maybe_rollup()
                self._save()
        except Exception as exc:
            log.warning("Could not record wake-word metrics: %s", exc)

    # ── queries ─────────────────────────────────────────────────────────

    @staticmethod
    def _period_start(period: str) -> float:
        """Return the epoch start of the requested period."""
        now = datetime.now()
        if period == "today":
            return datetime(now.year, now.month, now.day).timestamp()
        if period == "week":
            # ISO week start (Monday).
            monday = now - timedelta(days=now.weekday())
            return datetime(monday.year, monday.month, monday.day).timestamp()
        if period == "month":
            return datetime(now.year, now.month, 1).timestamp()
        if period in ("all", "alltime"):
            return 0.0
        return datetime(now.year, now.month, now.day).timestamp()

    async def get_summary(self, period: str = "today") -> dict:
        """Return aggregated usage stats for the requested period.

        ``period`` is one of: today | week | month | all.
        """
        try:
            await self._ensure_loaded()
        except Exception:
            pass

        start = self._period_start(period)
        llm = [r for r in self._data.get("llm", []) if r.get("ts", 0) >= start]
        stt = [r for r in self._data.get("stt", []) if r.get("ts", 0) >= start]
        tts = [r for r in self._data.get("tts", []) if r.get("ts", 0) >= start]
        wake = [r for r in self._data.get("wakeword", []) if r.get("ts", 0) >= start]

        total_tokens = sum(
            r.get("input_tokens", 0) + r.get("output_tokens", 0) for r in llm
        )
        total_input = sum(r.get("input_tokens", 0) for r in llm)
        total_output = sum(r.get("output_tokens", 0) for r in llm)
        latency_ms = sum(r.get("latency_ms", 0) for r in llm)

        # Per-engine breakdown for STT / TTS.
        def _engine_breakdown(records: list[dict]) -> dict:
            by_engine: dict = {}
            for r in records:
                eng = r.get("engine", "unknown") or "unknown"
                bucket = by_engine.setdefault(eng, {"calls": 0, "total": 0})
                bucket["calls"] += 1
                bucket["total"] += r.get("duration_ms", 0) or r.get("chars", 0) or 0
            return by_engine

        return {
            "period": period,
            "llm": {
                "calls": len(llm),
                "tokens": total_tokens,
                "input_tokens": total_input,
                "output_tokens": total_output,
                "avg_latency_ms": round(latency_ms / len(llm), 2) if llm else 0,
                "models": self._model_breakdown(llm),
            },
            "stt": {
                "calls": len(stt),
                "duration_ms": sum(r.get("duration_ms", 0) for r in stt),
                "engines": _engine_breakdown(stt),
            },
            "tts": {
                "calls": len(tts),
                "chars": sum(r.get("chars", 0) for r in tts),
                "engines": _engine_breakdown(tts),
            },
            "wakeword": {
                "detections": len(wake),
            },
        }

    @staticmethod
    def _model_breakdown(records: list[dict]) -> dict:
        """Per-model LLM breakdown: {model: {calls, tokens}}."""
        by_model: dict = {}
        for r in records:
            model = r.get("model", "unknown") or "unknown"
            bucket = by_model.setdefault(model, {"calls": 0, "tokens": 0})
            bucket["calls"] += 1
            bucket["tokens"] += r.get("input_tokens", 0) + r.get("output_tokens", 0)
        return by_model

    async def get_daily_breakdown(self, days: int = 7) -> list:
        """Return per-day usage for the last ``days`` days (oldest first).

        Each entry: {date, llm_calls, tokens, stt_calls, tts_calls, wakewords}.
        """
        try:
            await self._ensure_loaded()
        except Exception:
            pass

        today = datetime.now().date()
        start_day = today - timedelta(days=days - 1)
        start_ts = datetime(start_day.year, start_day.month, start_day.day).timestamp()

        buckets: dict = defaultdict(lambda: {
            "date": "", "llm_calls": 0, "tokens": 0,
            "stt_calls": 0, "tts_calls": 0, "wakewords": 0,
        })

        for r in self._data.get("llm", []):
            if r.get("ts", 0) < start_ts:
                continue
            d = self._day_key(r["ts"])
            b = buckets[d]
            b["llm_calls"] += 1
            b["tokens"] += r.get("input_tokens", 0) + r.get("output_tokens", 0)

        for r in self._data.get("stt", []):
            if r.get("ts", 0) < start_ts:
                continue
            d = self._day_key(r["ts"])
            buckets[d]["stt_calls"] += 1

        for r in self._data.get("tts", []):
            if r.get("ts", 0) < start_ts:
                continue
            d = self._day_key(r["ts"])
            buckets[d]["tts_calls"] += 1

        for r in self._data.get("wakeword", []):
            if r.get("ts", 0) < start_ts:
                continue
            d = self._day_key(r["ts"])
            buckets[d]["wakewords"] += 1

        # Fill every day in the window so charts have a continuous axis.
        ordered = []
        for i in range(days):
            day = start_day + timedelta(days=i)
            key = day.strftime("%Y-%m-%d")
            b = buckets.get(key)
            if b is None:
                b = {
                    "date": key, "llm_calls": 0, "tokens": 0,
                    "stt_calls": 0, "tts_calls": 0, "wakewords": 0,
                }
            b["date"] = key
            ordered.append(b)
        return ordered

    # ── maintenance ─────────────────────────────────────────────────────

    async def rollup_now(self) -> None:
        """Force a rollup+prune (used at startup / scheduled maintenance)."""
        try:
            await self._ensure_loaded()
            async with self._lock:
                self._rollup_and_prune()
                self._last_rollup = self._now()
                self._save()
        except Exception as exc:
            log.warning("Could not run metrics rollup: %s", exc)


# Module-level singleton used by the voice pipeline and cogs.
metrics = MetricsStore()


def load() -> None:
    """Load the metrics store from disk (call once at bot startup)."""
    metrics.load()
