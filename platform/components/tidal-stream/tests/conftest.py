"""Shared test fixtures/helpers for tidal-stream tests.

Ensures the monorepo ``components/`` directory is importable so the shared
``hellodj_platform_logic`` package resolves without an editable install.
"""

from __future__ import annotations

import sys
from pathlib import Path

# components/tidal-stream/tests -> components/
_COMPONENTS_DIR = Path(__file__).resolve().parents[2]
if str(_COMPONENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPONENTS_DIR))

# components/tidal-stream (so `import tidal_stream` works in isolation)
_COMPONENT_DIR = Path(__file__).resolve().parents[1]
if str(_COMPONENT_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPONENT_DIR))
