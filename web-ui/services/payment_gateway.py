"""PayPal Payment Gateway for the HelloDJ SaaS platform.

Handles:
- PayPal payment URL generation for subscriptions
- IPN (Instant Payment Notification) verification with PayPal
- Payment record creation in PostgreSQL
- Consecutive failure tracking for manual review flagging
- Billing history retrieval (paginated)

IPN Verification Flow:
1. Receive IPN POST from PayPal
2. Echo back payload to PayPal's verification endpoint with cmd=_notify-validate
3. If VERIFIED: create payment record, notify Subscription Manager
4. If INVALID or timeout (30s): discard, log failure
5. If 3 consecutive failures for same txn: flag for manual review

Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.9
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
import psycopg2
import psycopg2.extras
import psycopg2.extensions

log = logging.getLogger(__name__)

# Register UUID adapter for psycopg2
psycopg2.extensions.register_adapter(
    uuid.UUID, lambda u: psycopg2.extensions.AsIs(f"'{u}'")
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PAYPAL_MODE = os.environ.get("PAYPAL_MODE", "sandbox")
PAYPAL_BUSINESS_EMAIL = os.environ.get("PAYPAL_BUSINESS_EMAIL", "celes@frameshift.net")

# PayPal IPN verification URLs
PAYPAL_IPN_URLS = {
    "sandbox": "https://ipnpb.sandbox.paypal.com/cgi-bin/webscr",
    "live": "https://ipnpb.paypal.com/cgi-bin/webscr",
}

# PayPal payment page URLs (where users are redirected to pay)
PAYPAL_PAYMENT_URLS = {
    "sandbox": "https://www.sandbox.paypal.com/cgi-bin/webscr",
    "live": "https://www.paypal.com/cgi-bin/webscr",
}

# IPN verification timeout in seconds
IPN_VERIFY_TIMEOUT = 30.0

# Consecutive failures before flagging for manual review
MAX_CONSECUTIVE_FAILURES = 3

# Base URL for return/cancel redirects
BASE_URL = os.environ.get("HELLODJ_BASE_URL", "https://hellodj.celestium.life")

PG_URI = os.environ.get(
    "HELLODJ_PG_URI",
    "postgresql://hellodj:hellodj@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj",
)


def _get_pg_conn():
    """Get a psycopg2 connection to PostgreSQL."""
    return psycopg2.connect(PG_URI)


def set_pg_uri(uri: str) -> None:
    """Override the PG URI (for testing)."""
    global PG_URI
    PG_URI = uri


# ---------------------------------------------------------------------------
# In-memory failure tracker (per-process; for clustered deployments, use Redis)
# ---------------------------------------------------------------------------

# Maps paypal_txn_id → consecutive failure count
_failure_counts: dict[str, int] = {}
# Set of txn_ids flagged for manual review
_flagged_txns: set[str] = set()


# ---------------------------------------------------------------------------
# Payment URL Generation
# ---------------------------------------------------------------------------


def generate_payment_url(
    subscription: dict[str, Any],
    return_url: str | None = None,
    cancel_url: str | None = None,
) -> str:
    """Generate a PayPal payment redirect URL for a subscription.

    Constructs a PayPal Payments Standard URL with the correct amount
    based on the subscription's plan and addons.

    Args:
        subscription: Subscription dict with keys: id, tenant_id, plan, addons.
        return_url: Optional override for success redirect URL.
        cancel_url: Optional override for cancel redirect URL.

    Returns:
        Full PayPal redirect URL.
    """
    from services.subscription_manager import SubscriptionManager

    sm = SubscriptionManager()
    plan = subscription.get("plan", "base")
    addons = subscription.get("addons") or []
    total_cents = sm.compute_total_price_cents(plan, addons)

    # Convert cents to dollars (2 decimal places)
    amount = f"{total_cents / 100:.2f}"

    subscription_id = subscription.get("id", "")

    # Build item description
    addon_str = f" + {', '.join(addons)}" if addons else ""
    item_name = f"HelloDJ {plan.title()} Plan{addon_str}"

    # PayPal Payments Standard parameters
    params = {
        "cmd": "_xclick",
        "business": PAYPAL_BUSINESS_EMAIL,
        "item_name": item_name,
        "amount": amount,
        "currency_code": "USD",
        "custom": str(subscription_id),
        "invoice": str(subscription_id),
        "notify_url": f"{BASE_URL}/api/v1/payments/ipn",
        "return": return_url or f"{BASE_URL}/api/v1/payments/success",
        "cancel_return": cancel_url or f"{BASE_URL}/api/v1/payments/cancel",
        "no_shipping": "1",
        "no_note": "1",
    }

    payment_base_url = PAYPAL_PAYMENT_URLS.get(PAYPAL_MODE, PAYPAL_PAYMENT_URLS["sandbox"])
    url = f"{payment_base_url}?{urlencode(params)}"

    log.info(
        "Generated PayPal payment URL: subscription=%s amount=%s plan=%s",
        subscription_id, amount, plan,
    )

    return url


# ---------------------------------------------------------------------------
# IPN Verification
# ---------------------------------------------------------------------------


def verify_ipn(ipn_data: dict[str, str]) -> str:
    """Verify an IPN notification with PayPal.

    Echoes back the full IPN payload with cmd=_notify-validate prepended.
    PayPal responds with VERIFIED or INVALID.

    Args:
        ipn_data: The raw IPN form data as a dict.

    Returns:
        "VERIFIED", "INVALID", or "TIMEOUT" if PayPal doesn't respond in 30s.
    """
    verify_url = PAYPAL_IPN_URLS.get(PAYPAL_MODE, PAYPAL_IPN_URLS["sandbox"])

    # Prepend the validation command
    verify_payload = "cmd=_notify-validate&" + urlencode(ipn_data)

    try:
        with httpx.Client(timeout=IPN_VERIFY_TIMEOUT) as client:
            response = client.post(
                verify_url,
                content=verify_payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            body = response.text.strip()
            log.debug("PayPal IPN verification response: %s", body)

            if body == "VERIFIED":
                return "VERIFIED"
            elif body == "INVALID":
                return "INVALID"
            else:
                log.warning("Unexpected PayPal IPN response: %r", body)
                return "INVALID"

    except httpx.TimeoutException:
        log.warning("PayPal IPN verification timed out after %ss", IPN_VERIFY_TIMEOUT)
        return "TIMEOUT"
    except httpx.HTTPError as exc:
        log.error("PayPal IPN verification HTTP error: %s", exc)
        return "TIMEOUT"


# ---------------------------------------------------------------------------
# IPN Processing
# ---------------------------------------------------------------------------


def process_ipn(ipn_data: dict[str, str]) -> dict[str, Any]:
    """Process a verified IPN notification.

    Handles the full IPN flow:
    1. Verify with PayPal
    2. On VERIFIED: create payment record, notify subscription manager
    3. On INVALID/TIMEOUT: log failure, track consecutive failures
    4. On 3 consecutive failures for same txn: flag for manual review

    Args:
        ipn_data: The raw IPN form data.

    Returns:
        Dict with keys: status ("ok", "invalid", "timeout", "flagged"),
        and optionally payment_id, txn_id.
    """
    txn_id = ipn_data.get("txn_id", "")
    payment_status = ipn_data.get("payment_status", "")

    log.info(
        "Processing IPN: txn_id=%s payment_status=%s",
        txn_id, payment_status,
    )

    # Skip if already flagged for manual review
    if txn_id in _flagged_txns:
        log.warning("IPN for flagged txn_id=%s — requires manual review", txn_id)
        return {"status": "flagged", "txn_id": txn_id}

    # Verify with PayPal
    verification = verify_ipn(ipn_data)

    if verification == "VERIFIED":
        # Reset failure counter on success
        _failure_counts.pop(txn_id, None)

        # Only process Completed payments
        if payment_status != "Completed":
            log.info(
                "IPN verified but payment_status=%s (not Completed), skipping",
                payment_status,
            )
            return {"status": "ok", "txn_id": txn_id, "note": "non-completed status"}

        # Create payment record and activate subscription
        result = _handle_verified_payment(ipn_data)
        return result

    else:
        # INVALID or TIMEOUT — track failure
        count = _failure_counts.get(txn_id, 0) + 1
        _failure_counts[txn_id] = count

        log.warning(
            "IPN verification %s for txn_id=%s (failure #%d)",
            verification, txn_id, count,
        )

        if count >= MAX_CONSECUTIVE_FAILURES:
            _flagged_txns.add(txn_id)
            log.error(
                "IPN txn_id=%s flagged for manual review after %d consecutive failures",
                txn_id, count,
            )
            return {"status": "flagged", "txn_id": txn_id}

        return {"status": verification.lower(), "txn_id": txn_id}


def _handle_verified_payment(ipn_data: dict[str, str]) -> dict[str, Any]:
    """Create a payment record and notify the subscription manager.

    Args:
        ipn_data: Verified IPN form data.

    Returns:
        Dict with status and payment details.
    """
    from services.subscription_manager import SubscriptionManager

    txn_id = ipn_data.get("txn_id", "")
    mc_gross = ipn_data.get("mc_gross", "0.00")
    mc_currency = ipn_data.get("mc_currency", "USD")
    custom = ipn_data.get("custom", "")  # subscription_id passed as custom field

    # Convert amount to cents
    try:
        amount_cents = int(float(mc_gross) * 100)
    except (ValueError, TypeError):
        log.error("Invalid mc_gross in IPN: %r", mc_gross)
        amount_cents = 0

    if amount_cents <= 0:
        log.error("IPN has non-positive amount: txn_id=%s mc_gross=%s", txn_id, mc_gross)
        return {"status": "error", "txn_id": txn_id, "error": "invalid amount"}

    # Resolve tenant_id from subscription
    tenant_id = _resolve_tenant_from_subscription(custom)
    if tenant_id is None:
        log.error(
            "Cannot resolve tenant for IPN: txn_id=%s custom=%s",
            txn_id, custom,
        )
        return {"status": "error", "txn_id": txn_id, "error": "unknown subscription"}

    # Create payment record
    payment_id = _create_payment_record(
        tenant_id=tenant_id,
        paypal_txn_id=txn_id,
        amount_cents=amount_cents,
        currency=mc_currency,
        status="completed",
    )

    if payment_id is None:
        # Likely a duplicate txn_id (unique constraint)
        log.warning("Duplicate payment txn_id=%s — skipping", txn_id)
        return {"status": "duplicate", "txn_id": txn_id}

    # Notify Subscription Manager to activate
    try:
        subscription_id = uuid.UUID(custom)
        sm = SubscriptionManager()
        sm.activate(subscription_id)
        log.info(
            "Subscription activated via IPN: subscription=%s tenant=%s",
            subscription_id, tenant_id,
        )
    except Exception as exc:
        log.error(
            "Failed to activate subscription %s after payment: %s",
            custom, exc,
        )

    return {
        "status": "ok",
        "txn_id": txn_id,
        "payment_id": str(payment_id),
    }


def _resolve_tenant_from_subscription(subscription_id_str: str) -> uuid.UUID | None:
    """Look up the tenant_id for a subscription.

    Args:
        subscription_id_str: The subscription UUID as a string (from IPN custom field).

    Returns:
        tenant_id UUID or None if not found.
    """
    try:
        subscription_id = uuid.UUID(subscription_id_str)
    except (ValueError, TypeError):
        return None

    conn = _get_pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT tenant_id FROM subscriptions WHERE id = %s",
                (subscription_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as exc:
        log.error("DB error resolving tenant from subscription: %s", exc)
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Payment Record CRUD
# ---------------------------------------------------------------------------


def _create_payment_record(
    tenant_id: uuid.UUID,
    paypal_txn_id: str,
    amount_cents: int,
    currency: str,
    status: str,
) -> uuid.UUID | None:
    """Insert a payment record into PostgreSQL.

    Returns:
        The payment UUID on success, None if duplicate (unique constraint violation).
    """
    payment_id = uuid.uuid4()
    conn = _get_pg_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO payments (id, tenant_id, paypal_txn_id, amount_cents, currency, status)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (paypal_txn_id) DO NOTHING
                RETURNING id
                """,
                (payment_id, tenant_id, paypal_txn_id, amount_cents, currency, status),
            )
            row = cur.fetchone()
            conn.commit()
            if row:
                log.info(
                    "Created payment record: id=%s txn=%s amount=%d%s tenant=%s",
                    payment_id, paypal_txn_id, amount_cents, currency, tenant_id,
                )
                return row[0]
            return None
    except Exception as exc:
        conn.rollback()
        log.error("Failed to create payment record: %s", exc)
        return None
    finally:
        conn.close()


