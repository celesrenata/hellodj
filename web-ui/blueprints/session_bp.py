"""Session management blueprint for the HelloDJ SaaS platform.

Provides:
- POST /api/v1/session/tenant — switch active tenant context

Requires an authenticated session (@login_required).
"""

from __future__ import annotations

import logging
import uuid

from flask import Blueprint, g, jsonify, request

from auth_middleware import _get_session_service, login_required

log = logging.getLogger(__name__)

session_bp = Blueprint("session_api", __name__, url_prefix="/api/v1/session")

# Cookie name must match the one used in auth_middleware
SESSION_COOKIE_NAME = "hellodj_session"


@session_bp.route("/tenant", methods=["POST"])
@login_required
def switch_tenant():
    """Switch the active tenant context for the current session.

    Accepts JSON body: {"tenant_id": "uuid-string"}

    Validates:
    - tenant_id is present and a valid UUID
    - tenant_id exists in the user's accessible tenant list (from session roles)

    Returns:
    - 200 with {"active_tenant_id": "...", "role": "..."} on success
    - 400 if tenant_id missing or invalid UUID format
    - 403 if tenant_id not in user's accessible tenant list
    """
    # Parse JSON body
    body = request.get_json(silent=True)
    if not body or "tenant_id" not in body:
        return jsonify({"error": "Missing required field: tenant_id"}), 400

    tenant_id = body["tenant_id"]

    # Validate UUID format
    try:
        uuid.UUID(str(tenant_id))
    except (ValueError, AttributeError):
        return jsonify({"error": "Invalid tenant_id format: must be a valid UUID"}), 400

    # Build accessible tenant list from session roles
    session = g.session
    roles = session.get("roles", [])
    accessible_tenants = [r["tenant_id"] for r in roles if "tenant_id" in r]

    # Check access
    if str(tenant_id) not in accessible_tenants:
        return jsonify({"error": "You do not have access to this tenant"}), 403

    # Get the session token from the cookie
    token = request.cookies.get(SESSION_COOKIE_NAME)

    # Call SessionService.switch_tenant
    svc = _get_session_service()
    success = svc.switch_tenant(token, str(tenant_id), accessible_tenants)

    if not success:
        # This shouldn't happen since we already checked, but handle defensively
        return jsonify({"error": "You do not have access to this tenant"}), 403

    # Look up the user's role for the switched tenant
    role = None
    for r in roles:
        if r.get("tenant_id") == str(tenant_id):
            role = r.get("role")
            break

    log.info(
        "Tenant context switched: discord_user_id=%s, active_tenant_id=%s, role=%s",
        session.get("discord_user_id"),
        tenant_id,
        role,
    )

    return jsonify({"active_tenant_id": str(tenant_id), "role": role}), 200
