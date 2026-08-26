"""Shared test fixtures/path setup for the web-ui component tests.

Ensures both the monorepo ``components/`` directory (so the shared
``hellodj_platform_logic`` package resolves) and the web-ui component directory
(so its top-level modules ``app``/``auth``/``pages``/``config_store``/
``secrets_store`` import) are on ``sys.path`` without an editable install.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# components/web-ui/tests -> components/
_COMPONENTS_DIR = Path(__file__).resolve().parents[2]
if str(_COMPONENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPONENTS_DIR))

# components/web-ui (so `import app`, `import auth`, ... work in isolation)
_COMPONENT_DIR = Path(__file__).resolve().parents[1]
if str(_COMPONENT_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPONENT_DIR))


@pytest.fixture()
def app():
    """A create_app() instance in degraded (no-datastore) mode for testing."""
    from app import create_app

    application = create_app(
        overrides={
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            # Deterministic config so template snapshots are stable.
            "HELLODJ_STAGE": "beta",
        }
    )
    return application


@pytest.fixture()
def client(app):
    """A Flask test client for the degraded-mode app."""
    return app.test_client()
