"""Pytest bootstrap for the playback test suite.

The tests in this directory use bare imports (e.g. ``from guild_credentials
import ...``) that rely on the ``bot/playback`` directory itself being on
``sys.path``. When pytest is invoked via the ``pytest`` console script (rather
than ``python3 -m pytest``), the current working directory is NOT automatically
placed on ``sys.path``, so those bare imports fail at collection time.

pytest imports the nearest ``conftest.py`` before collecting test modules, so
inserting this directory here guarantees the bare imports resolve regardless of
how pytest is launched (``pytest`` or ``python3 -m pytest``).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
