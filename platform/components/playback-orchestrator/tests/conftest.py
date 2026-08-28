"""Shared test path setup for the playback-orchestrator component tests.

Puts three directories on ``sys.path`` (without an editable install):

* the monorepo ``components/`` directory, so the shared
  ``hellodj_platform_logic`` package resolves;
* the component directory, so ``import playback_orchestrator`` works in
  isolation;
* the ``web-ui`` component directory, so the watchdog can import the shared
  ``source_credential_service`` module exactly as it does at runtime (the
  service is a dependency-light web-ui module the watchdog reuses — same
  identity/store spans web-ui, watchdog, and bot).
"""

from __future__ import annotations

import sys
from pathlib import Path

# components/playback-orchestrator/tests -> components/
_COMPONENTS_DIR = Path(__file__).resolve().parents[2]
if str(_COMPONENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPONENTS_DIR))

# components/playback-orchestrator (so `import playback_orchestrator` works)
_COMPONENT_DIR = Path(__file__).resolve().parents[1]
if str(_COMPONENT_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPONENT_DIR))

# components/web-ui (so the watchdog can import `source_credential_service`,
# the shared dependency-light credential store, in isolation)
_WEB_UI_DIR = _COMPONENTS_DIR / "web-ui"
if str(_WEB_UI_DIR) not in sys.path:
    sys.path.insert(0, str(_WEB_UI_DIR))
