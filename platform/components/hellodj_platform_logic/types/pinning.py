"""Flake input pinning, CodeCommit input classification, stale-pin report, alarm notification.

Types for pin verification (R11), CodeCommit inputs (R2/R3), stale-pin
reporting (R6.1), and alarm notification subject rewriting (R7.2/R7.3/R7.5).

Requirements: 11.x, 2.x, 3.x, 6.1, 7.2, 7.3, 7.5
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "FlakeInputPin",
    "PinVerification",
    "InputForm",
    "CodeCommitInput",
    "StalePin",
    "AlarmNotification",
    "EmailDelivery",
]


# ---------------------------------------------------------------------------
# Flake input pinning (Property 13, R11)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlakeInputPin:
    """One github:owner/repo/branch flake input and its pinned identifier (R11)."""

    input_name: str          # e.g. "lavalink", "temurin", "nixpkgs"
    owner: str               # e.g. "hellodj", "NixOS"
    repo: str
    branch: str              # github:owner/repo/branch
    pinned_identifier: str   # revision/tag/version captured in flake.lock at pin time


@dataclass(frozen=True)
class PinVerification:
    """Outcome of verifying a pin against upstream at pin time (R11.5/11.6)."""

    input_name: str
    accepted: bool
    upstream_identifier: str | None  # None when upstream could not be resolved (R11.6)
    reason: str = ""                 # set when rejected/unresolved; prior pin retained


# ---------------------------------------------------------------------------
# CodeCommit flake inputs and input-form classification (Property 2, R2/R3)
# ---------------------------------------------------------------------------


class InputForm(Enum):
    """Classification of a ``pins.toml`` entry's input form (R3.2/R3.3/R3.4).

    The pin gate classifies every manifest entry into exactly one of these
    forms. :attr:`GITHUB` is the legacy ``github:owner/repo/branch`` form (still
    accepted). :attr:`CODECOMMIT` is the newly accepted CodeCommit
    ``git+https://…/v1/repos/<repo>?ref=<branch>`` form (R3.2). :attr:`PATH`
    marks a ``path:`` input or ``path:``-style reference, which is always
    rejected (R3.3). :attr:`INVALID` marks a CodeCommit entry missing a required
    field — its region, repository name, or branch (R3.4).
    """

    GITHUB = "github"          # legacy github:owner/repo/branch (still accepted)
    CODECOMMIT = "codecommit"  # git+https CodeCommit (newly accepted, R3.2)
    PATH = "path"              # path: input — always REJECTED (R3.3)
    INVALID = "invalid"        # missing required field (R3.4)


@dataclass(frozen=True)
class CodeCommitInput:
    """A CodeCommit flake input (R2.1/R3.1).

    Resolves to
    ``git+https://git-codecommit.<region>.amazonaws.com/v1/repos/<repo>?ref=<branch>``.
    ``pinned_identifier`` is the commit revision captured in ``flake.lock`` at
    pin time.
    """

    input_name: str
    region: str
    repo: str
    branch: str
    pinned_identifier: str


# ---------------------------------------------------------------------------
# Stale-pin report
# (hellodj-private-source-and-toolchain Property 6, R6.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StalePin:
    """A pin whose pinned identifier != current upstream identifier (R6.1).

    One entry in the stale-pin report produced by
    :func:`hellodj_platform_logic.stale_pins.stale_pins`. A pin is stale exactly
    when the upstream identifier resolved for its entry is present and differs
    from the pinned identifier -- i.e. exactly the set
    :func:`hellodj_platform_logic.pinning.verify_pin` would reject. Each report
    entry lists both the pinned identifier and the current upstream identifier it
    differs from (R6.1). An entry whose upstream cannot be resolved is *not*
    stale (it is a resolution failure surfaced separately), so
    :attr:`upstream_identifier` is always a resolved, non-``None`` value here.
    """

    input_name: str
    pinned_identifier: str
    upstream_identifier: str  # the current upstream identifier it differs from


# ---------------------------------------------------------------------------
# Alarm notification + optional subject rewrite
# (hellodj-private-source-and-toolchain Property 7, R7.2/R7.3/R7.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlarmNotification:
    """The fields of a CloudWatch alarm state-change notification (R7.2/R7.3).

    Models the input to the optional ``Subject_Rewriter`` Lambda: the original
    alarm state-change notification as delivered by the alarm SNS topic. The
    rewriter reads ``alarm_name`` plus the ``previous_state`` and ``new_state``
    to build a body that reproduces each verbatim (R7.3), and prefixes
    ``original_subject`` with ``HelloDJ:`` for the delivered email (R7.2). On a
    rewriter failure the ``original_subject``/``original_body`` are delivered
    unchanged so no alarm is dropped (fail-open, R7.5).
    """

    alarm_name: str
    previous_state: str
    new_state: str
    original_subject: str
    original_body: str


@dataclass(frozen=True)
class EmailDelivery:
    """The email delivered by the (optional) Subject_Rewriter (R7.2/R7.3/R7.5).

    The output of :func:`hellodj_platform_logic.alarm_subject.rewriter_outcome`.
    On a successful rewrite (``rewritten`` True) the ``subject`` begins with
    ``HelloDJ:`` (R7.2) and the ``body`` reproduces the alarm name and both the
    previous and new state verbatim (R7.3). On a fail-open delivery
    (``rewritten`` False) the original notification's subject and body are
    delivered unaltered so the alarm is never silently dropped (R7.5).
    """

    subject: str              # begins with "HelloDJ:" when rewritten (R7.2)
    body: str                 # contains alarm_name + prev/new state verbatim (R7.3)
    rewritten: bool           # False on fail-open delivery of the original (R7.5)
