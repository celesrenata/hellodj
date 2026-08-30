"""Pure form-parsing helpers for the admin entitlement mutation routes.

These helpers translate a submitted HTML form into the ``changes`` map that
:meth:`entitlement_service.EntitlementService.set_fields` applies. They are
factored out of :mod:`entitlement_routes` to keep that module under the
project's 500-line ceiling, and are deliberately side-effect free (no Flask
request/session/render access, no boto3) so they can be unit-tested in isolation
by task 4.3. The ``form`` argument is any Mapping-like object exposing ``.get``
(a Werkzeug ``MultiDict`` in production, a plain ``dict`` in tests).

The flag catalog lives here too so the route module and tests share one source
of truth for which capabilities a ``POST .../flags`` request may flip:

* :data:`TOGGLE_FLAGS` — top-level boolean capabilities (custom identity R4,
  high-bitrate audio R5, video R6, visualizations R7, wake-word R8, AI R9).
* :data:`SOURCE_FLAGS` — playback sources flipped inside the ``sources`` map (R3).
* :data:`QUOTA_FIELDS` — numeric quotas set by ``POST .../quotas`` (R11.1, R12.1).

Requirements: 2.3, 3.1, 4.1, 4.2, 5.1, 6.1, 7.1, 8.1, 9.1, 10.5, 11.1, 12.1, 12.2
"""

from __future__ import annotations

from typing import Any

from flask import render_template
from jinja2 import TemplateNotFound

__all__ = [
    "TOGGLE_FLAGS",
    "SOURCE_FLAGS",
    "QUOTA_FIELDS",
    "FLAGS_PARTIAL",
    "QUOTAS_PARTIAL",
    "AI_PARTIAL",
    "UNAVAILABLE",
    "flip_change",
    "quota_changes",
    "markup_changes",
    "default_effective",
    "placeholder_response",
    "render_flags",
    "render_quotas",
    "render_ai",
]

#: Boolean entitlement flags a ``POST .../flags`` request may flip. Sources are
#: handled separately (nested under the ``sources`` map) via :data:`SOURCE_FLAGS`.
#: Covers custom identity (R4), high-bitrate audio (R5), video (R6),
#: visualizations (R7), wake-word (R8), and AI integration (R9).
TOGGLE_FLAGS = frozenset(
    {
        "custom_avatar",
        "custom_name",
        "audio_above_96k",
        "video_activities",
        "visualizations",
        "wakeword",
        "ai_integration",
        "premium_sources",
    }
)

#: Playback sources a ``POST .../flags`` request may flip (R3.1). Flipping a
#: source toggles its entry inside the entitlement ``sources`` map.
SOURCE_FLAGS = frozenset(
    {"youtube", "youtube_music", "soundcloud", "spotify", "tidal"}
)

#: Numeric quota fields a ``POST .../quotas`` request may set (R11.1, R12.1).
QUOTA_FIELDS = frozenset({"max_bots_per_guild", "max_guilds"})


def flip_change(effective: dict[str, Any], flag: str) -> dict[str, Any]:
    """Build the ``set_fields`` change map that flips ``flag``.

    The flip is relative to the current *effective* value (stored merged over
    defaults) so a user on defaults flips away from the default (R4.1/R4.2). A
    top-level toggle flips its boolean; a source flag flips its entry inside a
    full ``sources`` map (merged over the effective sources so the other
    providers are preserved).

    Args:
        effective: The user's current effective entitlements.
        flag: The capability or source name to flip.

    Returns:
        A single-field change map suitable for ``set_fields``.

    Raises:
        ValueError: If ``flag`` is not a recognized toggle or source flag.
    """
    if flag in TOGGLE_FLAGS:
        return {flag: not bool(effective.get(flag, False))}
    if flag in SOURCE_FLAGS:
        sources = dict(effective.get("sources") or {})
        sources[flag] = not bool(sources.get(flag, False))
        return {"sources": sources}
    raise ValueError(f"unknown entitlement flag: {flag!r}")


def quota_changes(form: Any) -> dict[str, Any]:
    """Build the quota change map from submitted form fields (R11.1, R12.1).

    Only quota fields present in the form are included. Numeric quotas are
    coerced to ``int`` here (a non-integer raises ``ValueError``, surfaced by the
    route as a field error) — the >= 1 bound itself is enforced by
    ``validate_quota`` inside ``set_fields`` (R12.2). The optional
    ``max_bots_per_guild_enabled`` marker is passed through as a boolean when
    present.

    Raises:
        ValueError: If a submitted quota value is not a whole number.
    """
    changes: dict[str, Any] = {}
    for field in QUOTA_FIELDS:
        raw = form.get(field)
        if raw is None or raw == "":
            continue
        try:
            changes[field] = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be a whole number") from exc
    marker = form.get("max_bots_per_guild_enabled")
    if marker is not None:
        changes["max_bots_per_guild_enabled"] = marker == "true"
    return changes


