"""Module entry point for the ``discord-bot-core`` component.

The container image runs ``python -m discord_bot_core`` (see the flake's
``Entrypoint``). ``python -m <package>`` executes the package's ``__main__``
submodule, so this thin shim delegates to :func:`discord_bot_core.main.main`,
which configures logging and runs the gateway event loop until shutdown.

Without this module the interpreter fails at startup with
``No module named discord_bot_core.__main__; 'discord_bot_core' is a package
and cannot be directly executed`` and the pod crash-loops.
"""

from __future__ import annotations

from .main import main

if __name__ == "__main__":
    main()
