"""Shared test fixtures/helpers for migration component tests.

Ensures the monorepo ``components/`` directory is importable so the shared
``hellodj_platform_logic`` package resolves, and the migration component
directory is importable so ``import migration_job`` works in isolation, both
without an editable install.
"""

from __future__ import annotations

import sys
from pathlib import Path

# components/migration/tests -> components/
_COMPONENTS_DIR = Path(__file__).resolve().parents[2]
if str(_COMPONENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPONENTS_DIR))

# components/migration (so `import migration_job` works in isolation)
_COMPONENT_DIR = Path(__file__).resolve().parents[1]
if str(_COMPONENT_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPONENT_DIR))
