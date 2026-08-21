"""Tests for ContentFilter module.

Validates Requirements 12.1–12.8:
- artist rule: case-insensitive author match
- track rule: exact URL match
- domain rule: glob pattern on URL hostname
- keyword rule: case-insensitive title substring
- add/remove/list rules per guild
- check_track returns matching rule or None
"""

from __future__ import annotations

import os
import uuid as uuid_mod

import pytest
import pytest_asyncio

from content_filter import ContentFilter, RULE_TYPES


@pytest.fixture
def tmp_data_path(tmp_path):
    """Provide a temporary path for the filter JSON file."""
    return str(tmp_path / "content_filters.json")


@pytest.fixture
def content_filter(tmp_data_path):
    """Create a ContentFilter instance backed by a temp file."""
    return ContentFilter(data_path=tmp_data_path)


class TestAddRule:
    @pytest.mark.asyncio
    async def test_add_artist_rule(self, content_filter):
        rule_id = await content_filter.add_rule(123, "artist", "Rick Astley", 456)
        assert rule_id is not None
        rules = content_filter.list_rules(123)
        assert len(rules) == 1
        assert rules[0]["type"] == "artist"
        assert rules[0]["value"] == "Rick Astley"
        assert rules[0]["added_by"] == 456
        assert rules[0]["id"] == rule_id

    @pytest.mark.asyncio
    async def test_add_track_rule(self, content_filter):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        rule_id = await content_filter.add_rule(123, "track", url, 456)
        rules = content_filter.list_rules(123)
        assert len(rules) == 1
        assert rules[0]["type"] == "track"
        assert rules[0]["value"] == url

    @pytest.mark.asyncio
    async def test_add_domain_rule(self, content_filter):
        rule_id = await content_filter.add_rule(123, "domain", "*.example.com", 456)
        rules = content_filter.list_rules(123)
        assert len(rules) == 1
        assert rules[0]["type"] == "domain"
        assert rules[0]["value"] == "*.example.com"

    @pytest.mark.asyncio
    async def test_add_keyword_rule(self, content_filter):
        rule_id = await content_filter.add_rule(123, "keyword", "explicit", 456)
        rules = content_filter.list_rules(123)
        assert len(rules) == 1
        assert rules[0]["type"] == "keyword"
        assert rules[0]["value"] == "explicit"

    @pytest.mark.asyncio
    async def test_add_invalid_type_raises(self, content_filter):
        with pytest.raises(ValueError, match="Invalid rule_type"):
            await content_filter.add_rule(123, "invalid_type", "foo", 456)

    @pytest.mark.asyncio
    async def test_add_multiple_rules_same_guild(self, content_filter):
        await content_filter.add_rule(123, "artist", "Artist A", 456)
        await content_filter.add_rule(123, "keyword", "bad word", 789)
        rules = content_filter.list_rules(123)
        assert len(rules) == 2

    @pytest.mark.asyncio
    async def test_add_rules_different_guilds(self, content_filter):
        await content_filter.add_rule(111, "artist", "Artist A", 456)
        await content_filter.add_rule(222, "artist", "Artist B", 789)
        assert len(content_filter.list_rules(111)) == 1
        assert len(content_filter.list_rules(222)) == 1


class TestRemoveRule:
    @pytest.mark.asyncio
    async def test_remove_existing_rule(self, content_filter):
        rule_id = await content_filter.add_rule(123, "artist", "Test", 456)
        result = await content_filter.remove_rule(123, rule_id)
        assert result is True
        assert content_filter.list_rules(123) == []

    @pytest.mark.asyncio
    async def test_remove_nonexistent_rule(self, content_filter):
        result = await content_filter.remove_rule(123, "nonexistent-id")
        assert result is False

    @pytest.mark.asyncio
    async def test_remove_from_nonexistent_guild(self, content_filter):
        result = await content_filter.remove_rule(999, "some-id")
        assert result is False

    @pytest.mark.asyncio
    async def test_remove_only_target_rule(self, content_filter):
        id1 = await content_filter.add_rule(123, "artist", "Keep", 456)
        id2 = await content_filter.add_rule(123, "artist", "Remove", 456)
        await content_filter.remove_rule(123, id2)
        rules = content_filter.list_rules(123)
        assert len(rules) == 1
        assert rules[0]["value"] == "Keep"


