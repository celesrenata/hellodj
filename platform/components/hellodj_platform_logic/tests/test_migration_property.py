"""Property-based test for the clean-slate migration filter (task 7.6).

Feature: aws-saas-replatform, Property 12

Property 12 (clean-slate migration filter): *for any* legacy dataset mixing all
record types, the migration filter SHALL output only the admin bootstrap
credential -- every returned record has ``record_type ==
ADMIN_BOOTSTRAP_CREDENTIAL``, every input record of that type is retained in
order, and no excluded-type (playback/session/playlist/configuration) record
survives.

Validates: Requirements 19.1, 19.2, 19.4
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.migration import (
    EXCLUDED_LEGACY_RECORD_TYPES,
    MIGRATED_LEGACY_RECORD_TYPE,
    filter_legacy,
)
from hellodj_platform_logic.types import LegacyRecord, LegacyRecordType

# Sample record types from the full closed set of legacy record kinds so every
# generated dataset can mix all types (admin bootstrap credential + every
# excluded playback/session/playlist/configuration kind).
_RECORD_TYPES = st.sampled_from(list(LegacyRecordType))

# Arbitrary (possibly empty) identifiers and payloads: the filter must reason
# purely over record_type, never over these opaque fields.
_TEXT = st.text(max_size=16)


@st.composite
def legacy_records(draw: st.DrawFn) -> list[LegacyRecord]:
    """Generate an arbitrary legacy dataset mixing all record types.

    Each record independently draws its ``record_type`` from the full
    :class:`LegacyRecordType` enum and arbitrary ``record_id``/``payload``
    values, so datasets range over empty lists, credential-only lists,
    excluded-only lists, and mixtures in any order.
    """
    return draw(
        st.lists(
            st.builds(
                LegacyRecord,
                record_type=_RECORD_TYPES,
                record_id=_TEXT,
                payload=_TEXT,
            ),
            max_size=40,
        )
    )


@settings(max_examples=200)
@given(records=legacy_records())
def test_clean_slate_migration_filter(records: list[LegacyRecord]) -> None:
    """Feature: aws-saas-replatform, Property 12.

    Validates: Requirements 19.1, 19.2, 19.4
    """
    result = filter_legacy(records)

    # --- Only the admin bootstrap credential survives (R19.1) --------------
    # Every returned record is an admin bootstrap credential; equivalently, no
    # excluded-type record (playback/session/playlist/configuration) survives
    # (R19.2, R19.4).
    for record in result:
        assert record.record_type is MIGRATED_LEGACY_RECORD_TYPE
        assert record.record_type is LegacyRecordType.ADMIN_BOOTSTRAP_CREDENTIAL
        assert record.record_type not in EXCLUDED_LEGACY_RECORD_TYPES

    # Pin the migrated type and the excluded set so the property cannot pass if
    # the clean-slate policy is broadened to carry legacy data forward.
    assert MIGRATED_LEGACY_RECORD_TYPE is (
        LegacyRecordType.ADMIN_BOOTSTRAP_CREDENTIAL
    )
    assert EXCLUDED_LEGACY_RECORD_TYPES == frozenset(
        {
            LegacyRecordType.PLAYBACK,
            LegacyRecordType.SESSION,
            LegacyRecordType.PLAYLIST,
            LegacyRecordType.CONFIGURATION,
        }
    )

    # --- Every credential record is retained, in input order (R19.1) -------
    # The surviving records are exactly the admin-bootstrap-credential records
    # from the input, in their original relative order (identity-preserving:
    # none dropped, none reordered, none fabricated).
    expected = [
        record
        for record in records
        if record.record_type is MIGRATED_LEGACY_RECORD_TYPE
    ]
    assert result == expected

    # Count invariant stated directly: the filter neither drops nor duplicates
    # any credential record.
    credential_count = sum(
        1
        for record in records
        if record.record_type is MIGRATED_LEGACY_RECORD_TYPE
    )
    assert len(result) == credential_count

    # --- No excluded-type record survives, stated directly (R19.2, R19.4) --
    assert not any(
        record.record_type in EXCLUDED_LEGACY_RECORD_TYPES for record in result
    )
