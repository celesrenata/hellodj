"""Admin-only entitlement control-plane routes (distinct blueprint).

This blueprint is the administrative control plane for per-user entitlements —
deliberately separate from the self-service ``pages`` and ``guild`` blueprints
so the admin surface is distinct from the Dashboard/Config/Guilds pages (R1.1).
Every route is gated twice:

1. a login check + the same ``_is_admin`` group check ``pages.py`` uses, which
   redirects a non-admin to the dashboard **before any admin content is
   produced** (R1.2); and
2. a hardened post-guard assertion (:func:`_deny_nonadmin`) that runs after the
   redirect check as an explicit fallback — if a non-admin somehow reaches the
   route body (guard bypassed, direct URL manipulation) it returns an HTTP 403
   error page and clears the session rather than rendering admin content
   (R1.3, Property 9).

The read routes here (task 4) are the user picker (``GET /admin/entitlements``)
and a single user's entitlement view (``GET /admin/entitlements/<sub>``). The
mutation routes (task 4.1) flip a flag, set a quota, set the AI markup/cap, and
reset the AI tally, re-rendering the matching HTMX partial on success. Every
mutation is wrapped so that any save failure (quota validation, persistence,
timeout) re-renders the partial with an error notice and never reports success
(R2.4). The templates + nav entry are authored in parallel by task 4.2; the
partial names are referenced here but ``TemplateNotFound`` is tolerated
gracefully (a minimal placeholder response) until those land, so the blueprint
is wired end to end.

Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 2.3, 2.4, 3.1, 4.1, 4.2, 5.1, 6.1, 7.1,
8.1, 9.1, 10.2, 10.4, 10.5, 10.6, 11.1, 12.1, 12.2, 15.3
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from flask import (
    Blueprint,
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from jinja2 import TemplateNotFound

import entitlement_route_helpers as helpers

__all__ = ["build_entitlement_blueprint"]

#: HTTP status returned by the hardened fallback when a non-admin reaches a
#: route body despite the redirect guard (R1.3).
_FORBIDDEN = 403

def _svc(name: str) -> Any:
    """Return an app-extension service by name, or ``None`` in degraded mode."""
    return current_app.extensions.get(name)


def _require_login() -> bool:
    """Return whether an authenticated session exists."""
    return bool(session.get("user"))


def _is_admin() -> bool:
    """Return whether the current session belongs to an administrator.

    Reuses the ``pages`` definition so there is a single source of truth for
    the ``admins``-group check that gates every admin surface.
    """
    from pages import _is_admin as pages_is_admin  # noqa: PLC0415

    return pages_is_admin()


def _deny_nonadmin() -> tuple[str, int]:
    """Explicit fallback deny for a non-admin that reached a route body (R1.3).

    This is the hardened post-guard assertion: the redirect check should have
    already sent a non-admin away, so arriving here means the guard was
    bypassed (e.g. direct URL manipulation). Rather than serving any admin
    content we clear the session (forcing logout) and return an HTTP 403 error
    page. Falls back to a plain 403 body if the error template is absent
    (templates land in task 4.2).
    """
    session.clear()
    try:
        return render_template("pages/forbidden.html"), _FORBIDDEN
    except TemplateNotFound:
        return "Forbidden", _FORBIDDEN


def _admin_guard(view: Callable[..., Any]) -> Callable[..., Any]:
    """Gate a route: login-required, admin-only, with a hardened fallback.

    Applies the two-layer protection every entitlement route shares:

    * not logged in -> redirect to the login page;
    * logged in but not an admin -> redirect to the dashboard *before* any
      admin content is produced (R1.2);
    * a final in-body assertion (:func:`_deny_nonadmin`) that denies with a 403
      / forced logout if a non-admin somehow reaches the wrapped body (R1.3).
    """

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not _require_login():
            return redirect(url_for("pages.login"))
        if not _is_admin():
            return redirect(url_for("pages.dashboard"))
        # Hardened post-guard fallback: never render admin content to a
        # non-admin even if the checks above were somehow bypassed (R1.3).
        if not _is_admin():
            return _deny_nonadmin()
        return view(*args, **kwargs)

    return wrapper


def _layout() -> str:
    """Pick the template layout (partial for HTMX, full shell otherwise)."""
    from pages import _layout as pages_layout  # noqa: PLC0415

    return pages_layout()


def _nav() -> list[dict[str, Any]]:
    """Return the nav items for the current (admin) user."""
    from pages import _nav_for_current_user  # noqa: PLC0415

    return _nav_for_current_user()


def _entitlement_service() -> Any:
    """Return the :class:`EntitlementService` or ``None`` in degraded mode."""
    return _svc("entitlement_service")


def _admin_sub() -> str:
    """Return the acting administrator's Cognito subject for the audit trail.

    The subject is the stable identity recorded against every entitlement change
    (R15.1). It is set on the session at login (``session["user"]["sub"]``);
    falls back to an empty string if somehow absent so a write is still auditable
    with a known-empty actor rather than crashing.
    """
    return str((session.get("user") or {}).get("sub", ""))


def _admin_directory() -> Any:
    """Return the :class:`AdminDirectory` or ``None`` in degraded mode."""
    return _svc("admin_directory")


def _user_rows() -> list[dict[str, Any]]:
    """Return the account rows for the entitlement user picker.

    Reuses the Cognito-backed :class:`AdminDirectory.list_users` (the same
    source the admin panel enumerates), degrading to an empty list when no
    directory is configured so the page renders in tests / no-datastore mode.
    """
    directory = _admin_directory()
    if not directory:
        return []
    return directory.list_users()


def _user_row(sub: str) -> dict[str, Any]:
    """Return the directory row whose Cognito ``sub`` matches ``sub``.

    The picker links each user by their stable Cognito subject; entitlements are
    keyed by ``sub`` (not username) so a single identity spans web-ui and bot.
    Returns an empty mapping when the user is not found in the directory.
    """
    for row in _user_rows():
        if row.get("sub") == sub:
            return row
    return {}


def build_entitlement_blueprint() -> Blueprint:
    """Construct the admin-only entitlement routes blueprint."""
    bp = Blueprint("entitlements", __name__)

    @bp.route("/admin/entitlements")
    @_admin_guard
    def entitlements_index():  # type: ignore[unused-ignore]
        """User picker: choose a user to govern. Admin-only (R1.1, R2.1).

        Enumerates accounts from the Cognito directory; the admin selects one to
        open that user's entitlement view. Renders a minimal placeholder until
        the picker template lands in task 4.2.
        """
        users = _user_rows()
        try:
            return render_template(
                "pages/admin_entitlements.html",
                layout=_layout(),
                nav_items=_nav(),
                active="admin",
                users=users,
            )
        except TemplateNotFound:
            # Template arrives in task 4.2; return a valid response meanwhile.
            return helpers.placeholder_response(
                "Entitlements", f"{len(users)} user(s)"
            )

    @bp.route("/admin/entitlements/<sub>")
    @_admin_guard
    def entitlement_detail(sub: str):  # type: ignore[unused-ignore]
        """One user's flags, quotas, AI tally, and change history. Admin-only.

        Displays the user's identity plus the current value of every entitlement
        flag and quota (R2.1). When the user has no stored record the effective
        values are the secure defaults and ``is_default`` marks them as not
        explicitly set (R2.2). Also surfaces the AI cost tally (R10.4) and the
        reverse-chronological change history (R15.3).
        """
        service = _entitlement_service()
        user = _user_row(sub)
        if service is None:
            # Degraded mode (no datastore): render read-only defaults and mark
            # the values as defaults, matching the graceful-degrade convention.
            effective = helpers.default_effective()
            raw: dict[str, Any] | None = None
            tally: dict[str, Any] = {}
            pricing: dict[str, Any] = {}
            history: list[dict[str, Any]] = []
        else:
            effective = service.get_effective(sub)
            raw = service.get_raw(sub)
            tally = service.get_tally(sub)
            pricing = service.get_pricing()
            history = service.history(sub)
        context = {
            "layout": _layout(),
            "nav_items": _nav(),
            "active": "admin",
            "sub": sub,
            "user": user,
            "effective": effective,
            "is_default": raw is None,
            "tally": tally,
            "pricing": pricing,
            "history": history,
        }
        try:
            return render_template(
                "pages/admin_entitlement_detail.html", **context
            )
        except TemplateNotFound:
            # Template arrives in task 4.2; return a valid response meanwhile.
            return helpers.placeholder_response(
                "Entitlement detail",
                user.get("username", sub),
            )

    @bp.route("/admin/entitlements/<sub>/flags", methods=["POST"])
    @_admin_guard
    def entitlement_flag(sub: str):  # type: ignore[unused-ignore]
        """Flip a boolean entitlement flag and re-render the flag partial.

        The ``flag`` form field names the capability to toggle. A plain flag
        (video, viz, wake-word, AI, custom avatar/name, audio>96k) flips its
        top-level boolean; a source flag flips its entry inside the ``sources``
        map. The new value is always the opposite of the current *effective*
        value (R4.1/R4.2 flip semantics), so a user on defaults flips away from
        the default. On success the change is persisted and audited via
        ``set_fields`` and the flag partial re-renders; on any failure an error
        notice is shown and the change is not reported saved (R2.4).

        Requirements: 2.3, 2.4, 3.1, 4.1, 4.2, 5.1, 6.1, 7.1, 8.1, 9.1
        """
        flag = request.form.get("flag", "")
        service = _entitlement_service()
        if service is None:
            return helpers.render_flags(sub, service, error=helpers.UNAVAILABLE)
        try:
            changes = helpers.flip_change(service.get_effective(sub), flag)
        except ValueError as exc:
            return helpers.render_flags(sub, service, error=str(exc))
        try:
            service.set_fields(sub, changes, admin_sub=_admin_sub())
        except Exception as exc:  # noqa: BLE001 - surface to admin (R2.4)
            return helpers.render_flags(sub, service, error=str(exc))
        return helpers.render_flags(sub, service, saved=True)

    @bp.route("/admin/entitlements/<sub>/quotas", methods=["POST"])
    @_admin_guard
    def entitlement_quota(sub: str):  # type: ignore[unused-ignore]
        """Set one or both numeric quotas, with a field-level ≥1 validation.

        Reads ``max_bots_per_guild`` / ``max_guilds`` from the form (only the
        submitted fields are changed) and the optional
        ``max_bots_per_guild_enabled`` marker. ``set_fields`` runs the shared
        ``validate_quota`` (≥ 1) so a value of 0 or below — or a non-integer —
        raises and is rendered as a field-level validation error without
        persisting anything (R12.2). Any other save failure likewise renders an
        error notice and does not report success (R2.4).

        Requirements: 2.3, 2.4, 11.1, 12.1, 12.2
        """
        service = _entitlement_service()
        if service is None:
            return helpers.render_quotas(
                sub, service, error=helpers.UNAVAILABLE
            )
        try:
            changes = helpers.quota_changes(request.form)
        except ValueError as exc:
            return helpers.render_quotas(sub, service, error=str(exc))
        if not changes:
            return helpers.render_quotas(sub, service)
        try:
            service.set_fields(sub, changes, admin_sub=_admin_sub())
        except ValueError as exc:
            # Quota < 1 (or non-integer) rejected by validate_quota (R12.2).
            return helpers.render_quotas(sub, service, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - surface to admin (R2.4)
            return helpers.render_quotas(sub, service, error=str(exc))
        return helpers.render_quotas(sub, service, saved=True)

    @bp.route("/admin/entitlements/<sub>/ai/markup", methods=["POST"])
    @_admin_guard
    def entitlement_ai_markup(sub: str):  # type: ignore[unused-ignore]
        """Set the per-user AI spend cap (and re-render the AI section).

        Reads ``ai_spend_cap`` from the form: a blank value clears the cap
        (``None`` — no cap, R10.5) and a numeric value sets it. The cap drives
        the over-cap *warning* only; it never hard-blocks AI requests (R10.5).
        The global markup lives in the shared pricing item (R10.2) and is
        ops-edited there, so this route governs the per-user cap. A non-numeric
        cap or any save failure renders an error notice and is not reported
        saved (R2.4).

        Requirements: 2.3, 2.4, 10.2, 10.5
        """
        service = _entitlement_service()
        if service is None:
            return helpers.render_ai(sub, service, error=helpers.UNAVAILABLE)
        try:
            changes = helpers.markup_changes(request.form)
        except ValueError as exc:
            return helpers.render_ai(sub, service, error=str(exc))
        try:
            service.set_fields(sub, changes, admin_sub=_admin_sub())
        except Exception as exc:  # noqa: BLE001 - surface to admin (R2.4)
            return helpers.render_ai(sub, service, error=str(exc))
        return helpers.render_ai(sub, service, saved=True)

    @bp.route("/admin/entitlements/<sub>/ai/reset", methods=["POST"])
    @_admin_guard
    def entitlement_ai_reset(sub: str):  # type: ignore[unused-ignore]
        """Reset a user's accumulated AI cost tally to zero (audited, R10.6).

        Delegates to ``reset_tally`` (write-before-apply, audited). On success
        the AI section re-renders with the zeroed tally; on any failure an error
        notice is shown and the reset is not reported done (R2.4).

        Requirements: 2.4, 10.6
        """
        service = _entitlement_service()
        if service is None:
            return helpers.render_ai(sub, service, error=helpers.UNAVAILABLE)
        try:
            service.reset_tally(sub, admin_sub=_admin_sub())
        except Exception as exc:  # noqa: BLE001 - surface to admin (R2.4)
            return helpers.render_ai(sub, service, error=str(exc))
        return helpers.render_ai(sub, service, saved=True)

    return bp
