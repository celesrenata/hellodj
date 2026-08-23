"""Tests for the GPU Resource Scheduler (SR-IOV VF allocation).

Covers: allocation, release, capacity limits, re-allocation,
is_allocated query, active_sessions/available_vfs properties,
and GPUCapacityExceededError behavior.

Requirements: Req 4 (AC 1-5), Req 12 (AC 4)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the bot directory is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))

from video.gpu_scheduler import (
    MAX_VISUALIZER_VFS,
    GPUCapacityExceededError,
    GPUResourceScheduler,
    VFAllocation,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def scheduler() -> GPUResourceScheduler:
    """Create a default GPUResourceScheduler (max 7 VFs)."""
    return GPUResourceScheduler()


@pytest.fixture
def small_scheduler() -> GPUResourceScheduler:
    """Create a scheduler with only 2 VF slots for capacity testing."""
    return GPUResourceScheduler(max_visualizer_vfs=2)


# ---------------------------------------------------------------------------
# Unit Tests — Constants
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify module-level constants are correct."""

    def test_max_visualizer_vfs_is_seven(self):
        assert MAX_VISUALIZER_VFS == 7

    def test_scheduler_default_max(self, scheduler):
        assert scheduler._max_vfs == 7


# ---------------------------------------------------------------------------
# Unit Tests — Initial State
# ---------------------------------------------------------------------------


class TestInitialState:
    """Scheduler starts with no allocations."""

    def test_no_active_sessions(self, scheduler):
        assert scheduler.active_sessions == 0

    def test_all_vfs_available(self, scheduler):
        assert scheduler.available_vfs == 7

    def test_guild_not_allocated(self, scheduler):
        assert scheduler.is_allocated(12345) is False


# ---------------------------------------------------------------------------
# Unit Tests — Allocation
# ---------------------------------------------------------------------------


class TestAllocate:
    """allocate() assigns a VF slot and returns VFAllocation."""

    def test_returns_vf_allocation(self, scheduler):
        alloc = scheduler.allocate(guild_id=100, engine_type="projectm")
        assert isinstance(alloc, VFAllocation)

    def test_allocation_has_correct_guild_id(self, scheduler):
        alloc = scheduler.allocate(guild_id=100, engine_type="varda")
        assert alloc.guild_id == 100

    def test_allocation_has_correct_engine_type(self, scheduler):
        alloc = scheduler.allocate(guild_id=100, engine_type="fosfora")
        assert alloc.engine_type == "fosfora"

    def test_allocation_has_timestamp(self, scheduler):
        alloc = scheduler.allocate(guild_id=100, engine_type="audiovis")
        assert alloc.allocated_at > 0

    def test_increments_active_sessions(self, scheduler):
        scheduler.allocate(guild_id=100, engine_type="projectm")
        assert scheduler.active_sessions == 1

    def test_decrements_available_vfs(self, scheduler):
        scheduler.allocate(guild_id=100, engine_type="projectm")
        assert scheduler.available_vfs == 6

    def test_multiple_guilds(self, scheduler):
        scheduler.allocate(guild_id=100, engine_type="projectm")
        scheduler.allocate(guild_id=200, engine_type="varda")
        scheduler.allocate(guild_id=300, engine_type="fosfora")
        assert scheduler.active_sessions == 3
        assert scheduler.available_vfs == 4

    def test_guild_marked_allocated(self, scheduler):
        scheduler.allocate(guild_id=100, engine_type="projectm")
        assert scheduler.is_allocated(100) is True

    def test_other_guild_not_allocated(self, scheduler):
        scheduler.allocate(guild_id=100, engine_type="projectm")
        assert scheduler.is_allocated(999) is False


# ---------------------------------------------------------------------------
# Unit Tests — Release
# ---------------------------------------------------------------------------


class TestRelease:
    """release() frees a VF slot."""

    def test_release_allocated_guild(self, scheduler):
        scheduler.allocate(guild_id=100, engine_type="projectm")
        scheduler.release(guild_id=100)
        assert scheduler.active_sessions == 0
        assert scheduler.available_vfs == 7

    def test_release_marks_guild_unallocated(self, scheduler):
        scheduler.allocate(guild_id=100, engine_type="projectm")
        scheduler.release(guild_id=100)
        assert scheduler.is_allocated(100) is False

    def test_release_unallocated_guild_is_noop(self, scheduler):
        # Should not raise
        scheduler.release(guild_id=999)
        assert scheduler.active_sessions == 0

    def test_release_one_of_many(self, scheduler):
        scheduler.allocate(guild_id=100, engine_type="projectm")
        scheduler.allocate(guild_id=200, engine_type="varda")
        scheduler.allocate(guild_id=300, engine_type="fosfora")
        scheduler.release(guild_id=200)
        assert scheduler.active_sessions == 2
        assert scheduler.is_allocated(100) is True
        assert scheduler.is_allocated(200) is False
        assert scheduler.is_allocated(300) is True

    def test_double_release_is_noop(self, scheduler):
        scheduler.allocate(guild_id=100, engine_type="projectm")
        scheduler.release(guild_id=100)
        scheduler.release(guild_id=100)  # Should not raise
        assert scheduler.active_sessions == 0


# ---------------------------------------------------------------------------
# Unit Tests — Capacity Limit
# ---------------------------------------------------------------------------


