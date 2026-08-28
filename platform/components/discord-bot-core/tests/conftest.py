"""Shared test path setup for the discord-bot-core component tests.

Ensures both the monorepo ``components/`` directory (so the shared
``hellodj_platform_logic`` package resolves without an editable install — the
pipeline copies it into the component source tree at build time, and locally it
lives one level up) and the component directory itself are on ``sys.path``.
Mirrors ``platform/components/web-ui/tests/conftest.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

# components/discord-bot-core/tests -> components/  (so hellodj_platform_logic
# resolves as a top-level package).
_COMPONENTS_DIR = Path(__file__).resolve().parents[2]
if str(_COMPONENTS_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPONENTS_DIR))

# components/discord-bot-core (so `import discord_bot_core` works in isolation).
_COMPONENT_DIR = Path(__file__).resolve().parents[1]
if str(_COMPONENT_DIR) not in sys.path:
    sys.path.insert(0, str(_COMPONENT_DIR))
