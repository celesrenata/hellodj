"""Subscription Manager for the HelloDJ SaaS platform.

Manages subscription lifecycle: plan creation, activation, expiration, cancellation.
Enforces addon prerequisites (Base_Plan required), auto-cancel on payment timeout,
and 3-day grace period before bot deactivation on expiry.

Status flow: pending_payment → active → expired/cancelled

Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 7.9, 7.10
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.extensions

log = logging.getLogger(__name__)

# Register UUID adapter so psycopg2 can handle uuid.UUID objects directly
psycopg2.extensions.register_adapter(
    uuid.UUID, lambda u: psycopg2.extensions.AsIs(f"'{u}'")
)

# ---------------------------------------------------------------------------
# Plan and Addon Definitions
# ---------------------------------------------------------------------------

PLANS: dict[str, dict[str, Any]] = {
    "base": {
        "price_cents": 699,
        "bot_instances": 1,
        "features": ["audio"],
    },
    "trial": {
        "price_cents": 0,
        "bot_instances": 1,
        "features": ["audio"],
        "duration_days": 30,
    },
}

ADDONS: dict[str, dict[str, Any]] = {
    "video": {
        "price_cents": 199,
        "features": ["video", "activity", "hls", "visualizer"],
    },
    "premium": {
        "price_cents": 199,
        "features": ["tidal_hifi", "lossless", "priority_queue"],
    },
    "additional_bot": {
        "price_cents": 199,
        "per_instance": True,
        "max": 9,
    },
}

# Auto-cancel timeout: payment must be verified within 24 hours
PAYMENT_TIMEOUT_HOURS = 24
# Grace period: bot deactivation deferred for 3 days after expiry
GRACE_PERIOD_DAYS = 3

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


def set_pg_uri(uri: str) -> None:
    """Override the PG URI (for testing)."""
    global PG_URI
    PG_URI = uri


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SubscriptionError(Exception):
    """Base exception for subscription operations."""
    pass


class AddonPrerequisiteError(SubscriptionError):
    """Raised when addon subscription is attempted without an active Base_Plan."""
    pass


class InvalidPlanError(SubscriptionError):
    """Raised when an invalid plan name is provided."""
    pass


class InvalidAddonError(SubscriptionError):
    """Raised when an invalid addon name is provided."""
    pass


class SubscriptionNotFoundError(SubscriptionError):
    """Raised when a subscription ID does not exist."""
    pass


class InvalidStateTransitionError(SubscriptionError):
    """Raised when a status transition is not valid."""
    pass


# ---------------------------------------------------------------------------
# SubscriptionManager
# ---------------------------------------------------------------------------


class SubscriptionManager:
    """Manages subscription lifecycle for the HelloDJ SaaS platform.

    Plans:
      - Base: $6.99/mo, 1 bot instance, audio only
      - Trial: free, 30 days, audio only

    Addons (require active Base_Plan):
      - Video: +$1.99/mo — video, activity, hls, visualizer
      - Premium: +$1.99/mo — tidal_hifi, lossless, priority_queue
      - Additional Bot: +$1.99/mo per instance, max 9
    """

    PLANS = PLANS
    ADDONS = ADDONS

    def __init__(self, pg_uri: str | None = None):
        """Initialize with an optional PostgreSQL URI override."""
        if pg_uri:
            self._pg_uri = pg_uri
        else:
            self._pg_uri = PG_URI

    def _get_conn(self):
        """Get a database connection."""
        return psycopg2.connect(self._pg_uri)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_subscription(
        self,
        tenant_id: uuid.UUID,
        plan: str,
        addons: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new subscription with status 'pending_payment'.

        Args:
            tenant_id: The tenant UUID.
            plan: Plan name ('base' or 'trial').
            addons: List of addon names (e.g., ['video', 'premium']).

        Returns:
            The created subscription record as a dict.

        Raises:
            InvalidPlanError: If the plan is not a recognized plan name.
            InvalidAddonError: If any addon is not a recognized addon name.
            AddonPrerequisiteError: If addons are requested without an
                active Base_Plan subscription for the tenant.
        """
        addons = addons or []

        # Validate plan
        if plan not in self.PLANS:
            raise InvalidPlanError(f"Invalid plan: {plan!r}. Valid plans: {list(self.PLANS.keys())}")

        # Validate addons
        for addon in addons:
            if addon not in self.ADDONS:
                raise InvalidAddonError(
                    f"Invalid addon: {addon!r}. Valid addons: {list(self.ADDONS.keys())}"
                )

        # Enforce addon prerequisite: addons require active Base_Plan
        if addons:
            if not self._has_active_base_plan(tenant_id):
                raise AddonPrerequisiteError(
                    "An active Base_Plan subscription is required before adding add-ons. "
                    "Please subscribe to the Base plan first."
                )

        # Determine expiry for trial plans
        now = datetime.now(timezone.utc)
        expires_at = None
        if plan == "trial":
            expires_at = now + timedelta(days=self.PLANS["trial"]["duration_days"])

        subscription_id = uuid.uuid4()

        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO subscriptions (id, tenant_id, plan, addons, status, started_at, expires_at)
                    VALUES (%s, %s, %s, %s, 'pending_payment', %s, %s)
                    RETURNING id, tenant_id, plan, addons, status, started_at, expires_at, created_at
                    """,
                    (
                        subscription_id,
                        tenant_id,
                        plan,
                        addons,
                        now,
                        expires_at,
                    ),
                )
                row = cur.fetchone()
                conn.commit()
                log.info(
                    "Created subscription: id=%s tenant=%s plan=%s addons=%s",
                    subscription_id, tenant_id, plan, addons,
                )
                return dict(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def activate(self, subscription_id: uuid.UUID) -> dict[str, Any]:
        """Activate a subscription and trigger bot provisioning.

        Transitions status from 'pending_payment' to 'active'.

        Args:
            subscription_id: The subscription UUID to activate.

        Returns:
            The updated subscription record.

        Raises:
            SubscriptionNotFoundError: If the subscription does not exist.
            InvalidStateTransitionError: If the subscription is not in
                'pending_payment' status.
        """
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                # Lock the row for update
                cur.execute(
                    "SELECT * FROM subscriptions WHERE id = %s FOR UPDATE",
                    (subscription_id,),
                )
                row = cur.fetchone()

                if row is None:
                    raise SubscriptionNotFoundError(
                        f"Subscription not found: {subscription_id}"
                    )

                if row["status"] != "pending_payment":
                    raise InvalidStateTransitionError(
                        f"Cannot activate subscription in '{row['status']}' status. "
                        f"Only 'pending_payment' subscriptions can be activated."
                    )

                now = datetime.now(timezone.utc)
                cur.execute(
                    """
                    UPDATE subscriptions
                    SET status = 'active', started_at = %s
                    WHERE id = %s
                    RETURNING id, tenant_id, plan, addons, status, started_at, expires_at, created_at
                    """,
                    (now, subscription_id),
                )
                updated = cur.fetchone()
                conn.commit()

                log.info(
                    "Activated subscription: id=%s tenant=%s plan=%s",
                    subscription_id, updated["tenant_id"], updated["plan"],
                )

                # Trigger bot provisioning (interface only — actual provisioning
                # is handled by Bot Orchestrator in Task 12)
                self._trigger_bot_provisioning(dict(updated))

                return dict(updated)
        except (SubscriptionNotFoundError, InvalidStateTransitionError):
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def expire(self, subscription_id: uuid.UUID) -> dict[str, Any]:
        """Expire a subscription with 3-day grace period before bot deactivation.

        Transitions status from 'active' to 'expired'. Bot instances are
        deactivated after the 3-day grace period (expires_at + 3 days).

        Args:
            subscription_id: The subscription UUID to expire.

        Returns:
            The updated subscription record.

        Raises:
            SubscriptionNotFoundError: If the subscription does not exist.
            InvalidStateTransitionError: If the subscription is not in
                'active' status.
        """
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM subscriptions WHERE id = %s FOR UPDATE",
                    (subscription_id,),
                )
                row = cur.fetchone()

                if row is None:
                    raise SubscriptionNotFoundError(
                        f"Subscription not found: {subscription_id}"
                    )

                if row["status"] != "active":
                    raise InvalidStateTransitionError(
                        f"Cannot expire subscription in '{row['status']}' status. "
                        f"Only 'active' subscriptions can be expired."
                    )

                now = datetime.now(timezone.utc)
                # Grace period: bot deactivation deferred by 3 days
                grace_expiry = now + timedelta(days=GRACE_PERIOD_DAYS)

                cur.execute(
                    """
                    UPDATE subscriptions
                    SET status = 'expired', expires_at = %s
                    WHERE id = %s
                    RETURNING id, tenant_id, plan, addons, status, started_at, expires_at, created_at
                    """,
                    (grace_expiry, subscription_id),
                )
                updated = cur.fetchone()
                conn.commit()

                log.info(
                    "Expired subscription: id=%s tenant=%s grace_until=%s",
                    subscription_id, updated["tenant_id"], grace_expiry,
                )

                # Schedule bot deactivation after grace period
                # (interface only — actual deactivation handled by Bot Orchestrator)
                self._schedule_bot_deactivation(dict(updated), grace_expiry)

                return dict(updated)
        except (SubscriptionNotFoundError, InvalidStateTransitionError):
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def cancel(self, subscription_id: uuid.UUID) -> dict[str, Any]:
        """Cancel a subscription immediately.

        Transitions status to 'cancelled' from any active or pending state.

        Args:
            subscription_id: The subscription UUID to cancel.

        Returns:
            The updated subscription record.

        Raises:
            SubscriptionNotFoundError: If the subscription does not exist.
            InvalidStateTransitionError: If the subscription is already
                cancelled or expired.
        """
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM subscriptions WHERE id = %s FOR UPDATE",
                    (subscription_id,),
                )
                row = cur.fetchone()

                if row is None:
                    raise SubscriptionNotFoundError(
                        f"Subscription not found: {subscription_id}"
                    )

                if row["status"] in ("cancelled", "expired"):
                    raise InvalidStateTransitionError(
                        f"Cannot cancel subscription in '{row['status']}' status. "
                        f"Subscription is already terminated."
                    )

                cur.execute(
                    """
                    UPDATE subscriptions
                    SET status = 'cancelled'
                    WHERE id = %s
                    RETURNING id, tenant_id, plan, addons, status, started_at, expires_at, created_at
                    """,
                    (subscription_id,),
                )
                updated = cur.fetchone()
                conn.commit()

                log.info(
                    "Cancelled subscription: id=%s tenant=%s",
                    subscription_id, updated["tenant_id"],
                )

                return dict(updated)
        except (SubscriptionNotFoundError, InvalidStateTransitionError):
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def auto_cancel_expired_pending(self) -> list[dict[str, Any]]:
        """Auto-cancel subscriptions that have been pending_payment for over 24 hours.

        This should be called periodically (e.g., via a background task or
        checked at query time).

        Returns:
            List of cancelled subscription records.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=PAYMENT_TIMEOUT_HOURS)

        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE subscriptions
                    SET status = 'cancelled'
                    WHERE status = 'pending_payment'
                      AND created_at < %s
                    RETURNING id, tenant_id, plan, addons, status, started_at, expires_at, created_at
                    """,
                    (cutoff,),
                )
                cancelled = cur.fetchall()
                conn.commit()

                if cancelled:
                    log.info(
                        "Auto-cancelled %d subscription(s) past 24h payment timeout",
                        len(cancelled),
                    )
                    for sub in cancelled:
                        log.info(
                            "  Auto-cancelled: id=%s tenant=%s created_at=%s",
                            sub["id"], sub["tenant_id"], sub["created_at"],
                        )

                return [dict(row) for row in cancelled]
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_subscription(self, subscription_id: uuid.UUID) -> dict[str, Any] | None:
        """Retrieve a subscription by ID.

        Returns:
            The subscription record as a dict, or None if not found.
        """
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM subscriptions WHERE id = %s",
                    (subscription_id,),
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            conn.close()

    def get_tenant_subscriptions(
        self, tenant_id: uuid.UUID, status: str | None = None
    ) -> list[dict[str, Any]]:
        """Retrieve all subscriptions for a tenant, optionally filtered by status.

        Args:
            tenant_id: The tenant UUID.
            status: Optional status filter.

        Returns:
            List of subscription records.
        """
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                if status:
                    cur.execute(
                        "SELECT * FROM subscriptions WHERE tenant_id = %s AND status = %s ORDER BY created_at DESC",
                        (tenant_id, status),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM subscriptions WHERE tenant_id = %s ORDER BY created_at DESC",
                        (tenant_id,),
                    )
                rows = cur.fetchall()
                return [dict(row) for row in rows]
        finally:
            conn.close()

    def compute_total_price_cents(self, plan: str, addons: list[str]) -> int:
        """Compute the total monthly price in cents for a plan + addons.

        Args:
            plan: Plan name.
            addons: List of addon names.

        Returns:
            Total price in cents.
        """
        total = self.PLANS.get(plan, {}).get("price_cents", 0)
        for addon in addons:
            addon_info = self.ADDONS.get(addon, {})
            total += addon_info.get("price_cents", 0)
        return total

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _has_active_base_plan(self, tenant_id: uuid.UUID) -> bool:
        """Check if the tenant has an active Base_Plan subscription.

        Returns True if there is at least one subscription with plan='base'
        and status='active' for the given tenant.
        """
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM subscriptions
                        WHERE tenant_id = %s AND plan = 'base' AND status = 'active'
                    )
                    """,
                    (tenant_id,),
                )
                return cur.fetchone()[0]
        finally:
            conn.close()

    def _trigger_bot_provisioning(self, subscription: dict[str, Any]) -> None:
        """Signal the bot orchestrator to provision instances.

        This is an interface stub — actual implementation in Task 12.
        The Bot Orchestrator will handle Pod creation via the Kubernetes API.
        """
        log.info(
            "Bot provisioning triggered for subscription %s (tenant=%s, plan=%s)",
            subscription["id"],
            subscription["tenant_id"],
            subscription["plan"],
        )

    def _schedule_bot_deactivation(
        self, subscription: dict[str, Any], grace_expiry: datetime
    ) -> None:
        """Schedule bot instance deactivation after grace period.

        This is an interface stub — actual implementation in Task 12.
        The Bot Orchestrator will handle graceful Pod termination.
        """
        log.info(
            "Bot deactivation scheduled for subscription %s after grace period (until %s)",
            subscription["id"],
            grace_expiry.isoformat(),
        )
