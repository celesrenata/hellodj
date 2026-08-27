"""Property-based test: Feature Flag Computation Correctness.

**Validates: Requirements 13.1, 13.2, 13.3, 13.4, 13.5**

Property 9: For any valid combination of plan and addons, the feature flag
computation SHALL enable exactly the features defined for that combination:
- Base_Plan/Trial enables `audio` only
- Video_Addon enables `video`, `activity`, `hls`, `visualizer`
- Premium_Addon enables `tidal_hifi`, `lossless`, `priority_queue`

No feature SHALL be enabled without the corresponding addon being active.
max_bot_instances = 1 + count(additional_bot), capped at 10.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import given, settings

# Ensure web-ui/services is importable
_webui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _webui_dir not in sys.path:
    sys.path.insert(0, _webui_dir)

from services.feature_flags import compute_features

from tests.strategies import addon_sets, plans


# ---------------------------------------------------------------------------
# Expected feature mappings (ground truth from requirements)
# ---------------------------------------------------------------------------

BASE_EXPECTED = {"audio"}
VIDEO_ADDON_FEATURES = {"video", "activity", "hls", "visualizer"}
PREMIUM_ADDON_FEATURES = {"tidal_hifi", "lossless", "priority_queue"}

ALL_GATED_FEATURES = BASE_EXPECTED | VIDEO_ADDON_FEATURES | PREMIUM_ADDON_FEATURES


# ---------------------------------------------------------------------------
# Property 9.1: Positive — correct features enabled for plan+addon combo
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(plan=plans, addons=addon_sets)
def test_feature_flags_enable_exactly_expected_features(plan: str, addons: list[str]):
    """Property 9.1: For any plan+addon combination, compute_features() returns
    exactly the expected features enabled.

    - Any active plan (base/trial) enables 'audio'
    - 'video' addon enables video/activity/hls/visualizer
    - 'premium' addon enables tidal_hifi/lossless/priority_queue
    - 'additional_bot' addon does NOT enable any feature flags

    **Validates: Requirements 13.1, 13.2, 13.3**
    """
    flags = compute_features(plan, addons)

    # Build expected set of enabled features
    expected_enabled: set[str] = set()
    expected_enabled |= BASE_EXPECTED  # Any valid plan enables audio

    if "video" in addons:
        expected_enabled |= VIDEO_ADDON_FEATURES
    if "premium" in addons:
        expected_enabled |= PREMIUM_ADDON_FEATURES

    # Check each feature flag matches expected
    for feature in ALL_GATED_FEATURES:
        assert flags[feature] == (feature in expected_enabled), (
            f"Feature '{feature}' should be "
            f"{'enabled' if feature in expected_enabled else 'disabled'} "
            f"for plan={plan!r}, addons={addons!r}, but got {flags[feature]}"
        )


# ---------------------------------------------------------------------------
# Property 9.2: Negative — no feature enabled without corresponding addon
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(plan=plans, addons=addon_sets)
def test_no_feature_enabled_without_addon(plan: str, addons: list[str]):
    """Property 9.2: No feature is enabled unless the corresponding plan or
    addon is present (inverse check).

    - video/activity/hls/visualizer require 'video' addon
    - tidal_hifi/lossless/priority_queue require 'premium' addon
    - audio requires an active plan (always true here since plans strategy
      only generates 'base'/'trial')

    **Validates: Requirements 13.4, 13.5**
    """
    flags = compute_features(plan, addons)

    # If 'video' addon is NOT present, video features must be disabled
    if "video" not in addons:
        for feature in VIDEO_ADDON_FEATURES:
            assert flags[feature] is False, (
                f"Feature '{feature}' is enabled without 'video' addon! "
                f"plan={plan!r}, addons={addons!r}"
            )

    # If 'premium' addon is NOT present, premium features must be disabled
    if "premium" not in addons:
        for feature in PREMIUM_ADDON_FEATURES:
            assert flags[feature] is False, (
                f"Feature '{feature}' is enabled without 'premium' addon! "
                f"plan={plan!r}, addons={addons!r}"
            )


# ---------------------------------------------------------------------------
# Property 9.3: max_bot_instances = 1 + count(additional_bot), capped at 10
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(plan=plans, addons=addon_sets)
def test_max_bot_instances_computation(plan: str, addons: list[str]):
    """Property 9.3: max_bot_instances equals 1 + count of 'additional_bot'
    addons in the list, capped at a maximum of 10.

    **Validates: Requirements 13.1, 13.2**
    """
    flags = compute_features(plan, addons)

    additional_bot_count = sum(1 for a in addons if a == "additional_bot")
    expected = min(1 + additional_bot_count, 10)

    assert flags["max_bot_instances"] == expected, (
        f"max_bot_instances should be {expected} for addons={addons!r}, "
        f"but got {flags['max_bot_instances']}"
    )
