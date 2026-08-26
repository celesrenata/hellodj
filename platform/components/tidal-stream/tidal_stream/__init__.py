"""HelloDJ ``tidal-stream`` component.

Direct Tidal audio streaming sidecar rebuilt for the AWS re-platform. It
authenticates Tidal source access through the HelloDJ-owned **first-party
single-app-id** OAuth integration (Requirements 9.1-9.5), refreshing tokens via
the shared :mod:`hellodj_platform_logic.tidal_refresh` decision logic and
persisting the refresh token in AWS Secrets Manager.

The legacy two-client-id key-split approach is removed: the token manager routes
every refresh through :func:`hellodj_platform_logic.tidal_refresh.refresh_tidal`,
whose guard rejects the legacy mode outright (R9.3).

The component is packaged as an independently deployable unit (R15.1) exposing:

    * direct Tidal streaming endpoints (search / stream-url resolution), and
    * the HelloDJ-owned OAuth callback endpoint ``/auth/callback`` that the
      web-ui forwards the authorization code to (R9.2).
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
