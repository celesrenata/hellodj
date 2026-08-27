"""Subscription management blueprint for the HelloDJ SaaS platform.

Exposes:
- GET    /api/v1/subscriptions      — list tenant's subscriptions
- POST   /api/v1/subscriptions      — create subscription, return PayPal redirect URL
- DELETE /api/v1/subscriptions/{id}  — cancel subscription
- POST   /api/v1/trials/apply        — submit trial application
- GET    /api/v1/trials/status       — check trial application status

All routes are protected by @login_required.

Requirements: 7.5, 7.6, 6.1
"""

from __future__ import annotations

import logging
import uuid

from flask import Blueprint, g, jsonify, request

from auth_middleware import login_required
from services.payment_gateway import generate_payment_url
from services.subscription_manager import (
    AddonPrerequisiteError,
    InvalidAddonError,
    InvalidPlanError,
    InvalidStateTransitionError,
    SubscriptionManager,
    SubscriptionNotFoundError,
)
from services import trial_manager
from services.trial_manager import TrialError

log = logging.getLogger(__name__)

subscriptions_bp = Blueprint("subscriptions", __name__)

# Shared SubscriptionManager instance
_subscription_manager: SubscriptionManager | None = None


def _get_subscription_manager() -> SubscriptionManager:
    """Return the module-level SubscriptionManager, creating one on first use."""
    global _subscription_manager
    if _subscription_manager is None:
        _subscription_manager = SubscriptionManager()
    return _subscription_manager


def _get_tenant_id() -> uuid.UUID | None:
    """Extract and validate the tenant_id from the authenticated session."""
    tenant = g.tenant
    tenant_id_str = tenant.get("id") or tenant.get("tenant_id")
    if not tenant_id_str:
        return None
    try:
        return uuid.UUID(str(tenant_id_str))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Subscription Routes
# ---------------------------------------------------------------------------


@subscriptions_bp.route("/api/v1/subscriptions", methods=["GET"])
@login_required
def list_subscriptions():
    """List all subscriptions for the authenticated tenant.

    Query params:
        status: Optional filter by subscription status.

    Returns:
        JSON array of subscription records.
    """
    tenant_id = _get_tenant_id()
    if tenant_id is None:
        return jsonify({"error": "Invalid or missing tenant ID"}), 401

    status_filter = request.args.get("status")
    sm = _get_subscription_manager()

    try:
        subscriptions = sm.get_tenant_subscriptions(tenant_id, status=status_filter)
    except Exception as exc:
        log.error("Failed to list subscriptions for tenant %s: %s", tenant_id, exc)
        return jsonify({"error": "Failed to retrieve subscriptions"}), 500

    # Serialize datetime and UUID fields for JSON response
    result = []
    for sub in subscriptions:
        result.append(_serialize_subscription(sub))

    return jsonify({"subscriptions": result}), 200


@subscriptions_bp.route("/api/v1/subscriptions", methods=["POST"])
@login_required
def create_subscription():
    """Create a new subscription and return a PayPal payment redirect URL.

    Request JSON body:
        plan: str — plan name ('base' or 'trial')
        addons: list[str] — optional list of addon names

    Returns:
        JSON with subscription record and PayPal redirect URL.
        HTTP 201 on success.
        HTTP 400 on validation errors.
        HTTP 409 on addon prerequisite failure.
    """
    tenant_id = _get_tenant_id()
    if tenant_id is None:
        return jsonify({"error": "Invalid or missing tenant ID"}), 401

    # Parse and validate request body
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    plan = data.get("plan")
    if not plan:
        return jsonify({"error": "Missing required field: plan"}), 400

    addons = data.get("addons", [])
    if not isinstance(addons, list):
        return jsonify({"error": "Field 'addons' must be a list"}), 400

    sm = _get_subscription_manager()

    try:
        subscription = sm.create_subscription(tenant_id, plan, addons)
    except InvalidPlanError as exc:
        return jsonify({"error": str(exc)}), 400
    except InvalidAddonError as exc:
        return jsonify({"error": str(exc)}), 400
    except AddonPrerequisiteError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        log.error(
            "Failed to create subscription for tenant %s: %s", tenant_id, exc
        )
        return jsonify({"error": "Failed to create subscription"}), 500

    # Generate PayPal payment URL
    try:
        payment_url = generate_payment_url(subscription)
    except Exception as exc:
        log.error(
            "Failed to generate payment URL for subscription %s: %s",
            subscription.get("id"),
            exc,
        )
        # Subscription was created but payment URL generation failed
        return jsonify({
            "error": "Subscription created but payment URL generation failed",
            "subscription": _serialize_subscription(subscription),
        }), 500

    log.info(
        "Subscription created: id=%s tenant=%s plan=%s addons=%s",
        subscription.get("id"),
        tenant_id,
        plan,
        addons,
    )

    return jsonify({
        "subscription": _serialize_subscription(subscription),
        "payment_url": payment_url,
    }), 201