def markup_changes(form: Any) -> dict[str, Any]:
    """Build the AI change map (per-user spend cap) from the form (R10.5).

    A blank ``ai_spend_cap`` clears the cap (stores ``None`` — no cap). A numeric
    value sets it; a non-numeric value raises ``ValueError`` (surfaced by the
    route as an error notice). The cap is a warning threshold only and never
    hard-blocks AI requests.

    Raises:
        ValueError: If a submitted spend cap is not a number.
    """
    changes: dict[str, Any] = {}
    raw = form.get("ai_spend_cap")
    if raw is not None:
        if raw == "":
            changes["ai_spend_cap"] = None
        else:
            try:
                changes["ai_spend_cap"] = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("AI spend cap must be a number") from exc
    return changes


# --------------------------------------------------------------------------- #
# HTMX partial rendering helpers
#
# These render the flag / quota / AI-section partials the mutation routes swap
# back into the page. They live here (not in ``entitlement_routes``) to keep the
# route module under the 500-line ceiling. They *do* touch Flask's
# ``render_template`` (that is their whole job), but they take the
# ``EntitlementService`` as an argument rather than reaching into app globals, so
# they stay straightforward to test with a fake service.
# --------------------------------------------------------------------------- #

#: HTMX partial templates re-rendered after a mutation (authored by task 4.2).
FLAGS_PARTIAL = "partials/entitlement_flags.html"
QUOTAS_PARTIAL = "partials/entitlement_quotas.html"
AI_PARTIAL = "partials/entitlement_ai.html"

#: Error notice shown when a mutation is attempted with no datastore configured
#: (degraded mode): the write is unavailable and is never reported saved (R2.4).
UNAVAILABLE = "Entitlements storage is unavailable; the change was not saved."


def default_effective() -> dict[str, Any]:
    """Return the effective secure-default entitlement set (degraded mode)."""
    import entitlements_core  # noqa: PLC0415

    return entitlements_core.merge_effective(None)


def placeholder_response(title: str, detail: str) -> str:
    """Return a minimal valid HTML response used until templates land (4.2).

    Kept deliberately tiny and admin-gated (only reachable through the route
    guard); task 4.2 replaces these with the real entitlement templates.
    """
    return (
        "<!doctype html><title>"
        f"{title}</title><main><h1>{title}</h1><p>{detail}</p></main>"
    )


def render_flags(
    sub: str, service: Any, *, saved: bool = False, error: str | None = None
) -> Any:
    """Re-render the flag HTMX partial with the freshest effective flags (R2.4).

    Re-reads the effective entitlements (post-write on success, unchanged on
    failure) so the partial always reflects the true stored state. ``saved`` is
    only ``True`` when the write actually succeeded.
    """
    return _render_partial(
        FLAGS_PARTIAL, "Entitlement flags", sub, service,
        saved=saved, error=error,
    )


def render_quotas(
    sub: str, service: Any, *, saved: bool = False, error: str | None = None
) -> Any:
    """Re-render the quota HTMX partial (field-level error on violation, R12.2).

    On a validation failure the rejected value is not persisted, so the re-read
    effective quotas show the last-saved values alongside the ``error`` notice.
    """
    return _render_partial(
        QUOTAS_PARTIAL, "Entitlement quotas", sub, service,
        saved=saved, error=error,
    )


def render_ai(
    sub: str, service: Any, *, saved: bool = False, error: str | None = None
) -> Any:
    """Re-render the AI-section HTMX partial (tally, cap, pricing/markup).

    Surfaces the accumulated tally (R10.4) and the over-cap warning (R10.5) — a
    warning signal only, never a hard block. ``saved`` reflects a successful
    markup/cap change or tally reset; ``error`` carries the notice on failure.
    """
    import entitlements_core  # noqa: PLC0415

    tally = service.get_tally(sub) if service else {}
    pricing = service.get_pricing() if service else {}
    effective = service.get_effective(sub) if service else default_effective()
    accumulated = float(tally.get("accumulated_cost", 0.0))
    return _render_partial(
        AI_PARTIAL, "Entitlement AI usage", sub, service,
        saved=saved, error=error,
        tally=tally,
        pricing=pricing,
        over_cap=entitlements_core.over_cap(
            accumulated, effective.get("ai_spend_cap")
        ),
    )


def _render_partial(
    template: str,
    title: str,
    sub: str,
    service: Any,
    *,
    saved: bool = False,
    error: str | None = None,
    **extra: Any,
) -> Any:
    """Render an entitlement HTMX partial, tolerating its absence (task 4.2).

    Supplies the shared context every entitlement partial needs (the user's
    ``sub``, effective entitlements, the ``is_default`` marker, and the
    saved/error state) plus any section-specific ``extra`` context. Until the
    partials exist a ``TemplateNotFound`` falls back to a minimal placeholder
    that still conveys the saved/error state so the route contract (never
    silently succeed on failure, R2.4) holds end to end.
    """
    effective = service.get_effective(sub) if service else default_effective()
    context: dict[str, Any] = {
        "sub": sub,
        "effective": effective,
        "is_default": service is not None and service.get_raw(sub) is None,
        "saved": saved,
        "error": error,
        **extra,
    }
    try:
        return render_template(template, **context)
    except TemplateNotFound:
        if error:
            detail = f"error: {error}"
        elif saved:
            detail = "saved"
        else:
            detail = "ok"
        return placeholder_response(title, detail)
