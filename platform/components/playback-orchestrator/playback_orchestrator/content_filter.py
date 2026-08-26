"""Per-guild content filtering for the playback-orchestrator.

Rule-based content blocking with four rule types (ported from the legacy
on-prem filter, kept storage-agnostic for the AWS platform):

* ``artist``  — case-insensitive exact match against a track's author.
* ``track``   — exact match against a track's URL.
* ``domain``  — glob pattern (``fnmatch``) against the URL hostname.
* ``keyword`` — case-insensitive substring match against the track title.

Unlike the legacy module, this class does **no** file I/O. Per-guild rules are
held in memory and are loaded/persisted by the caller through the DynamoDB
``hellodj-core`` config path (Config entity), keeping the orchestrator's only
DynamoDB writer the session persistence layer. Rules can be seeded at
construction or mutated in-process.

Requirements: 6.1, 6.4
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatch
from urllib.parse import urlparse

__all__ = ["FilterRule", "ContentFilter", "RULE_TYPES"]

#: Valid rule-type identifiers.
RULE_TYPES: frozenset[str] = frozenset({"artist", "track", "domain", "keyword"})


@dataclass(frozen=True)
class FilterRule:
    """A single per-guild content-filter rule.

    Attributes:
        rule_id: Stable unique identifier for the rule.
        rule_type: One of :data:`RULE_TYPES`.
        value: The rule payload (artist, URL, domain glob, or keyword).
        added_by: Discord user id of the moderator who added the rule.
        added_at: ISO-8601 UTC timestamp of when the rule was added.
    """

    rule_id: str
    rule_type: str
    value: str
    added_by: int
    added_at: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON/DynamoDB-friendly mapping for this rule."""
        return {
            "id": self.rule_id,
            "type": self.rule_type,
            "value": self.value,
            "added_by": self.added_by,
            "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> FilterRule:
        """Build a :class:`FilterRule` from a stored mapping."""
        rule_type = str(data["type"])
        if rule_type not in RULE_TYPES:
            raise ValueError(
                f"invalid rule_type {rule_type!r}; must be one of "
                f"{', '.join(sorted(RULE_TYPES))}"
            )
        return cls(
            rule_id=str(data.get("id") or uuid.uuid4()),
            rule_type=rule_type,
            value=str(data["value"]),
            added_by=int(data.get("added_by", 0)),
            added_at=str(data.get("added_at") or _utc_now_iso()),
        )


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


@dataclass
class ContentFilter:
    """In-memory, per-guild content filter.

    The store maps ``guild_id`` to an ordered list of :class:`FilterRule`.
    Matching returns the first rule that matches a track; ordering therefore
    determines precedence. The class is storage-agnostic: the caller seeds it
    from and flushes it back to DynamoDB config.
    """

    _rules: dict[int, list[FilterRule]] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[int, Iterable[Mapping[str, object]]]) -> ContentFilter:
        """Construct a filter from a ``guild_id -> [rule dicts]`` mapping."""
        rules: dict[int, list[FilterRule]] = {}
        for guild_id, raw_rules in data.items():
            rules[int(guild_id)] = [FilterRule.from_dict(r) for r in raw_rules]
        return cls(_rules=rules)

    def add_rule(self, guild_id: int, rule_type: str, value: str, added_by: int) -> str:
        """Add a rule to a guild and return its generated id.

        Raises:
            ValueError: If ``rule_type`` is not one of :data:`RULE_TYPES`.
        """
        if rule_type not in RULE_TYPES:
            raise ValueError(
                f"invalid rule_type {rule_type!r}; must be one of "
                f"{', '.join(sorted(RULE_TYPES))}"
            )
        rule = FilterRule(
            rule_id=str(uuid.uuid4()),
            rule_type=rule_type,
            value=value,
            added_by=added_by,
            added_at=_utc_now_iso(),
        )
        self._rules.setdefault(guild_id, []).append(rule)
        return rule.rule_id

    def remove_rule(self, guild_id: int, rule_id: str) -> bool:
        """Remove a rule by id. Return ``True`` when a rule was removed."""
        rules = self._rules.get(guild_id)
        if not rules:
            return False
        for index, rule in enumerate(rules):
            if rule.rule_id == rule_id:
                rules.pop(index)
                if not rules:
                    del self._rules[guild_id]
                return True
        return False

    def list_rules(self, guild_id: int) -> list[FilterRule]:
        """Return a copy of the rules for ``guild_id`` (empty if none)."""
        return list(self._rules.get(guild_id, ()))

    def check_track(
        self,
        guild_id: int,
        *,
        title: str | None = None,
        author: str | None = None,
        url: str | None = None,
    ) -> FilterRule | None:
        """Return the first rule matching a track, or ``None`` if allowed.

        Args:
            guild_id: The guild whose rules to evaluate.
            title: Track title (checked by ``keyword`` rules).
            author: Track author/artist (checked by ``artist`` rules).
            url: Track URL (checked by ``track`` and ``domain`` rules).
        """
        rules = self._rules.get(guild_id)
        if not rules:
            return None

        title_lower = title.lower() if title else None
        author_lower = author.lower() if author else None
        url_hostname = _safe_hostname(url) if url else None

        for rule in rules:
            if self._rule_matches(
                rule,
                title_lower=title_lower,
                author_lower=author_lower,
                url=url,
                url_hostname=url_hostname,
            ):
                return rule
        return None

    @staticmethod
    def _rule_matches(
        rule: FilterRule,
        *,
        title_lower: str | None,
        author_lower: str | None,
        url: str | None,
        url_hostname: str | None,
    ) -> bool:
        """Return whether a single rule matches the given track fields."""
        if rule.rule_type == "artist" and author_lower is not None:
            return rule.value.lower() == author_lower
        if rule.rule_type == "track" and url is not None:
            return rule.value == url
        if rule.rule_type == "domain" and url_hostname is not None:
            return fnmatch(url_hostname, rule.value.lower())
        if rule.rule_type == "keyword" and title_lower is not None:
            return rule.value.lower() in title_lower
        return False

    def to_mapping(self) -> dict[int, list[dict[str, object]]]:
        """Return a ``guild_id -> [rule dicts]`` mapping for persistence."""
        return {
            guild_id: [rule.to_dict() for rule in rules]
            for guild_id, rules in self._rules.items()
        }


def _safe_hostname(url: str) -> str | None:
    """Return the lowercase hostname of ``url`` or ``None`` on parse failure."""
    try:
        return (urlparse(url).hostname or "").lower() or None
    except ValueError:
        return None
