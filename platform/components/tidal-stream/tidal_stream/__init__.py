"""HelloDJ ``tidal-stream`` component (multi-tenant).

Direct Tidal audio streaming sidecar for the AWS platform. It is **multi-tenant**
(multi-tenant-source-streaming R5): every request carries a ``guild_id``, the
owning user's Cognito ``sub`` is resolved server-side, and the request is served
from that user's own Tidal token — resolved read-only from the unified per-user
credential store (``hellodj-core`` + KMS Decrypt-only) via the shared
:class:`hellodj_platform_logic.user_credential_resolver.UserCredentialResolver`.
The single startup-bound account is gone; there is no cross-user fallback (R5.4,
R10.5).

Per-user sessions live in a :data:`~tidal_stream.user_sessions.TidalSessionRegistry`
(the shared bounded-LRU :class:`hellodj_platform_logic.session_registry.SessionRegistry`
keyed by ``sub``). The ``TIDAL_APP_ID`` / ``TIDAL_CALLBACK_URL`` stay global
single-app-id config; only the per-user token varies, and the durable watchdog
owns Tidal refresh (the sidecar is read-only — R5.3).

The component is packaged as an independently deployable unit (R15.1) exposing:

    * per-user Tidal streaming endpoints
      (``/search/{guild_id}`` / ``/stream/{guild_id}/{track_id}``), and
    * an OPTIONAL legacy first-party OAuth callback ``/auth/callback`` (present
      only when a refresh secret is configured) that the web-ui forwards the
      authorization code to (R9.2) — not part of the per-user streaming path.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
