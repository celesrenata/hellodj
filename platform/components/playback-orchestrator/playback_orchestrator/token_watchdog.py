"""Durable token-refresh watchdog hosted in the playback-orchestrator.

The watchdog is the durable half of the unified-oauth-and-token-watchdog
feature. It keeps every stored source credential alive **independently of the
bot**: it runs as a background loop inside the standing ``playback-orchestrator``
container (alongside the health server), so refresh survives a bot pod bounce
(Requirement 5).

Design (design.md "Watchdog loop (in playback-orchestrator)"):

    * :class:`TokenWatchdog` consumes the shared
      :class:`~source_credential_service.SourceCredentialService` (the exact
      same store the web-ui writes to) and a ``{provider: RefreshClient}`` map.
    * :meth:`TokenWatchdog.tick` runs one pass: it asks the service for the
      near-expiry credentials (a key-projected scan that never decrypts —
      R5.2), then for each item picks the matching provider refresh client,
      loads the current token, applies the unified
      :func:`~hellodj_platform_logic.source_refresh.apply_refresh`, and writes
      the outcome back via
      :meth:`~source_credential_service.SourceCredentialService.record_refresh`
      (success → new encrypted blob + ``refresh_status=ok`` — R5.3).
    * **Per-item isolation** — each credential is refreshed independently. One
      item's failure (refresh error, missing client, decrypt failure, optimistic
      -lock conflict) is logged, recorded on that item as
      ``refresh_status=failed`` with a short reason (never token material), and
      the pass continues. One item can never stop the pass or crash the loop
      (R5.4).
    * :meth:`TokenWatchdog.run_forever` sleeps ``interval`` between ticks and
      catches any loop-level exception so the container never dies (R5.4, R5.7).
    * **Multi-replica safe** — ``record_refresh`` is an optimistic-lock
      read-modify-write, so two watchdog replicas racing on the same item cannot
      corrupt it; a losing writer re-reads and re-applies (R5.5).

Degraded mode (R5.7): when no datastore / KMS / refresh clients are configured,
:func:`build_watchdog` returns ``None`` and :func:`start_watchdog_thread` logs
"degraded: watchdog disabled" and starts nothing. The health server is
unaffected.

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

from hellodj_platform_logic.source_refresh import (
    RefreshClient,
    TokenState,
    apply_refresh,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from source_credential_service import SourceCredentialService


def _preserve_prior_extra(prior: TokenState, refreshed: TokenState) -> TokenState:
    """Carry forward prior ``extra`` keys the refresh response did not set.

    A provider refresh (``apply_refresh`` → ``RefreshClient.refresh``) builds a
    FRESH :class:`TokenState` whose ``extra`` holds only the leftover fields
    from that provider's token response. It therefore drops any
    provider-independent material the connect flow previously stored under
    ``extra`` — most importantly the Spotify ``librespot_credentials`` reusable
    blob (multi-tenant-source-streaming task 2.2), which is long-lived and NOT
    OAuth-refreshable. This merges the prior ``extra`` back in with the refreshed
    values winning on any key collision, so the librespot blob (and any other
    prior extra) survives a Spotify OAuth refresh and the ``spotify-stream``
    reader keeps working (writer/watchdog/reader move together — R10.3). Token
    material is never logged.
    """
    prior_extra = dict(prior.extra)
    if not prior_extra:
        return refreshed
    merged = dict(prior_extra)
    merged.update(dict(refreshed.extra))
    return replace(refreshed, extra=merged)

_LOG = logging.getLogger("playback_orchestrator.token_watchdog")

__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_THRESHOLD_SECONDS",
    "TokenWatchdog",
]

#: Default seconds between watchdog ticks (design default: every 5 minutes).
DEFAULT_INTERVAL_SECONDS = 300.0

#: Default "near expiry" window (seconds). A credential whose access token
#: expires within this window of ``now`` is refreshed on the next tick. Kept a
#: little wider than the interval so a token is refreshed before it lapses even
#: if a single tick is skipped.
DEFAULT_THRESHOLD_SECONDS = 600.0


class TokenWatchdog:
    """Refresh near-expiry source credentials on a durable background loop.

    The watchdog owns no token material of its own: it reads the plaintext
    ``expires_at`` status to decide *what* to refresh (no decrypt), and only
    decrypts a blob (via the service) for an item it is actually refreshing.

    Args:
        service: The shared credential store (same one the web-ui writes).
        clients_by_provider: Map of provider id → :class:`RefreshClient`. A
            provider with no client (for example ``discord``, which is identity
            -only) is simply skipped when enumerated.
        interval: Seconds to sleep between ticks in :meth:`run_forever`.
        threshold: "Near expiry" window in seconds passed to
            :meth:`~source_credential_service.SourceCredentialService.iter_near_expiry`.
        clock: Injectable epoch-seconds clock (for tests).
        sleep: Injectable sleep function (for tests / clean shutdown).
    """

    def __init__(
        self,
        service: SourceCredentialService,
        clients_by_provider: Mapping[str, RefreshClient],
        *,
        interval: float = DEFAULT_INTERVAL_SECONDS,
        threshold: float = DEFAULT_THRESHOLD_SECONDS,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._service = service
        self._clients = dict(clients_by_provider)
        self._interval = interval
        self._threshold = threshold
        self._clock = clock
        self._sleep = sleep
        self._stop = threading.Event()

    def tick(self) -> int:
        """Run one refresh pass over the near-expiry credentials (R5.2-R5.4).

        Enumerates near-expiry credentials via the key-projected scan (no
        decrypt during enumeration) and refreshes each independently. Returns
        the number of credentials refreshed successfully on this pass.

        This method never raises: a failure enumerating the store, or refreshing
        any single item, is logged and (for a single item) recorded as
        ``refresh_status=failed`` — the pass still completes so one bad
        credential can never stall the rest (R5.4).
        """
        now = float(self._clock())
        refreshed = 0
        try:
            near = list(self._service.iter_near_expiry(now, self._threshold))
        except Exception:  # noqa: BLE001 - never let enumeration crash the loop
            _LOG.exception("token watchdog: enumeration failed; skipping tick")
            return 0

        for cred in near:
            if self._refresh_one(cred.sub, cred.provider, now):
                refreshed += 1
        return refreshed

    def _refresh_one(self, sub: str, provider: str, now: float) -> bool:
        """Refresh a single credential, isolating any failure (R5.4).

        Returns ``True`` on a successful refresh+write-back, ``False`` otherwise.
        Any exception is caught, logged without token material, and recorded on
        the item as ``refresh_status=failed`` so the next tick can retry.
        """
        client = self._clients.get(provider)
        if client is None:
            # No refresh grant for this provider (e.g. discord identity-only, or
            # a provider not configured in this deployment). Nothing to do.
            _LOG.debug(
                "token watchdog: no refresh client for provider %s; skipping",
                provider,
            )
            return False

        try:
            state = self._service.load_token(sub, provider)
            if state is None:
                _LOG.warning(
                    "token watchdog: credential vanished before refresh "
                    "(provider=%s); skipping",
                    provider,
                )
                return False
            new_state = apply_refresh(state, client, now, force=True)
            new_state = _preserve_prior_extra(state, new_state)
            self._service.record_refresh(sub, provider, new_state=new_state)
            _LOG.info(
                "token watchdog: refreshed credential (provider=%s)", provider
            )
            return True
        except Exception as exc:  # noqa: BLE001 - per-item isolation (R5.4)
            reason = f"{type(exc).__name__}: {exc}"
            _LOG.warning(
                "token watchdog: refresh failed (provider=%s): %s",
                provider,
                type(exc).__name__,
            )
            # Best-effort failure record; the prior blob stays intact (R5.4).
            # A failure to even record must not stop the pass either.
            try:
                self._service.record_refresh(sub, provider, error=reason)
            except Exception:  # noqa: BLE001
                _LOG.exception(
                    "token watchdog: could not record refresh failure "
                    "(provider=%s)",
                    provider,
                )
            return False

    def stop(self) -> None:
        """Signal :meth:`run_forever` to exit after the current sleep."""
        self._stop.set()

    def run_forever(self) -> None:
        """Tick forever, sleeping ``interval`` between passes (R5.4, R5.7).

        Any loop-level exception is caught and logged so the loop (and therefore
        the container) never dies. Exits cleanly when :meth:`stop` is called.
        """
        _LOG.info(
            "token watchdog started: interval=%ss threshold=%ss providers=%s",
            self._interval,
            self._threshold,
            sorted(self._clients),
        )
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the loop must never die (R5.7)
                _LOG.exception("token watchdog: tick raised; continuing")
            self._sleep(self._interval)
        _LOG.info("token watchdog stopped")
