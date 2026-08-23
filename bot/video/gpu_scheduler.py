"""GPU Resource Scheduler — SR-IOV VF allocation for visualizer engines.

Manages the allocation of Intel Meteor Lake SR-IOV Virtual Functions (VFs)
across concurrent visualizer sessions. Each guild gets at most one VF slot.
One VF is reserved for the video transcode pipeline, leaving 7 for visualizers.
"""

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

MAX_VFS_PER_NODE = 8
RESERVED_FOR_VIDEO_TRANSCODE = 1
MAX_VISUALIZER_VFS = MAX_VFS_PER_NODE - RESERVED_FOR_VIDEO_TRANSCODE  # 7


@dataclass
class VFAllocation:
    """Represents a single SR-IOV VF allocation for a visualizer session."""

    guild_id: int
    engine_type: str
    allocated_at: float = field(default_factory=time.monotonic)


class GPUCapacityExceededError(Exception):
    """Raised when no SR-IOV VFs are available for visualizer allocation."""


class GPUResourceScheduler:
    """Manages SR-IOV VF allocation across concurrent visualizer sessions.

    All methods are synchronous (asyncio single-threaded event loop).
    """

    def __init__(self, max_visualizer_vfs: int = MAX_VISUALIZER_VFS) -> None:
        self._max_vfs = max_visualizer_vfs
        self._allocations: dict[int, VFAllocation] = {}

    @property
    def active_sessions(self) -> int:
        """Number of currently allocated VF slots."""
        return len(self._allocations)

    @property
    def available_vfs(self) -> int:
        """Number of VF slots still available for allocation."""
        return self._max_vfs - len(self._allocations)

    def allocate(self, guild_id: int, engine_type: str) -> VFAllocation:
        """Allocate a VF slot for a guild's visualizer session.

        If the guild already has an allocation, the old one is released first
        (re-allocation for engine change).

        Args:
            guild_id: The Discord guild requesting a VF.
            engine_type: The visualizer engine type (e.g., "projectm", "varda").

        Returns:
            The new VFAllocation.

        Raises:
            GPUCapacityExceededError: If all visualizer VF slots are occupied.
        """
        if guild_id in self._allocations:
            self.release(guild_id)

        if len(self._allocations) >= self._max_vfs:
            raise GPUCapacityExceededError(
                f"All {self._max_vfs} visualizer VF slots occupied"
            )

        alloc = VFAllocation(guild_id=guild_id, engine_type=engine_type)
        self._allocations[guild_id] = alloc
        log.info(
            "GPU VF allocated: guild=%d engine=%s (%d/%d in use)",
            guild_id,
            engine_type,
            len(self._allocations),
            self._max_vfs,
        )
        return alloc

    def release(self, guild_id: int) -> None:
        """Release the VF slot for a guild. No-op if not allocated."""
        alloc = self._allocations.pop(guild_id, None)
        if alloc:
            log.info(
                "GPU VF released: guild=%d engine=%s", guild_id, alloc.engine_type
            )

    def is_allocated(self, guild_id: int) -> bool:
        """Check whether a guild currently holds a VF allocation."""
        return guild_id in self._allocations
