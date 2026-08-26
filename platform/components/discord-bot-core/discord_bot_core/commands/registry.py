"""Cog and command registration scaffolding.

The registry decouples *which* cogs exist from *how* they are attached to the
gateway client, so cogs can be added/removed without touching the bootstrap.
Cogs are supplied as factories (callables that build a cog given the shared
dependencies), keeping registration lazy and testable without importing
discord.py at module load time.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from typing import Any, Protocol

log = logging.getLogger(__name__)

__all__ = ["CogFactory", "CommandRegistry"]


class CogDependencies(Protocol):
    """Shared dependencies passed to every cog factory.

    Concretely this is satisfied by the wiring container in
    :mod:`discord_bot_core.main` and exposes the playback client and guild
    policy the cogs need. Declared as a Protocol so cogs depend on the surface,
    not the concrete container.
    """

    @property
    def playback(self) -> Any:
        """The playback client cogs delegate playback to."""
        ...

    @property
    def guild_policy(self) -> Any:
        """The guild policy cogs consult before acting."""
        ...


class CogFactory(Protocol):
    """A callable that builds a cog instance from shared dependencies.

    The returned object is whatever ``Bot.add_cog`` accepts (a
    ``discord.ext.commands.Cog``); typed as ``Any`` here so this module never
    needs to import discord.py.
    """

    def __call__(self, deps: CogDependencies) -> Any:
        """Build and return a cog instance."""
        ...


class _AddCogClient(Protocol):
    """Minimal structural type for the client the registry attaches cogs to."""

    async def add_cog(self, cog: Any) -> Awaitable[None] | None:
        """Attach a cog to the client."""
        ...


class CommandRegistry:
    """Collects cog factories and attaches the built cogs to a client."""

    def __init__(self) -> None:
        self._factories: list[CogFactory] = []

    def register(self, factory: CogFactory) -> None:
        """Register a cog factory to be built at attach time."""
        self._factories.append(factory)

    @property
    def count(self) -> int:
        """Number of registered cog factories."""
        return len(self._factories)

    async def attach_all(
        self, client: _AddCogClient, deps: CogDependencies
    ) -> int:
        """Build every registered cog and attach it to ``client``.

        Args:
            client: The gateway client exposing an async ``add_cog``.
            deps: Shared dependencies handed to each cog factory.

        Returns:
            The number of cogs successfully attached.
        """
        attached = 0
        for factory in self._factories:
            cog = factory(deps)
            result = client.add_cog(cog)
            if result is not None:
                await result
            attached += 1
            log.info("registered cog %s", type(cog).__name__)
        return attached