class TestCapacityLimit:
    """GPUCapacityExceededError raised when all slots occupied."""

    def test_raises_when_full(self, small_scheduler):
        small_scheduler.allocate(guild_id=1, engine_type="projectm")
        small_scheduler.allocate(guild_id=2, engine_type="varda")
        with pytest.raises(GPUCapacityExceededError):
            small_scheduler.allocate(guild_id=3, engine_type="fosfora")

    def test_error_message_includes_max(self, small_scheduler):
        small_scheduler.allocate(guild_id=1, engine_type="projectm")
        small_scheduler.allocate(guild_id=2, engine_type="varda")
        with pytest.raises(GPUCapacityExceededError, match="2 visualizer VF slots"):
            small_scheduler.allocate(guild_id=3, engine_type="fosfora")

    def test_no_over_allocation(self, small_scheduler):
        small_scheduler.allocate(guild_id=1, engine_type="projectm")
        small_scheduler.allocate(guild_id=2, engine_type="varda")
        with pytest.raises(GPUCapacityExceededError):
            small_scheduler.allocate(guild_id=3, engine_type="fosfora")
        # Verify state unchanged after failed allocation
        assert small_scheduler.active_sessions == 2
        assert small_scheduler.available_vfs == 0
        assert small_scheduler.is_allocated(3) is False

    def test_allocate_after_release_succeeds(self, small_scheduler):
        small_scheduler.allocate(guild_id=1, engine_type="projectm")
        small_scheduler.allocate(guild_id=2, engine_type="varda")
        small_scheduler.release(guild_id=1)
        # Now there's room
        alloc = small_scheduler.allocate(guild_id=3, engine_type="fosfora")
        assert alloc.guild_id == 3
        assert small_scheduler.active_sessions == 2

    def test_fill_to_max_default(self, scheduler):
        """All 7 slots can be allocated."""
        for i in range(7):
            scheduler.allocate(guild_id=i, engine_type="projectm")
        assert scheduler.active_sessions == 7
        assert scheduler.available_vfs == 0
        with pytest.raises(GPUCapacityExceededError):
            scheduler.allocate(guild_id=99, engine_type="varda")


# ---------------------------------------------------------------------------
# Unit Tests — Re-allocation (same guild, new engine)
# ---------------------------------------------------------------------------


class TestReallocation:
    """Re-allocating a guild releases the old slot first."""

    def test_reallocation_updates_engine_type(self, scheduler):
        scheduler.allocate(guild_id=100, engine_type="projectm")
        alloc = scheduler.allocate(guild_id=100, engine_type="varda")
        assert alloc.engine_type == "varda"

    def test_reallocation_does_not_increase_count(self, scheduler):
        scheduler.allocate(guild_id=100, engine_type="projectm")
        scheduler.allocate(guild_id=100, engine_type="varda")
        assert scheduler.active_sessions == 1
        assert scheduler.available_vfs == 6

    def test_reallocation_updates_timestamp(self, scheduler):
        alloc1 = scheduler.allocate(guild_id=100, engine_type="projectm")
        alloc2 = scheduler.allocate(guild_id=100, engine_type="varda")
        assert alloc2.allocated_at >= alloc1.allocated_at

    def test_reallocation_when_full_succeeds_for_existing_guild(self, small_scheduler):
        """Re-allocating existing guild at capacity should work (releases first)."""
        small_scheduler.allocate(guild_id=1, engine_type="projectm")
        small_scheduler.allocate(guild_id=2, engine_type="varda")
        # Guild 1 re-allocates — should release first, then allocate
        alloc = small_scheduler.allocate(guild_id=1, engine_type="fosfora")
        assert alloc.engine_type == "fosfora"
        assert small_scheduler.active_sessions == 2


# ---------------------------------------------------------------------------
# Unit Tests — VFAllocation Dataclass
# ---------------------------------------------------------------------------


class TestVFAllocation:
    """VFAllocation dataclass behaves correctly."""

    def test_fields(self):
        alloc = VFAllocation(guild_id=42, engine_type="varda")
        assert alloc.guild_id == 42
        assert alloc.engine_type == "varda"
        assert isinstance(alloc.allocated_at, float)

    def test_custom_timestamp(self):
        alloc = VFAllocation(guild_id=1, engine_type="projectm", allocated_at=123.456)
        assert alloc.allocated_at == 123.456


# ---------------------------------------------------------------------------
# Unit Tests — Properties Consistency
# ---------------------------------------------------------------------------


class TestPropertyConsistency:
    """active_sessions + available_vfs == max_vfs always."""

    def test_empty(self, scheduler):
        assert scheduler.active_sessions + scheduler.available_vfs == 7

    def test_partially_filled(self, scheduler):
        scheduler.allocate(guild_id=1, engine_type="projectm")
        scheduler.allocate(guild_id=2, engine_type="varda")
        assert scheduler.active_sessions + scheduler.available_vfs == 7

    def test_full(self, scheduler):
        for i in range(7):
            scheduler.allocate(guild_id=i, engine_type="projectm")
        assert scheduler.active_sessions + scheduler.available_vfs == 7

    def test_after_release(self, scheduler):
        for i in range(5):
            scheduler.allocate(guild_id=i, engine_type="projectm")
        scheduler.release(guild_id=2)
        scheduler.release(guild_id=4)
        assert scheduler.active_sessions + scheduler.available_vfs == 7
