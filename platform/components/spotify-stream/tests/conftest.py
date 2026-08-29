"""Shared test fixtures/helpers for spotify-stream tests.

Ensures the monorepo ``components/`` directory AND the shared
``hellodj_platform_logic`` package (which lives in the ``hellodj-cdk`` repo) are
importable so tests run without an editable install. The component itself is
made importable so ``import spotify_stream`` works in isolation.
"""

from __future__ import annotations

import sys
from pathlib import Path

# components/spotify-stream/tests -> components/
_COMPONENTS_DIR = Path(__file__).resolve().parents[2]
if str(_COMPONENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPONENTS_DIR))

# components/spotify-stream (so `import spotify_stream` works in isolation)
_COMPONENT_DIR = Path(__file__).resolve().parents[1]
if str(_COMPONENT_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPONENT_DIR))

# The shared hellodj_platform_logic package lives in the sibling hellodj-cdk
# repo (see the website-debug-context steering note). Add its `shared/` dir to
# the path so `import hellodj_platform_logic` resolves in a bare local venv.
# The pipeline vendors the package into the source tree for the Nix build.
for candidate in (
    _COMPONENTS_DIR / "hellodj_platform_logic",  # pipeline-vendored copy
    Path.home() / "sources" / "celesrenata" / "hellodj-cdk" / "shared",
):
    parent = candidate.parent if candidate.name == "hellodj_platform_logic" else candidate
    if (parent / "hellodj_platform_logic").is_dir() and str(parent) not in sys.path:
        sys.path.insert(0, str(parent))
