"""Payments blueprint for the HelloDJ SaaS platform.

Exposes:
- POST /api/v1/payments/ipn      — PayPal IPN webhook receiver
- GET  /api/v1/payments          — Paginated billing history
- GET  /api/v1/payments/success  — Success redirect from PayPal
- GET  /api/v1/payments/cancel   — Cancel redirect from PayPal

The IPN endpoint is publicly accessible (PayPal sends POSTs without auth).
The billing history endpoint requires authentication.

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.9
"""

from __future__ import annotations

import logging
import uuid

from flask import Blueprint, jsonify, redirect, request

from auth_middleware import login_required
from services.payment_gateway import (
    generate_payment_url,
    get_billing_history,
    process_ipn,
)

log = logging.getLogger(__name__)

payments_bp = Blueprint("payments", __name__, url_prefix="/api/v1/payments")


# ---------------------------------------------------------------------------
# IPN Webhook (publicly accessible — PayPal sends POSTs here)
# ---------------------------------------------------------------------------


@payments_bp.route("/ipn", methods=["POST"])
def ipn_webhook():
    """Receive and process PayPal IPN notifications.

    PayPal sends form-encoded POST data. We echo it back for verification,
    then process the result. Always return 200 to PayPal regardless of
    our internal processing result (PayPal retries on non-200).
    """
    # Parse form data from PayPal
    ipn_data = request.form.to_dict()

    if not ipn_data:
        log.warning("Empty IPN received")
        return "", 200

    txn_id = ipn_data.get("txn_id", "unknown")
    log.info("IPN received: txn_id=%s payment_status=%s",
             txn_id, ipn_data.get("payment_status", ""))

    # Process the IPN (verify + handle)
    result = process_ipn(ipn_data)

    log.info("IPN processed: txn_id=%s result=%s", txn_id, result.get("status"))

    # Always return 200 to PayPal — they retry on failure codes
    return "", 200


# ---------------------------------------------------------------------------
# Billing History (authenticated)
# ---------------------------------------------------------------------------


@payments_bp.route("", methods=["GET"])
@login_required
def billing_history():
    """Return paginated billing history for the authenticated tenant.

    Query params:
        limit: int (default 100, max 100)
        offset: int (default 0)

    Returns:
        JSON with payments array, total count, limit, and offset.
    """
    from flask import g

    tenant = g.tenant
    tenant_id_str = tenant.get("id") or tenant.get("tenant_id")

    if not tenant_id_str:
        return jsonify({"error": "No tenant ID in session"}), 401

    try:
        tenant_id = uuid.UUID(str(tenant_id_str))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid tenant ID"}), 400

    # Parse pagination params
    try:
        limit = int(request.args.get("limit", 100))
    except (ValueError, TypeError):
        limit = 100

    try:
        offset = int(request.args.get("offset", 0))
    except (ValueError, TypeError):
        offset = 0

    result = get_billing_history(tenant_id, limit=limit, offset=offset)
    return jsonify(result), 200


# ---------------------------------------------------------------------------
# PayPal Redirects (user-facing)
# ---------------------------------------------------------------------------


@payments_bp.route("/success", methods=["GET"])
def payment_success():
    """Handle successful PayPal payment redirect.

    PayPal redirects the user here after completing payment. The actual
    payment confirmation comes via IPN (asynchronous), so we just show
    a success message.
    """
    log.info("Payment success redirect received")
    # Redirect to dashboard with success indication
    return redirect("/dashboard?payment=success")


@payments_bp.route("/cancel", methods=["GET"])
def payment_cancel():
    """Handle PayPal payment cancellation redirect.

    User cancelled payment on the PayPal page. No payment record is created,
    subscription status is not modified.
    """
    log.info("Payment cancel redirect received")
    # Redirect to dashboard with cancel indication
    return redirect("/dashboard?payment=cancelled")
