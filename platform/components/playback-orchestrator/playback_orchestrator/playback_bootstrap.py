"""Env-driven bootstrap for the orchestrator's playback HTTP API (degraded-safe).

Builds the :class:`~playback_orchestrator.playback_api.PlaybackService` (router +
single-writer session store over ``hellodj-session``) from the environment, or
returns ``None`` when the session table isn't configured / boto3 is unavailable.
A ``None`` service makes the HTTP layer return a clean "not configured" body
rather than a 404, so the bot reply is truthful.

Content filtering and user bans are left at the router's ``None`` defaults for
now (no per-guild rules loaded here); the router handles both being absent
cleanly. Loading per-guild filter/ban rules from ``hellodj-core`` can layer on
later without changing this seam.

Env:

* ``HELLODJ_SESSION_TABLE``  DynamoDB table name (defaults to ``hellodj-session``).
* ``AWS_REGION``             Region for the boto3 resource.

Mirrors ``watchdog_bootstrap`` / ``instance_bootstrap``: no boto3 import at
module load, degrade to ``None`` on any failure.
"""

from __future__ import annotations

import logging
import os

from .persistence import SessionStore
from .playback_api import PlaybackService
from .router import PlaybackRouter

_LOG = logging.getLogger("playback_orchestrator.playback_bootstrap")

#: Default session table name (mirrors data_access.SESSION_TABLE_NAME).
_DEFAULT_SESSION_TABLE = "hellodj-session"

__all__ = ["build_playback_service"]


def build_playback_service() -> PlaybackService | None:
    """Build the playback service from env, or ``None`` (degraded).

    Returns ``None`` when boto3 / the shared data-access layer can't be built
    (e.g. a test env or missing credentials), so the caller serves a clean
    "not configured" response instead of crashing or 404ing.
    """
    table_name = (
        os.getenv("HELLODJ_SESSION_TABLE", "").strip() or _DEFAULT_SESSION_TABLE
    )
    try:
        import boto3
        from hellodj_platform_logic.data_access import SessionTable

        ddb = boto3.resource(
            "dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1")
        )
        store = SessionStore(SessionTable(ddb.Table(table_name)))
    except Exception:  # noqa: BLE001 - degrade: no playback service
        _LOG.info(
            "degraded: playback API disabled (no session table / boto3)"
        )
        return None
    router = PlaybackRouter(store)
    return PlaybackService(router, store)
