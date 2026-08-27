"""Hypothesis strategies for HelloDJ SaaS platform property-based tests.

Shared generators used across all 12 correctness property tests defined in the
hellodj-saas-platform design document.
"""

from __future__ import annotations

from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Credential strategies (Properties 1, 2, 10, 11)
# ---------------------------------------------------------------------------

# Valid credential key strings — non-empty, printable, typical config-key chars
credential_keys = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P")),
    min_size=1,
    max_size=100,
).filter(lambda s: s.strip() == s and len(s.strip()) > 0)

# Credential values — arbitrary strings (may be empty, may contain any chars)
credential_values = st.text(min_size=0, max_size=10000)


# ---------------------------------------------------------------------------
# Tenant strategies (Properties 3, 4, 5, 6, 7, 9, 12)
# ---------------------------------------------------------------------------

# Valid UUIDs for tenant identification
tenant_ids = st.uuids()

# Discord user IDs — 18-digit snowflake integers (valid range for Discord)
discord_user_ids = st.integers(
    min_value=100000000000000000, max_value=999999999999999999
)


# ---------------------------------------------------------------------------
# Subscription strategies (Properties 4, 5, 6, 8, 9)
# ---------------------------------------------------------------------------

# Plan types as constrained by the schema CHECK
plans = st.sampled_from(["base", "trial"])

# Addon sets — subsets of available addons (unique list)
addon_sets = st.lists(
    st.sampled_from(["video", "premium", "additional_bot"]),
    unique=True,
    min_size=0,
    max_size=3,
)

# Subscription status values as constrained by the schema CHECK
subscription_statuses = st.sampled_from(
    ["active", "past_due", "cancelled", "expired", "pending_payment"]
)


# ---------------------------------------------------------------------------
# Feature subscription composite strategy (Property 9)
# ---------------------------------------------------------------------------

# Represents a valid subscription state for feature flag computation
feature_subscriptions = st.fixed_dictionaries(
    {
        "plan": plans,
        "addons": addon_sets,
        "status": st.just("active"),
    }
)
