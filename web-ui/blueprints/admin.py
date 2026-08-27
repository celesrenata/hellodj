"""Admin panel blueprint for the HelloDJ SaaS platform.

Exposes operator-only routes for managing trials, subscriptions, bot instances,
and viewing system-wide metrics.

All routes are protected by @operator_required (session + operator identity check).

Endpoints:
- GET    /api/v1/admin/trials                   — pending trial applications (oldest first)
- POST   /api/v1/admin/trials/{id}/approve      — approve trial
- POST   /api/v1/admin/trials/{id}/deny         — deny trial
- GET    /api/v1/admin/subscriptions            — all subscriptions with tenant + bot health
- POST   /api/v1/admin/subscriptions/{id}/suspend   — suspend subscription
- POST   /api/v1/admin/subscriptions/{id}/terminate — terminate subscription
- GET    /api/v1/admin/metrics                  — system-wide metrics
- GET    /api/v1/admin/instances                — all bot instances with health status

Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7
"""

from __future__ import annotations

import logging
import os
import uuid

import psycopg2
import psycopg2.extras
from flask import Blueprint, g, jsonify, request

from auth_middleware import operator_required
from services import trial_manager
from services.trial_manager import TrialError
from services.subscription_manager import (
    InvalidStateTransitionError,
    SubscriptionManager,
    SubscriptionNotFoundError,
)

log = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/api/v1/admin")

# ---------------------------------------------------------------------------
# Database connection
# ---------------------------------------------------------------------------

PG_URI = os.environ.get(
    "HELLODJ_PG_URI",
    "postgresql://hellodj:hellodj@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj",
)


def _get_pg_conn():
    """Get a psycopg2 connection to PostgreSQL."""
    return psycopg2.connect(PG_URI)


# ---------------------------------------------------------------------------
# Module-level SubscriptionManager
# ---------------------------------------------------------------------------

_subscription_manager: SubscriptionManager | None = None


def _get_subscription_manager() -> SubscriptionManager:
    """Return the module-level SubscriptionManager, creating one on first use."""
    global _subscription_manager
    if _subscription_manager is None:
        _subscription_manager = SubscriptionManager()
    return _subscription_manager


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _serialize(record: dict) -> dict:
    """Convert a database record dict to JSON-serializable format."""
    result = {}
    for key, value in record.items():
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# Trial Management Routes
# ---------------------------------------------------------------------------


@admin_bp.route("/trials", methods=["GET"])
@operator_required
def list_pending_trials():
    """List pending trial applications ordered by application date (oldest first).

    Returns JSON array of pending applications with tenant details.

    Validates: Requirement 9.2
    """
    try:
        applications = trial_manager.get_pending_applications()
    except Exception as exc:
        log.error("Failed to fetch pending trial applications: %s", exc)
        return jsonify({"error": "Failed to retrieve pending trials"}), 500

    result = [_serialize(app) for app in applications]
    return jsonify({"trials": result}), 200


@admin_bp.route("/trials/<application_id>/approve", methods=["POST"])
@operator_required
def approve_trial(application_id: str):
    """Approve a trial application, activating a 30-day trial for the tenant.

    Path params:
        application_id: UUID of the trial application.

    Validates: Requirements 9.3
    """
    # Validate UUID format
    try:
        uuid.UUID(application_id)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid application ID format"}), 400

    # Use the operator's Discord username as the decided_by value
    decided_by = g.tenant.get("discord_username", "operator")

    try:
        result = trial_manager.approve(application_id, decided_by)
    except TrialError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        log.error("Failed to approve trial %s: %s", application_id, exc)
        return jsonify({"error": "Failed to approve trial"}), 500

    log.info(
        "Trial approved via admin panel: application_id=%s decided_by=%s",
        application_id,
        decided_by,
    )

    return jsonify({
        "application": _serialize(result["application"]),
        "subscription": _serialize(result["subscription"]),
    }), 200


@admin_bp.route("/trials/<application_id>/deny", methods=["POST"])
@operator_required
def deny_trial(application_id: str):
    """Deny a trial application.

    Path params:
        application_id: UUID of the trial application.

    Validates: Requirement 9.4
    """
    # Validate UUID format
    try:
        uuid.UUID(application_id)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid application ID format"}), 400

    decided_by = g.tenant.get("discord_username", "operator")

    try:
        result = trial_manager.deny(application_id, decided_by)
    except TrialError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        log.error("Failed to deny trial %s: %s", application_id, exc)
        return jsonify({"error": "Failed to deny trial"}), 500

    log.info(
        "Trial denied via admin panel: application_id=%s decided_by=%s",
        application_id,
        decided_by,
    )

    return jsonify({"application": _serialize(result)}), 200


# ---------------------------------------------------------------------------
# Subscription Management Routes
# ---------------------------------------------------------------------------


