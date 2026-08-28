"""Tests for the best-effort auth rate limiter (``auth_ratelimit``).

Covers task 4 / Requirement 5.1: a key trips after N failures within the window
and resets once the window elapses or on an explicit success reset. Time is
injected (``now=``) so the tests are deterministic and fast.
"""

from __future__ import annotations

import pytest

from auth_ratelimit import RateLimited, RateLimiter


def test_allows_up_to_the_limit_then_trips():
    rl = RateLimiter(max_attempts=3, window_seconds=100)
    key = "1.2.3.4:login"
    # First 3 failures are allowed to be recorded; check() passes before each.
    for _ in range(3):
        rl.check(key, now=0)
        rl.record_failure(key, now=0)
    # 4th check trips.
    with pytest.raises(RateLimited):
        rl.check(key, now=0)


def test_window_elapse_resets():
    rl = RateLimiter(max_attempts=2, window_seconds=100)
    key = "1.2.3.4:login"
    rl.record_failure(key, now=0)
    rl.record_failure(key, now=0)
    with pytest.raises(RateLimited):
        rl.check(key, now=50)
    # After the window elapses the key is fresh again.
    rl.check(key, now=101)


def test_success_reset_clears_counter():
    rl = RateLimiter(max_attempts=2, window_seconds=100)
    key = "1.2.3.4:login"
    rl.record_failure(key, now=0)
    rl.record_failure(key, now=0)
    rl.reset(key)
    rl.check(key, now=1)  # no raise


def test_distinct_keys_isolated():
    rl = RateLimiter(max_attempts=1, window_seconds=100)
    rl.record_failure("a:login", now=0)
    with pytest.raises(RateLimited):
        rl.check("a:login", now=0)
    # A different key is unaffected.
    rl.check("b:login", now=0)


def test_unknown_key_never_trips():
    rl = RateLimiter(max_attempts=1, window_seconds=100)
    rl.check("never-seen", now=0)
