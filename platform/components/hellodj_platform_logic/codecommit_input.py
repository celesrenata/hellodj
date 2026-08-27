"""Input-form classification and CodeCommit input resolution for the pin gate.

This module holds the pure decision logic the amended pin gate
(``platform/tools/gate_pins.py``) invokes when it loads each ``pins.toml``
entry. The source of truth for the HelloDJ platform moves off public GitHub
into private Amazon CodeCommit, so flake inputs move from
``github:hellodj/<repo>/<branch>`` to
``git+https://git-codecommit.<region>.amazonaws.com/v1/repos/<repo>?ref=<branch>``
(R2.1/R3.1). The pin gate must accept the new CodeCommit form, still reject
``path:`` inputs, and still name a CodeCommit entry that is missing a required
field (R3.2/R3.3/R3.4).

Both functions here are pure — they perform no live network, git, or filesystem
calls — so they are importable by the pin gate and the CDK layer alike and are
exercised directly by the input-form classification property (Property 2).

Implemented here:

* :func:`classify_input` — Property 2 / R3.2, R3.3, R3.4. Classifies a
  ``pins.toml`` entry (a mapping of field name to value) into one of the four
  :class:`~hellodj_platform_logic.types.InputForm` members:

  - :attr:`~hellodj_platform_logic.types.InputForm.PATH` whenever any field
    declares a ``path:`` input or a ``path:``-style reference — a bare field
    value containing ``":"``, a value starting with ``path``, or ``type ==
    "path"`` — which the gate rejects (R3.3);
  - :attr:`~hellodj_platform_logic.types.InputForm.CODECOMMIT` when ``type ==
    "codecommit"`` and its ``region``, ``repo``, and ``branch`` are all present
    and non-empty (R3.2);
  - :attr:`~hellodj_platform_logic.types.InputForm.INVALID` when a CodeCommit
    entry is missing its ``region``, ``repo``, or ``branch`` (R3.4);
  - :attr:`~hellodj_platform_logic.types.InputForm.GITHUB` otherwise, for a
    well-formed legacy github entry.

* :func:`resolve_codecommit_input` — R3.1/R2.1. Resolves a CodeCommit input's
  ``region``, ``repo``, and ``branch`` to its canonical flake-input string
  ``git+https://git-codecommit.<region>.amazonaws.com/v1/repos/<repo>?ref=<branch>``.

Design references:
    * Components §3 — Amend the pin gate to accept CodeCommit inputs: schema
      extension, ``classify_input`` / ``resolve_codecommit_input``, still reject
      ``path:`` (R3.3), missing-field validation for CodeCommit entries (R3.4).
    * Correctness Property 2: Input-form classification accepts CodeCommit,
      rejects path, flags missing fields.
    * Error Handling: ``path:`` in any field -> reject (R3.3); CodeCommit entry
      missing region/repo/branch -> reject naming the missing field (R3.4).

Requirements: 2.1, 3.1, 3.2, 3.3, 3.4
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hellodj_platform_logic.types import InputForm

__all__ = [
    "classify_input",
    "resolve_codecommit_input",
    "missing_codecommit_fields",
]

#: The CodeCommit fields a ``type = "codecommit"`` entry must declare, in the
#: order they are reported when missing (R3.4).
_CODECOMMIT_REQUIRED_FIELDS = ("region", "repo", "branch")


def _is_path_style(value: Any) -> bool:
    """Whether a single field value is a ``path:`` input or ``path:``-style ref.

    A value is treated as a ``path:``-style reference (R3.3) when, as a string,
    it either starts with ``path`` (e.g. ``path:./x`` or a bare ``path``) or
    contains a ``":"`` (e.g. ``path:/abs`` or any ``scheme:``-style bare field
    that is not one of the structured CodeCommit/github fields). Non-string
    values are never path-style.
    """
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped:
        return False
    if stripped.startswith("path"):
        return True
    if ":" in stripped:
        return True
    return False


def _entry_has_path_reference(entry: Mapping[str, Any]) -> bool:
    """Whether any field of ``entry`` declares a ``path:`` input/reference (R3.3).

    Returns ``True`` when the entry's ``type`` is ``"path"`` or when any field
    value is a ``path:``-style reference. The ``type`` discriminator itself is
    exempt from the bare ``":"`` heuristic (its allowed values ``github`` /
    ``codecommit`` never contain ``":"``, and ``path`` is caught explicitly).
    """
    type_value = entry.get("type")
    if isinstance(type_value, str) and type_value.strip() == "path":
        return True

    for key, value in entry.items():
        if key == "type":
            # The discriminator's legitimate values do not contain ":"; a
            # literal "path" type is already handled above.
            continue
        if _is_path_style(value):
            return True
    return False


def _nonempty_str(value: Any) -> bool:
    """Whether ``value`` is a present, non-empty string."""
    return isinstance(value, str) and value.strip() != ""


def missing_codecommit_fields(entry: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the CodeCommit fields absent or empty on ``entry`` (R3.4).

    Reports ``region``, ``repo``, and/or ``branch`` (in that fixed order) when
    they are missing or empty. Used by :func:`classify_input` to flag an
    :class:`~hellodj_platform_logic.types.InputForm.INVALID` CodeCommit entry
    and by the pin gate to name the missing field in its rejection message.
    """
    return tuple(
        f for f in _CODECOMMIT_REQUIRED_FIELDS if not _nonempty_str(entry.get(f))
    )


