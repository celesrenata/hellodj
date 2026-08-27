"""Delegate management blueprint for the HelloDJ SaaS platform.

Exposes tenant-owner routes for managing delegated access — granting,
revoking, updating, and listing role assignments for Discord users
within a tenant.

All routes are protected by @role_required("owner") (session + RBAC check).

Endpoints:
- GET    /api/v1/tenants/<tenant_id>/delegates                      — list delegates
- POST   /api/v1/tenants/<tenant_id>/delegates                      — grant role
- DELETE /api/v1/tenants/<tenant_id>/delegates/<discord_user_id>     — revoke access
- PATCH  /api/v1/tenants/<tenant_id>/delegates/<discord_user_id>     — update role

Requirements: 6.1, 6.2, 6.4, 6.5, 6.6, 6.7, 6.8
"""

from __future__ import annotations

import logging
import uuid

from flask import Blueprint, g, jsonify, request

from auth_middleware import get_rbac_service, role_required
from services.rbac import DelegateLimitError, InvalidRoleError

log = logging.getLogger(__name__)

delegates_bp = Blueprint("delegates", __name__, url_prefix="/api/v1/tenants")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_tenant_id(tenant_id: str) -> bool:
    """Validate that tenant_id is a valid UUID."""
    try:
        uuid.UUID(tenant_id)
        return True
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Delegate Management Routes
# ---------------------------------------------------------------------------


@delegates_bp.route("/<tenant_id>/delegates", methods=["GET"])
@role_required("owner")
def list_delegates(tenant_id: str):
    """List all delegated users for a tenant.

    Returns a JSON list of delegates with their role, granted_at timestamp,
    and the discord_user_id of the user who granted the role.

    Validates: Requirements 6.1
    """
    if not _validate_tenant_id(tenant_id):
        return jsonify({"error": "Invalid tenant ID format"}), 400

    rbac = get_rbac_service()

    try:
        conn = rbac._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT discord_user_id, role, granted_at, granted_by "
                    "FROM tenant_roles WHERE tenant_id = %s "
                    "ORDER BY granted_at ASC",
                    (tenant_id,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as exc:
        log.error("Failed to list delegates for tenant %s: %s", tenant_id, exc)
        return jsonify({"error": "Failed to retrieve delegates"}), 500

    delegates = [
        {
            "discord_user_id": row["discord_user_id"],
            "role": row["role"],
            "granted_at": row["granted_at"].isoformat() if row["granted_at"] else None,
            "granted_by": row["granted_by"],
        }
        for row in rows
    ]

    return jsonify({"delegates": delegates}), 200


@delegates_bp.route("/<tenant_id>/delegates", methods=["POST"])
@role_required("owner")
def grant_delegate(tenant_id: str):
    """Grant a role to a Discord user for this tenant.

    Request body:
        {"discord_user_id": int, "role": "admin|editor|viewer"}

    Returns 201 on success with the created delegate entry.

    Validates: Requirements 6.1, 6.2, 6.8
    """
    if not _validate_tenant_id(tenant_id):
        return jsonify({"error": "Invalid tenant ID format"}), 400

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be valid JSON"}), 400

    discord_user_id = data.get("discord_user_id")
    role = data.get("role")

    if discord_user_id is None:
        return jsonify({"error": "discord_user_id is required"}), 400

    if not isinstance(discord_user_id, int):
        return jsonify({"error": "discord_user_id must be an integer"}), 400

    if not role:
        return jsonify({"error": "role is required"}), 400

    # Get the granting user's Discord user ID from the session
    granted_by = g.session.get("discord_user_id")
    if isinstance(granted_by, str):
        granted_by = int(granted_by)

    rbac = get_rbac_service()

    try:
        rbac.grant_role(
            tenant_id=tenant_id,
            discord_user_id=discord_user_id,
            role=role,
            granted_by=granted_by,
        )
    except InvalidRoleError:
        return jsonify({"error": "Invalid role. Must be admin, editor, or viewer"}), 400
    except DelegateLimitError:
        return jsonify({"error": "Maximum delegate limit of 20 reached"}), 400
    except Exception as exc:
        log.error(
            "Failed to grant role to user %s for tenant %s: %s",
            discord_user_id,
            tenant_id,
            exc,
        )
        return jsonify({"error": "Failed to grant role"}), 500

    return jsonify({
        "discord_user_id": discord_user_id,
        "role": role,
        "tenant_id": tenant_id,
    }), 201


@delegates_bp.route("/<tenant_id>/delegates/<int:discord_user_id>", methods=["DELETE"])
@role_required("owner")
def revoke_delegate(tenant_id: str, discord_user_id: int):
    """Revoke a delegated user's access to this tenant.

    Returns 204 on success (no content).

    Validates: Requirements 6.7
    """
    if not _validate_tenant_id(tenant_id):
        return jsonify({"error": "Invalid tenant ID format"}), 400

    rbac = get_rbac_service()

    try:
        rbac.revoke_role(tenant_id=tenant_id, discord_user_id=discord_user_id)
    except Exception as exc:
        log.error(
            "Failed to revoke role for user %s from tenant %s: %s",
            discord_user_id,
            tenant_id,
            exc,
        )
        return jsonify({"error": "Failed to revoke role"}), 500

    return "", 204


@delegates_bp.route("/<tenant_id>/delegates/<int:discord_user_id>", methods=["PATCH"])
@role_required("owner")
def update_delegate(tenant_id: str, discord_user_id: int):
    """Update a delegated user's role for this tenant.

    Uses grant_role() as UPSERT — if the user has an existing role, it
    is updated to the new value.

    Request body:
        {"role": "admin|editor|viewer"}

    Returns 200 on success.

    Validates: Requirements 6.2
    """
    if not _validate_tenant_id(tenant_id):
        return jsonify({"error": "Invalid tenant ID format"}), 400

    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be valid JSON"}), 400

    role = data.get("role")
    if not role:
        return jsonify({"error": "role is required"}), 400

    # Get the granting user's Discord user ID from the session
    granted_by = g.session.get("discord_user_id")
    if isinstance(granted_by, str):
        granted_by = int(granted_by)

    rbac = get_rbac_service()

    try:
        rbac.grant_role(
            tenant_id=tenant_id,
            discord_user_id=discord_user_id,
            role=role,
            granted_by=granted_by,
        )
    except InvalidRoleError:
        return jsonify({"error": "Invalid role. Must be admin, editor, or viewer"}), 400
    except DelegateLimitError:
        return jsonify({"error": "Maximum delegate limit of 20 reached"}), 400
    except Exception as exc:
        log.error(
            "Failed to update role for user %s in tenant %s: %s",
            discord_user_id,
            tenant_id,
            exc,
        )
        return jsonify({"error": "Failed to update role"}), 500

    return jsonify({
        "discord_user_id": discord_user_id,
        "role": role,
        "tenant_id": tenant_id,
    }), 200
