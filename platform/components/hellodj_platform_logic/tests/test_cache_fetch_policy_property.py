"""Property-based test for the cache-fetch-policy decision function (task 4.5).

Feature: hellodj-nix-native-delivery, Property 7

Property 7 (cache unreachability permits a recorded local rebuild): *for any*
combination of whether the cache responded within its budget and how many
consecutive retry attempts have been made, ``cache_fetch_policy`` SHALL permit
(and record) a local rebuild exactly when the cache did not respond within
budget or the ``CACHE_RETRY_LIMIT`` (3) consecutive retries were exhausted, and
SHALL NOT force a rebuild otherwise. The outcome also records whether the cache
responded within budget, whether the retries were exhausted (retries >= 3), and
a non-empty human-readable reason.

Validates: Requirements 7.6
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.binary_cache import CACHE_RETRY_LIMIT, cache_fetch_policy
from hellodj_platform_logic.types import CacheFetchOutcome

# Retry counts span values below the limit (0, 1, 2), the limit itself (3), and
# well above it, so both the "not exhausted" and "exhausted" branches are
# exercised, including the exact boundary at CACHE_RETRY_LIMIT.
_RETRIES = st.integers(min_value=0, max_value=10)


@settings(max_examples=200)
@given(responded=st.booleans(), retries=_RETRIES)
def test_cache_unreachability_permits_recorded_rebuild(
    responded: bool,
    retries: int,
) -> None:
    """Feature: hellodj-nix-native-delivery, Property 7.

    Validates: Requirements 7.6
    """
    outcome = cache_fetch_policy(responded, retries)

    # The oracle: a rebuild is permitted exactly when the cache did not respond
    # within budget OR the consecutive retries were exhausted (>= 3).
    expected_retries_exhausted = retries >= CACHE_RETRY_LIMIT
    expected_rebuilt = (not responded) or expected_retries_exhausted

    assert isinstance(outcome, CacheFetchOutcome)

    # --- rebuilt_locally matches the (not responded or retries>=3) oracle ---
    assert outcome.rebuilt_locally is expected_rebuilt

    # --- retries_exhausted matches retries >= CACHE_RETRY_LIMIT -------------
    assert outcome.retries_exhausted is expected_retries_exhausted

    # --- responded_within_timeout faithfully echoes the input --------------
    assert outcome.responded_within_timeout is responded

    # --- a rebuild is never forced when the cache is healthy ---------------
    # (responded within budget AND retries not exhausted => no rebuild).
    if responded and not expected_retries_exhausted:
        assert outcome.rebuilt_locally is False

    # --- the reason is always a non-empty explanation ----------------------
    assert isinstance(outcome.reason, str)
    assert outcome.reason != ""