def classify_input(entry: Mapping[str, Any]) -> InputForm:
    """Classify a ``pins.toml`` entry into its input form (Property 2).

    Implements R3.2/R3.3/R3.4. The classification is decided purely from the
    entry's fields, in this precedence order:

    1. **PATH (R3.3).** If ``type == "path"`` or any field declares a
       ``path:`` input or a ``path:``-style reference (a value starting with
       ``path`` or containing ``":"``), the entry is a
       :attr:`~hellodj_platform_logic.types.InputForm.PATH` input. The
       ``path:`` guard is checked first so a ``path:``-style value in any field
       is always rejected, regardless of the declared ``type``.
    2. **CODECOMMIT (R3.2).** Otherwise, if ``type == "codecommit"`` and its
       ``region``, ``repo``, and ``branch`` are all present and non-empty, the
       entry is a valid
       :attr:`~hellodj_platform_logic.types.InputForm.CODECOMMIT` input.
    3. **INVALID (R3.4).** Otherwise, if ``type == "codecommit"`` but any of
       ``region``, ``repo``, or ``branch`` is missing or empty, the entry is
       :attr:`~hellodj_platform_logic.types.InputForm.INVALID`. The specific
       missing field(s) are recoverable via :func:`missing_codecommit_fields`
       so the gate can name them (R3.4).
    4. **GITHUB.** Otherwise the entry is a well-formed legacy
       :attr:`~hellodj_platform_logic.types.InputForm.GITHUB` input (``type``
       absent or ``"github"``).

    Args:
        entry: A ``pins.toml`` entry as a mapping of field name to value (e.g.
            ``{"type": "codecommit", "region": "us-east-1", "repo": "Lavalink",
            "branch": "dev", "pinned_identifier": "..."}``).

    Returns:
        The :class:`~hellodj_platform_logic.types.InputForm` the entry declares.

    Requirements: 3.2, 3.3, 3.4
    """
    # (1) A path: input or path:-style reference in any field is always rejected
    # (R3.3), taking precedence over every other form.
    if _entry_has_path_reference(entry):
        return InputForm.PATH

    type_value = entry.get("type")
    normalized_type = type_value.strip() if isinstance(type_value, str) else type_value

    if normalized_type == "codecommit":
        # (2)/(3) A codecommit entry is valid only when region/repo/branch are
        # all present and non-empty; otherwise it is INVALID (R3.2/R3.4).
        if missing_codecommit_fields(entry):
            return InputForm.INVALID
        return InputForm.CODECOMMIT

    # (4) Everything else is a well-formed legacy github entry (type absent or
    # "github").
    return InputForm.GITHUB


def resolve_codecommit_input(region: str, repo: str, branch: str) -> str:
    """Resolve a CodeCommit input to its canonical flake-input string (R3.1).

    Returns
    ``git+https://git-codecommit.<region>.amazonaws.com/v1/repos/<repo>?ref=<branch>``,
    the ``git+https`` form the generic Nix fetcher resolves over IAM auth
    (R2.1). This is a pure string construction; the caller is responsible for
    having validated that ``region``, ``repo``, and ``branch`` are present
    (see :func:`classify_input`).

    Args:
        region: The AWS region hosting the CodeCommit repository (e.g.
            ``"us-east-1"``).
        repo: The CodeCommit repository name (e.g. ``"Lavalink"``).
        branch: The ``ref`` branch to fetch (e.g. ``"dev"``).

    Returns:
        The canonical CodeCommit ``git+https`` flake-input string.

    Requirements: 2.1, 3.1
    """
    return (
        f"git+https://git-codecommit.{region}.amazonaws.com"
        f"/v1/repos/{repo}?ref={branch}"
    )
