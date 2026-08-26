"""Property + unit tests for input-form classification (tasks 2.2, 2.3).

Feature: hellodj-private-source-and-toolchain, Property 2

These tests exercise the two pure decision functions the amended pin gate
invokes when it loads each ``pins.toml`` entry
(:mod:`hellodj_platform_logic.codecommit_input`):

* :func:`classify_input` -- classifies a manifest entry into one of the four
  :class:`~hellodj_platform_logic.types.InputForm` members. The source of truth
  moves off public GitHub into private Amazon CodeCommit, so the gate must
  accept the new CodeCommit form, still reject ``path:`` inputs, and still name
  a CodeCommit entry missing a required field.

* :func:`resolve_codecommit_input` -- resolves a CodeCommit input's region,
  repo, and branch to its canonical ``git+https`` flake-input string.

Property 2 (task 2.2) -- input-form classification accepts CodeCommit, rejects
path, flags missing fields: over generated entries,

* a well-formed ``type = "codecommit"`` entry (region/repo/branch all present
  and non-empty) classifies as CODECOMMIT (R3.2);
* an entry declaring a ``path:`` input or a ``path:``-style reference in any
  field classifies as PATH (R3.3);
* a codecommit entry with a dropped required field classifies as INVALID and
  the missing field is recoverable so the gate can name it (R3.4);
* a well-formed legacy github entry classifies as GITHUB.

Validates: Requirements 3.2, 3.3, 3.4.

The ``resolve_codecommit_input`` shape (task 2.3) is covered by a unit test
asserting the returned string equals
``git+https://git-codecommit.<region>.amazonaws.com/v1/repos/<repo>?ref=<branch>``
for representative inputs. Validates: Requirements 2.1, 3.1.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from hellodj_platform_logic.codecommit_input import (
    classify_input,
    missing_codecommit_fields,
    resolve_codecommit_input,
)
from hellodj_platform_logic.types import InputForm

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Field text that is a plain, non-empty identifier and never a ``path:``-style
# reference: lowercase letters/digits/hyphens only, so it never starts with
# ``path`` and never contains ``":"``. This keeps generated CodeCommit/github
# entries well-formed unless a path field is deliberately injected.
_PLAIN = st.text(
    alphabet=st.characters(
        whitelist_categories=(),
        whitelist_characters="abcdefghijklmnoqrstuvwxyz0123456789-",
    ),
    min_size=1,
    max_size=10,
).filter(lambda s: not s.startswith("path"))

# AWS-region-shaped text (also plain, never path-style).
_REGION = st.sampled_from(
    ["us-east-1", "us-west-2", "eu-west-1", "ap-southeast-2", "eu-central-1"]
)

# The three required CodeCommit fields, in the order they are reported missing.
_CODECOMMIT_FIELDS = ("region", "repo", "branch")


@st.composite
def wellformed_codecommit(draw: st.DrawFn) -> dict[str, str]:
    """A well-formed ``type = "codecommit"`` entry (all fields present, R3.2)."""
    return {
        "type": "codecommit",
        "region": draw(_REGION),
        "repo": draw(_PLAIN),
        "branch": draw(_PLAIN),
        "pinned_identifier": draw(_PLAIN),
    }


@st.composite
def wellformed_github(draw: st.DrawFn) -> dict[str, str]:
    """A well-formed legacy github entry (type absent or "github")."""
    entry = {
        "owner": draw(_PLAIN),
        "repo": draw(_PLAIN),
        "branch": draw(_PLAIN),
        "pinned_identifier": draw(_PLAIN),
    }
    # Half the time state the discriminator explicitly as "github".
    if draw(st.booleans()):
        entry["type"] = "github"
    return entry


@st.composite
def codecommit_missing_field(draw: st.DrawFn) -> tuple[dict[str, str], str]:
    """A codecommit entry with exactly one required field dropped (R3.4).

    Returns the entry plus the name of the dropped field so the test can assert
    it is the one flagged missing.
    """
    entry = draw(wellformed_codecommit())
    dropped = draw(st.sampled_from(_CODECOMMIT_FIELDS))
    del entry[dropped]
    return entry, dropped


@st.composite
def path_style_entry(draw: st.DrawFn) -> dict[str, str]:
    """An entry with a ``path:`` input / ``path:``-style reference (R3.3).

    Either the ``type`` is literally ``"path"``, or a ``path:``-style value is
    injected into one field of an otherwise well-formed codecommit/github entry.
    """
    kind = draw(st.sampled_from(["type_path", "path_value", "colon_value"]))
    base = draw(st.one_of(wellformed_codecommit(), wellformed_github()))

    if kind == "type_path":
        base["type"] = "path"
        return base

    # A concrete path:-style value to inject.
    path_value = draw(
        st.sampled_from(
            [
                "path:./local",
                "path:/abs/path",
                "path",
                "file:./x",  # bare scheme: -> contains ":" -> path-style
                "git+ssh://host/repo",
            ]
        )
    )
    # Inject into a real field of the entry (never the type discriminator).
    injectable = [k for k in base if k != "type"]
    target = draw(st.sampled_from(injectable))
    base[target] = path_value
    return base


# ---------------------------------------------------------------------------
# Property 2 (task 2.2)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(entry=wellformed_codecommit())
def test_wellformed_codecommit_classifies_as_codecommit(
    entry: dict[str, str],
) -> None:
    """Property 2: Input-form classification.

    A well-formed codecommit entry -> CODECOMMIT (R3.2).

    Validates: Requirements 3.2, 3.3, 3.4
    """
    assert classify_input(entry) is InputForm.CODECOMMIT
    # A valid codecommit entry has no missing required field.
    assert missing_codecommit_fields(entry) == ()


@settings(max_examples=200)
@given(entry=path_style_entry())
def test_path_reference_in_any_field_classifies_as_path(
    entry: dict[str, str],
) -> None:
    """Property 2: Input-form classification.

    A path: input / path:-style reference -> PATH (R3.3).

    Validates: Requirements 3.2, 3.3, 3.4
    """
    assert classify_input(entry) is InputForm.PATH


@settings(max_examples=200)
@given(scenario=codecommit_missing_field())
def test_codecommit_missing_field_classifies_as_invalid_and_names_field(
    scenario: tuple[dict[str, str], str],
) -> None:
    """Property 2: Input-form classification.

    Codecommit entry with dropped field -> INVALID (R3.4).

    Validates: Requirements 3.2, 3.3, 3.4
    """
    entry, dropped = scenario
    assert classify_input(entry) is InputForm.INVALID
    # The specific missing field is recoverable so the gate can name it.
    assert dropped in missing_codecommit_fields(entry)


@settings(max_examples=200)
@given(entry=wellformed_github())
def test_wellformed_github_classifies_as_github(entry: dict[str, str]) -> None:
    """Property 2: Input-form classification.

    A well-formed legacy github entry -> GITHUB.

    Validates: Requirements 3.2, 3.3, 3.4
    """
    # Guard: the plain-field strategy never produces a path:-style value, so a
    # github entry here is genuinely non-path.
    assume(classify_input(entry) is not InputForm.PATH)
    assert classify_input(entry) is InputForm.GITHUB


# ---------------------------------------------------------------------------
# resolve_codecommit_input shape (task 2.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("region", "repo", "branch"),
    [
        ("us-east-1", "Lavalink", "dev"),
        ("us-west-2", "lavaplayer", "main"),
        ("eu-west-1", "LavaSrc", "tidal-v2-api"),
        ("ap-southeast-2", "youtube-source", "main"),
        ("eu-central-1", "hellodj", "main"),
    ],
)
def test_resolve_codecommit_input_shape(
    region: str, repo: str, branch: str
) -> None:
    """resolve_codecommit_input returns the canonical git+https form (R2.1/R3.1).

    Validates: Requirements 2.1, 3.1
    """
    expected = (
        f"git+https://git-codecommit.{region}.amazonaws.com"
        f"/v1/repos/{repo}?ref={branch}"
    )
    assert resolve_codecommit_input(region, repo, branch) == expected


@settings(max_examples=200)
@given(region=_REGION, repo=_PLAIN, branch=_PLAIN)
def test_resolve_codecommit_input_shape_property(
    region: str, repo: str, branch: str
) -> None:
    """resolve_codecommit_input matches the canonical form for arbitrary inputs.

    Validates: Requirements 2.1, 3.1
    """
    expected = (
        f"git+https://git-codecommit.{region}.amazonaws.com"
        f"/v1/repos/{repo}?ref={branch}"
    )
    assert resolve_codecommit_input(region, repo, branch) == expected
