"""Per-guild audio-visualizer control registry (Requirement 6.2).

Tracks which visualizer engine is active per guild and whether its HLS stream is
ready, so late-joining Activity clients can be told the current visualizer state
(engine + CloudFront playlist URL). Engine switching is delegated to an injected
async switcher (the transcode/visualizer pipeline lives in the ``hls-transcode``
component), keeping this registry pure state with no runtime dependencies.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .models import VisualizerState

__all__ = ["VisualizerRegistry", "EngineSwitcher"]

#: Async callable(guild_id, engine) that performs the actual engine hot-swap in
#: the transcode/visualizer pipeline.
EngineSwitcher = Callable[[int, str], Awaitable[None]]


class VisualizerRegistry:
    """Holds visualizer control state per guild.

    The registry is the source of truth for the *control plane* (which engine is
    selected and whether HLS is ready). The *data plane* (GPU rendering + HLS
    encode) is owned by the transcode component; this registry only records what
    the clients should be shown and delegates switches to the injected switcher.
    """

    def __init__(self, switcher: EngineSwitcher | None = None) -> None:
        """Initialise with an optional async engine switcher."""
        self._states: dict[int, VisualizerState] = {}
        self._switcher = switcher

    def get(self, guild_id: int) -> VisualizerState | None:
        """Return the current visualizer state for a guild, if any."""
        return self._states.get(guild_id)

    def state_message(self, guild_id: int) -> dict | None:
        """Return the late-joiner ``visualizer`` message, or ``None``.

        ``None`` is returned when no engine is selected (or it is ``"off"``), so
        the caller can fall back to a default (e.g. DVD screensaver).
        """
        state = self._states.get(guild_id)
        if state is None or state.engine == "off" or not state.active:
            return None
        return state.to_message()

    def set_hls_ready(
        self, guild_id: int, playlist_url: str | None
    ) -> VisualizerState:
        """Mark the guild's visualizer HLS as ready with a playlist URL."""
        state = self._states.setdefault(guild_id, VisualizerState())
        state.hls_ready = True
        state.active = True
        state.playlist_url = playlist_url
        return state

    def clear(self, guild_id: int) -> None:
        """Remove visualizer state for a guild (session ended)."""
        self._states.pop(guild_id, None)

    async def set_engine(
        self, guild_id: int, engine: str, config: dict | None = None
    ) -> VisualizerState:
        """Select ``engine`` for a guild and delegate the hot-swap.

        The registry state is updated immediately (so late joiners see the
        selection) and, if a switcher was injected, the actual engine swap is
        awaited. Selecting ``"off"`` clears the active/HLS flags.
        """
        state = self._states.setdefault(guild_id, VisualizerState())
        state.engine = engine
        state.config = dict(config or {})
        if engine == "off":
            state.active = False
            state.hls_ready = False
            state.playlist_url = None
        else:
            state.active = True
            # HLS becomes ready only once the transcode pipeline reports it.
            state.hls_ready = False
            state.playlist_url = None
        if self._switcher is not None:
            await self._switcher(guild_id, engine)
        return state
