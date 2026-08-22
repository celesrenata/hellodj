"""Root conftest — ensures bot/ is importable for all tests."""

import sys
from pathlib import Path

# Add bot/ directory to sys.path so tests can import bot modules directly
# (e.g., `from playback.classifier import ...` instead of `from bot.playback.classifier import ...`)
_bot_dir = str(Path(__file__).resolve().parent / "bot")
if _bot_dir not in sys.path:
    sys.path.insert(0, _bot_dir)
