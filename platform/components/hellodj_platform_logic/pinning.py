"""Pin-time verification for the platform's ``github:owner/repo/branch`` inputs.

This module holds the pure decision function the pinning workflow invokes when
it captures or refreshes a flake input's pin. Every flake input (Lavalink,
lavaplayer, LavaSrc, youtube-source, Temurin/JDK, nixpkgs, nixos-generators,
Karpenter, the EKS Kubernetes version, ...) pins upstream via
``github:owner/repo/branch`` with the exact revision recorded in ``flake.lock``
(R11.3). At pin time the pinned identifier must equal the identifier resolved
from that input's upstream (R11.1/11.2); this module decides whether a given
pin is accepted on that basis.

It is imported by both the CDK infrastructure layer (the pinning/`nix flake
update` sync step) and any runtime pinning tooling so they share a single
source of truth, and it makes no live network or git calls -- the upstream
identifier is resolved by the caller and injected -- so the correctness
property can exercise it directly.

Implemented here:

* :func:`verify_pin` -- Property 13 / R11.1, R11.5, R11.6. Given a captured
  :class:`~hellodj_platform_logic.types.FlakeInputPin` and the identifier
  resolved from its upstream at pin time (``None`` when upstream could not be
  resolved), decide whether the pin is accepted:

  - **accept** iff the pinned identifier equals the resolved upstream
    identifier (R11.1);
  - **reject** the pin and name the input, retaining the prior pinned revision,
    when the pinned identifier differs from a resolved upstream identifier
    (R11.5);
  - **fail** the pin for that input and name it, retaining the prior pinned
    revision, when the upstream source cannot be resolved (``None``) (R11.6).

Design references:
    * Components -- Upstream version pinning (§11): pin-time verification
      resolves the upstream identifier and rejects a mismatched pin / fails an
      unresolved pin, retaining the prior pinned revision in both failure paths.
    * Correctness Property 13: Pin verification accepts equal identifiers and
      otherwise retains the prior pin.
    * Error Handling: mismatch -> reject + name input + retain prior (R11.5);
      unresolved upstream -> fail + name input + retain prior (R11.6).

Requirements: 11.1, 11.5, 11.6
"""

from __future__ import annotations

from hellodj_platform_logic.types import FlakeInputPin, PinVerification

__all__ = ["verify_pin"]


def verify_pin(
    pin: FlakeInputPin,
    upstream_identifier: str | None,
) -> PinVerification:
    """Verify a flake input pin against its upstream identifier at pin time.

    Implements Property 13 / R11.1, R11.5, R11.6. The upstream identifier is
    resolved by the caller (this function performs no network or git calls) and
    passed in; ``None`` signals that the input's upstream source could not be
    resolved at pin time.

    The decision is:

    * **Accepted (R11.1).** When ``upstream_identifier`` is resolved and equals
      ``pin.pinned_identifier``, the pin is accepted: the returned
      :class:`~hellodj_platform_logic.types.PinVerification` has ``accepted`` set
      to ``True``, carries the resolved ``upstream_identifier`` and an empty
      ``reason``.
    * **Rejected on mismatch (R11.5).** When ``upstream_identifier`` is resolved
      but differs from ``pin.pinned_identifier``, the pin is rejected: the result
      has ``accepted`` set to ``False``, carries the resolved
      ``upstream_identifier`` and a ``reason`` naming exactly this input and
      noting the prior pinned revision is retained.
    * **Failed on unresolved upstream (R11.6).** When ``upstream_identifier`` is
      ``None``, the pin fails for this input: the result has ``accepted`` set to
      ``False``, carries ``upstream_identifier=None`` and a ``reason`` naming
      exactly this input and noting the prior pinned revision is retained.

    In both failure paths the caller retains the prior pinned revision; this
    function only reports the outcome (it holds no state). The returned
    ``input_name`` always mirrors ``pin.input_name`` so the affected input is
    identified regardless of outcome.

    Args:
        pin: The captured flake input pin, carrying the ``input_name`` and the
            ``pinned_identifier`` (the revision/tag/version recorded in
            ``flake.lock`` at pin time) to verify.
        upstream_identifier: The identifier resolved from the input's upstream at
            pin time, or ``None`` when the upstream source could not be resolved.

    Returns:
        A :class:`~hellodj_platform_logic.types.PinVerification` for
        ``pin.input_name``: accepted with the matching identifier when they are
        equal (R11.1); otherwise rejected/failed with a populated ``reason``
        naming the input, indicating the prior pinned revision is retained
        (R11.5 on mismatch, R11.6 on unresolved upstream).

    Requirements: 11.1, 11.5, 11.6
    """
    if upstream_identifier is None:
        # Upstream could not be resolved: fail the pin for this input, name it,
        # and retain the prior pinned revision (R11.6).
        return PinVerification(
            input_name=pin.input_name,
            accepted=False,
            upstream_identifier=None,
            reason=(
                f"upstream for input {pin.input_name!r} could not be resolved "
                "at pin time; pin failed, prior pinned revision retained"
            ),
        )

    if upstream_identifier == pin.pinned_identifier:
        # Pinned identifier matches upstream: accept the pin (R11.1).
        return PinVerification(
            input_name=pin.input_name,
            accepted=True,
            upstream_identifier=upstream_identifier,
        )

    # Pinned identifier differs from the resolved upstream: reject the pin,
    # name the input, and retain the prior pinned revision (R11.5).
    return PinVerification(
        input_name=pin.input_name,
        accepted=False,
        upstream_identifier=upstream_identifier,
        reason=(
            f"pinned identifier {pin.pinned_identifier!r} for input "
            f"{pin.input_name!r} does not match upstream identifier "
            f"{upstream_identifier!r}; pin rejected, prior pinned revision "
            "retained"
        ),
    )