class TestCheckTrack:
    @pytest.mark.asyncio
    async def test_artist_match_case_insensitive(self, content_filter):
        await content_filter.add_rule(123, "artist", "Rick Astley", 456)
        result = content_filter.check_track(123, author="rick astley")
        assert result is not None
        assert result["type"] == "artist"

    @pytest.mark.asyncio
    async def test_artist_match_uppercase(self, content_filter):
        await content_filter.add_rule(123, "artist", "rick astley", 456)
        result = content_filter.check_track(123, author="RICK ASTLEY")
        assert result is not None

    @pytest.mark.asyncio
    async def test_artist_no_match(self, content_filter):
        await content_filter.add_rule(123, "artist", "Rick Astley", 456)
        result = content_filter.check_track(123, author="Queen")
        assert result is None

    @pytest.mark.asyncio
    async def test_track_exact_url_match(self, content_filter):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        await content_filter.add_rule(123, "track", url, 456)
        result = content_filter.check_track(123, url=url)
        assert result is not None
        assert result["type"] == "track"

    @pytest.mark.asyncio
    async def test_track_url_no_match(self, content_filter):
        await content_filter.add_rule(123, "track", "https://example.com/a", 456)
        result = content_filter.check_track(123, url="https://example.com/b")
        assert result is None

    @pytest.mark.asyncio
    async def test_domain_glob_match(self, content_filter):
        await content_filter.add_rule(123, "domain", "*.example.com", 456)
        result = content_filter.check_track(123, url="https://sub.example.com/video")
        assert result is not None
        assert result["type"] == "domain"

    @pytest.mark.asyncio
    async def test_domain_exact_match(self, content_filter):
        await content_filter.add_rule(123, "domain", "evil.com", 456)
        result = content_filter.check_track(123, url="https://evil.com/track/123")
        assert result is not None

    @pytest.mark.asyncio
    async def test_domain_no_match(self, content_filter):
        await content_filter.add_rule(123, "domain", "*.evil.com", 456)
        result = content_filter.check_track(123, url="https://good.com/track")
        assert result is None

    @pytest.mark.asyncio
    async def test_domain_case_insensitive(self, content_filter):
        await content_filter.add_rule(123, "domain", "*.EXAMPLE.COM", 456)
        result = content_filter.check_track(123, url="https://sub.example.com/x")
        assert result is not None

    @pytest.mark.asyncio
    async def test_keyword_substring_match(self, content_filter):
        await content_filter.add_rule(123, "keyword", "explicit", 456)
        result = content_filter.check_track(123, title="Song Title [Explicit Version]")
        assert result is not None
        assert result["type"] == "keyword"

    @pytest.mark.asyncio
    async def test_keyword_case_insensitive(self, content_filter):
        await content_filter.add_rule(123, "keyword", "NSFW", 456)
        result = content_filter.check_track(123, title="some nsfw content here")
        assert result is not None

    @pytest.mark.asyncio
    async def test_keyword_no_match(self, content_filter):
        await content_filter.add_rule(123, "keyword", "blocked", 456)
        result = content_filter.check_track(123, title="Perfectly Fine Song")
        assert result is None

    def test_no_rules_returns_none(self, content_filter):
        result = content_filter.check_track(123, title="Anything", author="Anyone")
        assert result is None

    @pytest.mark.asyncio
    async def test_different_guild_no_match(self, content_filter):
        await content_filter.add_rule(111, "artist", "Blocked Artist", 456)
        result = content_filter.check_track(222, author="Blocked Artist")
        assert result is None

    @pytest.mark.asyncio
    async def test_check_with_none_fields(self, content_filter):
        await content_filter.add_rule(123, "artist", "Test", 456)
        # Only title provided, artist rule should not match
        result = content_filter.check_track(123, title="Some title")
        assert result is None

    @pytest.mark.asyncio
    async def test_first_matching_rule_returned(self, content_filter):
        id1 = await content_filter.add_rule(123, "artist", "Artist", 456)
        id2 = await content_filter.add_rule(123, "keyword", "artist", 456)
        # Both could match if author="Artist" and title has "artist"
        # But artist rule is first in the list
        result = content_filter.check_track(123, author="Artist", title="featuring artist")
        assert result is not None
        assert result["id"] == id1


class TestListRules:
    def test_list_empty_guild(self, content_filter):
        assert content_filter.list_rules(999) == []

    @pytest.mark.asyncio
    async def test_list_returns_copies(self, content_filter):
        await content_filter.add_rule(123, "artist", "Test", 456)
        rules1 = content_filter.list_rules(123)
        rules2 = content_filter.list_rules(123)
        assert rules1 == rules2
        # Modifying one list should not affect the other
        rules1.clear()
        assert len(content_filter.list_rules(123)) == 1


class TestPersistence:
    @pytest.mark.asyncio
    async def test_data_persists_to_file(self, tmp_data_path):
        cf = ContentFilter(data_path=tmp_data_path)
        await cf.add_rule(123, "artist", "Persisted Artist", 456)

        # Create new instance from same file
        cf2 = ContentFilter(data_path=tmp_data_path)
        rules = cf2.list_rules(123)
        assert len(rules) == 1
        assert rules[0]["value"] == "Persisted Artist"

    @pytest.mark.asyncio
    async def test_remove_persists(self, tmp_data_path):
        cf = ContentFilter(data_path=tmp_data_path)
        rule_id = await cf.add_rule(123, "artist", "Temp", 456)
        await cf.remove_rule(123, rule_id)

        cf2 = ContentFilter(data_path=tmp_data_path)
        assert cf2.list_rules(123) == []

    def test_handles_corrupt_file(self, tmp_data_path):
        # Write corrupt JSON
        os.makedirs(os.path.dirname(tmp_data_path), exist_ok=True)
        with open(tmp_data_path, "w") as f:
            f.write("not valid json {{{")

        # Should not crash, just start empty
        cf = ContentFilter(data_path=tmp_data_path)
        assert cf.list_rules(123) == []

    def test_handles_missing_file(self, tmp_data_path):
        # File doesn't exist — should start empty
        cf = ContentFilter(data_path=tmp_data_path)
        assert cf.list_rules(123) == []


class TestRuleMetadata:
    @pytest.mark.asyncio
    async def test_rule_has_added_at_timestamp(self, content_filter):
        await content_filter.add_rule(123, "artist", "Test", 456)
        rules = content_filter.list_rules(123)
        assert "added_at" in rules[0]
        # Should be a valid ISO timestamp
        from datetime import datetime
        dt = datetime.fromisoformat(rules[0]["added_at"])
        assert dt.tzinfo is not None  # Should be timezone-aware

    @pytest.mark.asyncio
    async def test_rule_has_uuid_id(self, content_filter):
        rule_id = await content_filter.add_rule(123, "artist", "Test", 456)
        # Should be a valid UUID
        parsed = uuid_mod.UUID(rule_id)
        assert str(parsed) == rule_id
