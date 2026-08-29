"""Entrypoint for the ``spotify-stream`` sidecar (multi-tenant).

Builds runtime settings from the environment, wires the per-user streaming
router (unified-store resolver + guild→owner lookup + bounded per-``sub`` session
pool + per-``(sub,track)`` audio cache), the librespot capture service (sidecar
side of the one-time capture contract, task 2.2), and serves the aiohttp app. Run
as an independently deployable container (R9.1)::

    python -m spotify_stream

The single global session is gone: each request resolves its own guild owner's
Spotify credential from the unified credential store, read-only, and streams from
that user's librespot session — no shared-account fallback (R3.1/R3.6/R10.5).

Requirements: 3.1, 3.6, 9.1, 10.5
"""

from __future__ import annotations

import logging
import os

from aiohttp import web

from .config import SpotifyStreamSettings
from .librespot_capture import LibrespotCaptureService, LibrespotOAuthBackend
from .resolver_bootstrap import build_user_credential_resolver
from .server import build_app
from .session_pool import PerUserTrackCache, SpotifyStreamRouter


def _cache_dir_for(settings: SpotifyStreamSettings):
    """Return a ``sub -> per-user cache dir`` function under ``DATA_DIR/<sub>``.

    Each user's librespot credential cache lives in its OWN subdirectory so a
    restart never mixes users' credentials (R9.3). The ``sub`` is used only as a
    filesystem path component (never logged).
    """

    def _dir(sub: str) -> str:
        return os.path.join(settings.data_dir, sub)

    return _dir


def create_router(settings: SpotifyStreamSettings) -> SpotifyStreamRouter | None:
    """Build the per-user streaming router, or ``None`` if the store is absent.

    Wires the unified-store resolver + guild→owner lookup (R1.1), the bounded
    per-``sub`` session pool (R8), and the per-``(sub,track)`` audio cache (R6.2,
    R8.3). Returns ``None`` when the unified credential store cannot be reached,
    so the sidecar starts observably not-ready rather than serving a single
    ambient account (R7.5, R10.5).
    """
    wired = build_user_credential_resolver(settings)
    if wired is None:
        return None
    resolver, owner_lookup = wired

    from hellodj_platform_logic.session_registry import SessionRegistry
    from hellodj_platform_logic.types import SessionRegistryConfig

    registry = SessionRegistry(
        SessionRegistryConfig(
            max_sessions=settings.max_sessions,
            idle_timeout_seconds=settings.session_idle_timeout_seconds,
        ),
    )
    cache = PerUserTrackCache(
        max_entries=settings.track_cache_max,
        ttl_seconds=settings.track_cache_ttl_seconds,
    )
    return SpotifyStreamRouter(
        owner_lookup,
        resolver,
        registry,
        cache,
        cache_dir_for=_cache_dir_for(settings),
    )


def create_capture(settings: SpotifyStreamSettings) -> LibrespotCaptureService | None:
    """Build the librespot capture service (sidecar side of task 2.2), or ``None``.

    Uses the real :class:`LibrespotOAuthBackend`; returns ``None`` only if the
    native ``librespot`` package is unavailable (the capture routes are then not
    registered). A capture failure at runtime is surfaced observably.
    """
    try:
        backend = LibrespotOAuthBackend(_cache_dir_for(settings))
        return LibrespotCaptureService(backend)
    except Exception as exc:  # noqa: BLE001 - non-fatal: capture routes omitted
        logging.getLogger(__name__).warning(
            "spotify-stream: librespot capture unavailable (%s)", exc
        )
        return None


def main() -> None:
    """Configure logging, wire the per-user router + capture, and serve."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = SpotifyStreamSettings.from_env()
    router = create_router(settings)
    capture = create_capture(settings)
    app = build_app(router, capture=capture)
    logging.getLogger(__name__).info(
        "spotify-stream listening on %s:%s (table=%s, multi_tenant=%s, capture=%s)",
        settings.host,
        settings.port,
        settings.core_table,
        router is not None,
        capture is not None,
    )
    web.run_app(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
