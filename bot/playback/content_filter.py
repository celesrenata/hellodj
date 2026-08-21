"""HelloDJ — Per-guild content filtering.

Pure data module (no Discord imports). Provides rule-based content blocking
with four rule types:

  - artist  — Case-insensitive match against track's author field
  - track   — Exact URL match against track's webpage_url
  - domain  — Glob pattern (fnmatch) matched against URL hostname
  - keyword — Case-insensitive substring match against track's title

Data stored in ``data/content_filters.json`` keyed by guild_id (string).
Uses atomic writes and an asyncio.Lock for safe concurrent mutations.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from fnmatch import fnmatch
from urllib.parse import urlparse

__all__ = ["ContentFilter"]

log = logging.getLogger(__name__)

# Valid rule type identifiers
RULE_TYPES = frozenset({"artist", "track", "domain", "keyword"})


class ContentFilter:
    """Per-guild content filter with persistent JSON storage."""

    def __init__(self, data_path: str = "data/content_filters.json") -> None:
        self._data_path = data_path
        self._data: dict[str, dict] = {}  # { "guild_id": {"rules": [...]} }
        self._lock = asyncio.Lock()
        self._load()

    # ── Public API ──────────────────────────────────────────────────────

    async def add_rule(
        self, guild_id: int, rule_type: str, value: str, added_by: int
    ) -> str:
        """Add a filter rule to a guild. Returns the generated rule ID.

        Parameters
        ----------
        guild_id : int
            The Discord guild ID.
        rule_type : str
            One of: "artist", "track", "domain", "keyword".
        value : str
            The filter value (artist name, URL, domain glob, or keyword).
        added_by : int
            The Discord user ID of the moderator who added the rule.

        Returns
        -------
        str
            The UUID of the newly created rule.

        Raises
        ------
        ValueError
            If ``rule_type`` is not one of the valid types.
        """
        if rule_type not in RULE_TYPES:
            raise ValueError(
                f"Invalid rule_type {rule_type!r}. Must be one of: {', '.join(sorted(RULE_TYPES))}"
            )

        rule_id = str(uuid.uuid4())
        rule = {
            "id": rule_id,
            "type": rule_type,
            "value": value,
            "added_by": added_by,
            "added_at": datetime.now(timezone.utc).isoformat(),
        }

        async with self._lock:
            gid_str = str(guild_id)
            if gid_str not in self._data:
                self._data[gid_str] = {"rules": []}
            self._data[gid_str]["rules"].append(rule)
            self._save()

        log.info(
            "ContentFilter: added %s rule %r (id=%s) for guild %s by user %s",
            rule_type, value, rule_id, guild_id, added_by,
        )
        return rule_id

    async def remove_rule(self, guild_id: int, rule_id: str) -> bool:
        """Remove a rule by its ID. Returns True if found and removed."""
        async with self._lock:
            gid_str = str(guild_id)
            guild_data = self._data.get(gid_str)
            if guild_data is None:
                return False

            rules = guild_data["rules"]
            for i, rule in enumerate(rules):
                if rule["id"] == rule_id:
                    rules.pop(i)
                    # Clean up empty guild entries
                    if not rules:
                        del self._data[gid_str]
                    self._save()
                    log.info(
                        "ContentFilter: removed rule %s (type=%s, value=%r) from guild %s",
                        rule_id, rule["type"], rule["value"], guild_id,
                    )
                    return True

        return False

    def check_track(
        self,
        guild_id: int,
        *,
        title: str | None = None,
        author: str | None = None,
        url: str | None = None,
    ) -> dict | None:
        """Check if a track matches any filter rule for this guild.

        Returns the first matching rule dict, or None if no rule matches.

        Parameters
        ----------
        guild_id : int
            The Discord guild ID.
        title : str | None
            The track title (for keyword matching).
        author : str | None
            The track author/artist (for artist matching).
        url : str | None
            The track URL (for track and domain matching).
        """
        gid_str = str(guild_id)
        guild_data = self._data.get(gid_str)
        if guild_data is None:
            return None

        # Pre-compute lowercase values for case-insensitive matching
        title_lower = title.lower() if title else None
        author_lower = author.lower() if author else None

        # Extract hostname from URL for domain matching
        url_hostname: str | None = None
        if url:
            try:
                parsed = urlparse(url)
                url_hostname = (parsed.hostname or "").lower()
            except Exception:
                url_hostname = None

        for rule in guild_data["rules"]:
            rule_type = rule["type"]
            rule_value = rule["value"]

            if rule_type == "artist" and author_lower is not None:
                if rule_value.lower() == author_lower:
                    return rule

            elif rule_type == "track" and url is not None:
                if rule_value == url:
                    return rule

            elif rule_type == "domain" and url_hostname is not None:
                # Use fnmatch for glob-style domain pattern matching
                if fnmatch(url_hostname, rule_value.lower()):
                    return rule

            elif rule_type == "keyword" and title_lower is not None:
                if rule_value.lower() in title_lower:
                    return rule

        return None

    def list_rules(self, guild_id: int) -> list[dict]:
        """List all filter rules for a guild. Returns an empty list if none exist."""
        gid_str = str(guild_id)
        guild_data = self._data.get(gid_str)
        if guild_data is None:
            return []
        # Return copies to prevent external mutation
        return list(guild_data["rules"])

    # ── Persistence ─────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load filter data from disk. Safe to call once at init."""
        os.makedirs(os.path.dirname(self._data_path) or "data", exist_ok=True)
        if not os.path.exists(self._data_path):
            self._data = {}
            return
        try:
            with open(self._data_path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.error(
                "ContentFilter: could not read %s (%s); starting with empty filters.",
                self._data_path, exc,
            )
            self._data = {}

    def _save(self) -> None:
        """Atomically persist the in-memory store. Call while holding ``_lock``."""
        os.makedirs(os.path.dirname(self._data_path) or "data", exist_ok=True)
        tmp = f"{self._data_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._data_path)
