"""Skeleton smoke test: the shared logic package is importable (task 1.1)."""

from __future__ import annotations


def test_package_imports() -> None:
    """The shared pure-logic package imports cleanly and exposes a version."""
    import hellodj_platform_logic as logic

    assert isinstance(logic.__version__, str)
    assert logic.__version__


def test_all_is_defined() -> None:
    """The package declares a public ``__all__`` for later modules to extend."""
    import hellodj_platform_logic as logic

    assert isinstance(logic.__all__, list)
