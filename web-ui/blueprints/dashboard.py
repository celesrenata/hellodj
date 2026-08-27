"""Tenant dashboard blueprint for the HelloDJ SaaS platform.

Provides the authenticated tenant dashboard showing:
- Subscription overview (plan, addons, next billing date)
- Bot instance status cards (online/offline/restarting with 30s HTMX polling)
- Billing summary (recent payments)
- Registration confirmation for first-time users
- Trial apply / subscribe options when no active subscription

All routes are protected by @login_required.

Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 12.2, 12.5
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from flask import Blueprint, g, render_template, request

from auth_middleware import login_required

log = logging.getLogger(__name__)

dashboard_bp = Blueprint(
    "dashboard",
    __name__,
    url_prefix="/dashboard",
    template_folder="../templates",
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PG_URI = os.environ.get(
    "HELLODJ_PG_URI",
    "postgresql://hellodj:hellodj@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj",
)


def _get_pg_conn():
    """Get a psycopg2 connection to PostgreSQL."""
    return psycopg2.connect(PG_URI)


# ---------------------------------------------------------------------------
# Status color mapping for bot instances
# ---------------------------------------------------------------------------

STATUS_COLORS = {
    "running": "green",
    "provisioning": "yellow",
    "stopped": "gray",
    "error": "red",
    "pending_resources": "yellow",
    "failed": "red",
}

STATUS_LABELS = {
    "running": "Online",
    "provisioning": "Starting",
    "stopped": "Offline",
    "error": "Error",
    "pending_resources": "Waiting for Resources",
    "failed": "Failed",
}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@dashboard_bp.route("/", methods=["GET"])
@login_required
def index():
    """Main tenant dashboard page.

    Displays subscription overview, bot status cards, billing summary.
    Shows registration confirmation for first-time users (query param `?new=1`).
    Shows trial/subscribe options when no active subscription.

    Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 12.2, 12.5
    """
    tenant = g.tenant
    tenant_id = tenant["tenant_id"]
    is_new_user = request.args.get("new") == "1"

    conn = _get_pg_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Fetch active subscriptions for this tenant
            cur.execute(
                """
                SELECT id, plan, addons, status, started_at, expires_at, created_at
                FROM subscriptions
                WHERE tenant_id = %s
                  AND status IN ('active', 'pending_payment')
                ORDER BY created_at DESC
                """,
                (tenant_id,),
            )
            subscriptions = [dict(row) for row in cur.fetchall()]

            # Fetch bot instances for this tenant
            cur.execute(
                """
                SELECT id, guild_ids, status, node_name, pod_name, created_at
                FROM bot_instances
                WHERE tenant_id = %s
                ORDER BY created_at DESC
                """,
                (tenant_id,),
            )
            bot_instances = [dict(row) for row in cur.fetchall()]

            # Fetch recent payments (last 10)
            cur.execute(
                """
                SELECT id, paypal_txn_id, amount_cents, currency, status, created_at
                FROM payments
                WHERE tenant_id = %s
                ORDER BY created_at DESC
                LIMIT 10
                """,
                (tenant_id,),
            )
            recent_payments = [dict(row) for row in cur.fetchall()]

            # Check for pending trial application
            cur.execute(
                """
                SELECT id, status, applied_at
                FROM trial_applications
                WHERE tenant_id = %s
                ORDER BY applied_at DESC
                LIMIT 1
                """,
                (tenant_id,),
            )
            trial_application = cur.fetchone()
            if trial_application:
                trial_application = dict(trial_application)

    finally:
        conn.close()

    # Determine active subscription (first active one)
    active_subscription = next(
        (s for s in subscriptions if s["status"] == "active"), None
    )

    # Determine if tenant has no active subscription or trial
    has_active = active_subscription is not None
    pending_trial = (
        trial_application and trial_application["status"] == "pending"
    )

    # Enrich bot instances with color/label info
    for bot in bot_instances:
        bot["status_color"] = STATUS_COLORS.get(bot["status"], "gray")
        bot["status_label"] = STATUS_LABELS.get(bot["status"], bot["status"])

    # Compute next billing date from active subscription
    next_billing_date = None
    if active_subscription:
        if active_subscription.get("expires_at"):
            next_billing_date = active_subscription["expires_at"]

    return render_template(
        "pages/dashboard.html",
        tenant=tenant,
        is_new_user=is_new_user,
        active_subscription=active_subscription,
        subscriptions=subscriptions,
        bot_instances=bot_instances,
        recent_payments=recent_payments,
        trial_application=trial_application,
        has_active=has_active,
        pending_trial=pending_trial,
        next_billing_date=next_billing_date,
        status_colors=STATUS_COLORS,
        status_labels=STATUS_LABELS,
        active="dashboard",
    )


@dashboard_bp.route("/bot-status", methods=["GET"])
@login_required
def bot_status_partial():
    """HTMX partial: returns bot instance status cards.

    Polled every 30 seconds via hx-trigger="every 30s" to update
    bot instance status without full page reload.

    Requirements: 12.5
    """
    tenant = g.tenant
    tenant_id = tenant["tenant_id"]

    conn = _get_pg_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, guild_ids, status, node_name, pod_name, created_at
                FROM bot_instances
                WHERE tenant_id = %s
                ORDER BY created_at DESC
                """,
                (tenant_id,),
            )
            bot_instances = [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()

    # Enrich with color/label
    for bot in bot_instances:
        bot["status_color"] = STATUS_COLORS.get(bot["status"], "gray")
        bot["status_label"] = STATUS_LABELS.get(bot["status"], bot["status"])

    return render_template(
        "partials/bot_status_cards.html",
        bot_instances=bot_instances,
    )
