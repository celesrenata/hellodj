"""Unit tests for the per-guild content filter."""

from __future__ import annotations

import pytest

from playback_orchestrator.content_filter import ContentFilter

GUILD = 111


def test_add_and_match_artist_rule() -> None:
    cf = ContentFilter()
    cf.add_rule(GUILD, "artist", "Rick Astley", added_by=1)
    rule = cf.check_track(GUILD, author="rick astley")
    assert rule is not None
    assert rule.rule_type == "artist"


def test_keyword_substring_match() -> None:
    cf = ContentFilter()
    cf.add_rule(GUILD, "keyword", "explicit", added_by=1)
    assert cf.check_track(GUILD, title="Very EXPLICIT title") is not None
    assert cf.check_track(GUILD, title="clean title") is None


def test_exact_track_url_match() -> None:
    cf = ContentFilter()
    url = "https://youtu.be/xyz"
    cf.add_rule(GUILD, "track", url, added_by=1)
    assert cf.check_track(GUILD, url=url) is not None
    assert cf.check_track(GUILD, url="https://youtu.be/other") is None


def test_domain_glob_match() -> None:
    cf = ContentFilter()
    cf.add_rule(GUILD, "domain", "*.blocked.example", added_by=1)
    assert cf.check_track(GUILD, url="https://cdn.blocked.example/x") is not None
    assert cf.check_track(GUILD, url="https://allowed.example/x") is None


def test_no_rules_returns_none() -> None:
    cf = ContentFilter()
    assert cf.check_track(GUILD, title="anything") is None


def test_invalid_rule_type_raises() -> None:
    cf = ContentFilter()
    with pytest.raises(ValueError):
        cf.add_rule(GUILD, "bogus", "x", added_by=1)


def test_remove_rule() -> None:
    cf = ContentFilter()
    rule_id = cf.add_rule(GUILD, "keyword", "nope", added_by=1)
    assert cf.remove_rule(GUILD, rule_id) is True
    assert cf.remove_rule(GUILD, rule_id) is False
    assert cf.list_rules(GUILD) == []


def test_mapping_round_trip() -> None:
    cf = ContentFilter()
    cf.add_rule(GUILD, "artist", "Someone", added_by=7)
    restored = ContentFilter.from_mapping(cf.to_mapping())
    assert restored.check_track(GUILD, author="someone") is not None