@admin_bp.route("/subscriptions", methods=["GET"])
@operator_required
def list_all_subscriptions():
    """List all subscriptions with tenant details, plan, addons, payment status, bot health.

    Validates: Requirement 9.5
    """
    conn = _get_pg_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    s.id,
                    s.tenant_id,
                    s.plan,
                    s.addons,
                    s.status,
                    s.started_at,
                    s.expires_at,
                    s.created_at,
                    t.discord_user_id,
                    t.discord_username,
                    t.email
                FROM subscriptions s
                JOIN tenants t ON t.id = s.tenant_id
                ORDER BY s.created_at DESC
                """
            )
            subscriptions = cur.fetchall()

            # Fetch bot instance health for each subscription's tenant
            cur.execute(
                """
                SELECT
                    bi.id,
                    bi.tenant_id,
                    bi.status,
                    bi.node_name,
                    bi.pod_name,
                    bi.guild_ids
                FROM bot_instances bi
                """
            )
            instances = cur.fetchall()

        # Group instances by tenant_id for lookup
        instances_by_tenant: dict[str, list[dict]] = {}
        for inst in instances:
            tid = str(inst["tenant_id"])
            instances_by_tenant.setdefault(tid, []).append(dict(inst))

        # Build response with bot health info
        result = []
        for sub in subscriptions:
            sub_dict = _serialize(dict(sub))
            tid = str(sub["tenant_id"])
            tenant_instances = instances_by_tenant.get(tid, [])

            # Determine overall bot health from instance statuses
            bot_health = _compute_bot_health(tenant_instances)
            sub_dict["bot_health"] = bot_health
            sub_dict["bot_instances"] = [_serialize(i) for i in tenant_instances]
            result.append(sub_dict)

        return jsonify({"subscriptions": result}), 200
    except Exception as exc:
        log.error("Failed to list subscriptions: %s", exc)
        return jsonify({"error": "Failed to retrieve subscriptions"}), 500
    finally:
        conn.close()


@admin_bp.route("/subscriptions/<subscription_id>/suspend", methods=["POST"])
@operator_required
def suspend_subscription(subscription_id: str):
    """Suspend a subscription (requires confirmation).

    Sets the subscription status to 'cancelled' (suspended state).
    Request body may include {"confirmed": true} for confirmation flow.

    Validates: Requirement 9.6
    """
    # Validate UUID format
    try:
        sub_uuid = uuid.UUID(subscription_id)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid subscription ID format"}), 400

    # Check for confirmation
    data = request.get_json(silent=True) or {}
    if not data.get("confirmed", False):
        return jsonify({
            "error": "Confirmation required",
            "message": "Send {\"confirmed\": true} to confirm suspension",
            "subscription_id": subscription_id,
        }), 400

    sm = _get_subscription_manager()

    try:
        updated = sm.cancel(sub_uuid)
    except SubscriptionNotFoundError:
        return jsonify({"error": "Subscription not found"}), 404
    except InvalidStateTransitionError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        log.error("Failed to suspend subscription %s: %s", subscription_id, exc)
        return jsonify({"error": "Failed to suspend subscription"}), 500

    log.info(
        "Subscription suspended via admin panel: id=%s by=%s",
        subscription_id,
        g.tenant.get("discord_username", "operator"),
    )

    return jsonify({"subscription": _serialize(updated)}), 200


@admin_bp.route("/subscriptions/<subscription_id>/terminate", methods=["POST"])
@operator_required
def terminate_subscription(subscription_id: str):
    """Terminate a subscription (requires confirmation).

    Forces expiration of the subscription regardless of current state.
    Request body may include {"confirmed": true} for confirmation flow.

    Validates: Requirement 9.6
    """
    # Validate UUID format
    try:
        sub_uuid = uuid.UUID(subscription_id)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid subscription ID format"}), 400

    # Check for confirmation
    data = request.get_json(silent=True) or {}
    if not data.get("confirmed", False):
        return jsonify({
            "error": "Confirmation required",
            "message": "Send {\"confirmed\": true} to confirm termination",
            "subscription_id": subscription_id,
        }), 400

    sm = _get_subscription_manager()

    # For terminate, we first check the subscription exists, then force-expire it
    try:
        existing = sm.get_subscription(sub_uuid)
    except Exception as exc:
        log.error("Failed to fetch subscription %s: %s", subscription_id, exc)
        return jsonify({"error": "Failed to verify subscription"}), 500

    if existing is None:
        return jsonify({"error": "Subscription not found"}), 404

    # If already expired or cancelled, return current state
    if existing["status"] in ("expired", "cancelled"):
        return jsonify({
            "subscription": _serialize(existing),
            "message": "Subscription already terminated",
        }), 200

    # If active, expire it; if pending_payment, cancel it
    try:
        if existing["status"] == "active":
            updated = sm.expire(sub_uuid)
        else:
            updated = sm.cancel(sub_uuid)
    except (SubscriptionNotFoundError, InvalidStateTransitionError) as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        log.error("Failed to terminate subscription %s: %s", subscription_id, exc)
        return jsonify({"error": "Failed to terminate subscription"}), 500

    log.info(
        "Subscription terminated via admin panel: id=%s by=%s",
        subscription_id,
        g.tenant.get("discord_username", "operator"),
    )

    return jsonify({"subscription": _serialize(updated)}), 200


# ---------------------------------------------------------------------------
# System Metrics Route
# ---------------------------------------------------------------------------


@admin_bp.route("/metrics", methods=["GET"])
@operator_required
def system_metrics():
    """Return system-wide metrics.

    Returns:
        JSON with total tenants, active trials, active subscriptions,
        total bot instances, and GPU utilization per node.

    Validates: Requirement 9.7
    """
    conn = _get_pg_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Total tenants
            cur.execute("SELECT COUNT(*) AS count FROM tenants")
            total_tenants = cur.fetchone()["count"]

            # Active trials (subscriptions with plan='trial' and status='active')
            cur.execute(
                "SELECT COUNT(*) AS count FROM subscriptions WHERE plan = 'trial' AND status = 'active'"
            )
            active_trials = cur.fetchone()["count"]

            # Active subscriptions (all plans with status='active')
            cur.execute(
                "SELECT COUNT(*) AS count FROM subscriptions WHERE status = 'active'"
            )
            active_subscriptions = cur.fetchone()["count"]

            # Total bot instances
            cur.execute("SELECT COUNT(*) AS count FROM bot_instances")
            total_bot_instances = cur.fetchone()["count"]

            # Bot instances by status
            cur.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM bot_instances
                GROUP BY status
                """
            )
            instances_by_status = {
                row["status"]: row["count"] for row in cur.fetchall()
            }

        # GPU utilization per node — placeholder data
        # In production this would query the Intel GPU device plugin or
        # Prometheus metrics. For now return the node topology with dummy data.
        gpu_utilization = _get_gpu_utilization()

        return jsonify({
            "metrics": {
                "total_tenants": total_tenants,
                "active_trials": active_trials,
                "active_subscriptions": active_subscriptions,
                "total_bot_instances": total_bot_instances,
                "instances_by_status": instances_by_status,
                "gpu_utilization": gpu_utilization,
            }
        }), 200
    except Exception as exc:
        log.error("Failed to compute system metrics: %s", exc)
        return jsonify({"error": "Failed to retrieve metrics"}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bot Instances Route
# ---------------------------------------------------------------------------


@admin_bp.route("/instances", methods=["GET"])
@operator_required
def list_all_instances():
    """List all bot instances with health status.

    Returns JSON array of all bot instances with tenant info and health indicators.

    Validates: Requirement 9.7 (system visibility)
    """
    conn = _get_pg_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    bi.id,
                    bi.tenant_id,
                    bi.status,
                    bi.node_name,
                    bi.pod_name,
                    bi.guild_ids,
                    bi.created_at,
                    t.discord_user_id,
                    t.discord_username
                FROM bot_instances bi
                JOIN tenants t ON t.id = bi.tenant_id
                ORDER BY bi.created_at DESC
                """
            )
            instances = cur.fetchall()

        result = [_serialize(dict(inst)) for inst in instances]
        return jsonify({"instances": result}), 200
    except Exception as exc:
        log.error("Failed to list bot instances: %s", exc)
        return jsonify({"error": "Failed to retrieve instances"}), 500
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


def _compute_bot_health(instances: list[dict]) -> str:
    """Compute overall bot health from instance statuses.

    Returns one of: 'running', 'degraded', 'stopped', 'unreachable'

    Logic:
    - No instances → 'stopped'
    - All running → 'running'
    - Some running, some not → 'degraded'
    - None running but some in error/failed → 'unreachable'
    - All stopped → 'stopped'
    """
    if not instances:
        return "stopped"

    statuses = [inst.get("status", "unknown") for inst in instances]

    running_count = statuses.count("running")
    error_count = statuses.count("error") + statuses.count("failed")
    stopped_count = statuses.count("stopped")

    if running_count == len(statuses):
        return "running"
    elif running_count > 0:
        return "degraded"
    elif error_count > 0:
        return "unreachable"
    elif stopped_count == len(statuses):
        return "stopped"
    else:
        # Provisioning or pending_resources
        return "degraded"


def _get_gpu_utilization() -> list[dict]:
    """Return GPU utilization per node.

    This is a placeholder implementation returning the node topology
    with dummy utilization data. In production, this would query:
    - Kubernetes device plugin allocations (intel.com/sriov-gpudevice)
    - Node-level metrics from Prometheus or custom exporter

    The gremlin nodes each have 7 allocatable SR-IOV GPU VFs.
    """
    nodes = [
        {
            "node": "gremlin-1",
            "ip": "10.1.1.12",
            "total_vfs": 7,
            "allocated_vfs": 0,
            "utilization_percent": 0,
            "has_nvidia": True,
        },
        {
            "node": "gremlin-2",
            "ip": "10.1.1.13",
            "total_vfs": 7,
            "allocated_vfs": 0,
            "utilization_percent": 0,
            "has_nvidia": False,
        },
        {
            "node": "gremlin-3",
            "ip": "10.1.1.14",
            "total_vfs": 7,
            "allocated_vfs": 0,
            "utilization_percent": 0,
            "has_nvidia": False,
        },
        {
            "node": "gremlin-4",
            "ip": "10.1.1.15",
            "total_vfs": 7,
            "allocated_vfs": 0,
            "utilization_percent": 0,
            "has_nvidia": False,
        },
    ]
    return nodes