def get_billing_history(
    tenant_id: uuid.UUID,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Retrieve paginated billing history for a tenant.

    Args:
        tenant_id: The tenant UUID.
        limit: Maximum records per page (capped at 100).
        offset: Number of records to skip.

    Returns:
        Dict with keys: payments (list), total (int), limit, offset.
    """
    # Cap limit at 100
    limit = min(max(1, limit), 100)
    offset = max(0, offset)

    conn = _get_pg_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Get total count
            cur.execute(
                "SELECT COUNT(*) as total FROM payments WHERE tenant_id = %s",
                (tenant_id,),
            )
            total = cur.fetchone()["total"]

            # Get paginated records
            cur.execute(
                """
                SELECT id, tenant_id, paypal_txn_id, amount_cents, currency, status, created_at
                FROM payments
                WHERE tenant_id = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (tenant_id, limit, offset),
            )
            rows = cur.fetchall()

            payments = []
            for row in rows:
                payment = dict(row)
                # Convert UUID and datetime to strings for JSON serialization
                payment["id"] = str(payment["id"])
                payment["tenant_id"] = str(payment["tenant_id"])
                if payment["created_at"]:
                    payment["created_at"] = payment["created_at"].isoformat()
                payments.append(payment)

            return {
                "payments": payments,
                "total": total,
                "limit": limit,
                "offset": offset,
            }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Manual Review Helpers
# ---------------------------------------------------------------------------


def get_flagged_transactions() -> list[str]:
    """Return list of transaction IDs flagged for manual review."""
    return list(_flagged_txns)


def clear_flag(txn_id: str) -> bool:
    """Remove a transaction from the flagged set (after manual resolution).

    Returns True if the txn was flagged and is now cleared.
    """
    if txn_id in _flagged_txns:
        _flagged_txns.discard(txn_id)
        _failure_counts.pop(txn_id, None)
        log.info("Cleared manual review flag for txn_id=%s", txn_id)
        return True
    return False