@subscriptions_bp.route("/api/v1/subscriptions/<subscription_id>", methods=["DELETE"])
@login_required
def cancel_subscription(subscription_id: str):
    """Cancel a subscription owned by the authenticated tenant.

    Path params:
        subscription_id: UUID of the subscription to cancel.

    Returns:
        JSON with the updated subscription record.
        HTTP 200 on success.
        HTTP 400 on invalid subscription ID format.
        HTTP 403 if the subscription does not belong to the tenant.
        HTTP 404 if the subscription is not found.
        HTTP 409 if the subscription cannot be cancelled (already terminated).
    """
    tenant_id = _get_tenant_id()
    if tenant_id is None:
        return jsonify({"error": "Invalid or missing tenant ID"}), 401

    # Validate subscription_id format
    try:
        sub_uuid = uuid.UUID(subscription_id)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid subscription ID format"}), 400

    sm = _get_subscription_manager()

    # Verify ownership: the subscription must belong to this tenant
    try:
        existing = sm.get_subscription(sub_uuid)
    except Exception as exc:
        log.error("Failed to fetch subscription %s: %s", subscription_id, exc)
        return jsonify({"error": "Failed to verify subscription ownership"}), 500

    if existing is None:
        return jsonify({"error": "Subscription not found"}), 404

    # Compare tenant_id — handle both UUID objects and strings
    existing_tenant_id = existing.get("tenant_id")
    if str(existing_tenant_id) != str(tenant_id):
        log.warning(
            "Subscription cancel denied: tenant %s attempted to cancel subscription %s owned by %s",
            tenant_id,
            subscription_id,
            existing_tenant_id,
        )
        return jsonify({"error": "Subscription does not belong to this tenant"}), 403

    # Perform cancellation
    try:
        updated = sm.cancel(sub_uuid)
    except SubscriptionNotFoundError:
        return jsonify({"error": "Subscription not found"}), 404
    except InvalidStateTransitionError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        log.error(
            "Failed to cancel subscription %s: %s", subscription_id, exc
        )
        return jsonify({"error": "Failed to cancel subscription"}), 500

    log.info(
        "Subscription cancelled: id=%s tenant=%s",
        subscription_id,
        tenant_id,
    )

    return jsonify({"subscription": _serialize_subscription(updated)}), 200


# ---------------------------------------------------------------------------
# Trial Routes
# ---------------------------------------------------------------------------


@subscriptions_bp.route("/api/v1/trials/apply", methods=["POST"])
@login_required
def apply_for_trial():
    """Submit a trial application for the authenticated tenant.

    Returns:
        JSON with the created trial application record.
        HTTP 201 on success.
        HTTP 409 if the tenant already has an active trial or subscription.
    """
    tenant_id = _get_tenant_id()
    if tenant_id is None:
        return jsonify({"error": "Invalid or missing tenant ID"}), 401

    try:
        application = trial_manager.apply(str(tenant_id))
    except TrialError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:
        log.error(
            "Failed to create trial application for tenant %s: %s",
            tenant_id,
            exc,
        )
        return jsonify({"error": "Failed to submit trial application"}), 500

    log.info(
        "Trial application submitted: id=%s tenant=%s",
        application.get("id"),
        tenant_id,
    )

    return jsonify({"application": _serialize_trial_application(application)}), 201


@subscriptions_bp.route("/api/v1/trials/status", methods=["GET"])
@login_required
def trial_status():
    """Check the trial application status for the authenticated tenant.

    Returns:
        JSON with the tenant's trial application(s), ordered by applied_at DESC.
        Returns an empty list if no applications exist.
    """
    tenant_id = _get_tenant_id()
    if tenant_id is None:
        return jsonify({"error": "Invalid or missing tenant ID"}), 401

    try:
        applications = _get_tenant_trial_applications(tenant_id)
    except Exception as exc:
        log.error(
            "Failed to fetch trial status for tenant %s: %s", tenant_id, exc
        )
        return jsonify({"error": "Failed to retrieve trial status"}), 500

    result = [_serialize_trial_application(app) for app in applications]
    return jsonify({"applications": result}), 200


# ---------------------------------------------------------------------------
# Internal Helpers
# ---------------------------------------------------------------------------


def _get_tenant_trial_applications(tenant_id: uuid.UUID) -> list[dict]:
    """Query trial_applications for the given tenant, ordered by applied_at DESC."""
    import os

    import psycopg2
    import psycopg2.extras

    pg_uri = os.environ.get(
        "HELLODJ_PG_URI",
        "postgresql://hellodj:hellodj@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj",
    )
    conn = psycopg2.connect(pg_uri)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, tenant_id, status, applied_at, decided_at, decided_by
                FROM trial_applications
                WHERE tenant_id = %s
                ORDER BY applied_at DESC
                """,
                (str(tenant_id),),
            )
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _serialize_subscription(sub: dict) -> dict:
    """Convert a subscription record to JSON-serializable format."""
    result = {}
    for key, value in sub.items():
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


def _serialize_trial_application(app: dict) -> dict:
    """Convert a trial application record to JSON-serializable format."""
    result = {}
    for key, value in app.items():
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif hasattr(value, "isoformat"):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result
