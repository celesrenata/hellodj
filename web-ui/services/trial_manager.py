"""Trial Manager for HelloDJ SaaS platform.

Manages the 30-day early access trial lifecycle:
- apply(tenant_id)      → create trial_application with status 'pending'
- approve(application_id, decided_by) → activate 30-day trial with Base_Plan features
- deny(application_id, decided_by)    → set status 'rejected'

Business rules:
- Reject apply() if tenant already has an active trial or active subscription
- Trial subscriptions expire 30 days from approval (expires_at = now() + 30 days)
- Actual enforcement of expiry is handled at query time or via background task

Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta

import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)

PG_URI = os.environ.get(
    "HELLODJ_PG_URI",
    "postgresql://hellodj:hellodj@postgresql-rw.postgresql-service.svc.cluster.local:5432/hellodj",
)

# Trial duration in days
TRIAL_DURATION_DAYS = 30


class TrialError(Exception):
    """Raised when a trial operation cannot be completed due to business rules."""


def _get_pg_conn():
    """Get a psycopg2 connection to PostgreSQL."""
    return psycopg2.connect(PG_URI)


def apply(tenant_id: str) -> dict:
    """Create a trial application with status 'pending'.

    Args:
        tenant_id: UUID of the tenant applying for a trial.

    Returns:
        Dict with the created trial_application record.

    Raises:
        TrialError: If the tenant already has an active trial or subscription.

    Validates: Requirement 6.1, 6.5
    """
    conn = _get_pg_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Check for existing active subscription (any plan) or active trial
            cur.execute(
                """
                SELECT id FROM subscriptions
                WHERE tenant_id = %s
                  AND status = 'active'
                LIMIT 1
                """,
                (tenant_id,),
            )
            if cur.fetchone():
                raise TrialError(
                    "Tenant already has an active trial or subscription"
                )

            # Check for existing pending trial application
            cur.execute(
                """
                SELECT id FROM trial_applications
                WHERE tenant_id = %s
                  AND status = 'pending'
                LIMIT 1
                """,
                (tenant_id,),
            )
            if cur.fetchone():
                raise TrialError(
                    "Tenant already has a pending trial application"
                )

            # Create the trial application
            cur.execute(
                """
                INSERT INTO trial_applications (tenant_id, status)
                VALUES (%s, 'pending')
                RETURNING id, tenant_id, status, applied_at, decided_at, decided_by
                """,
                (tenant_id,),
            )
            application = cur.fetchone()
            conn.commit()

            log.info(
                "Trial application created: id=%s tenant_id=%s",
                application["id"],
                tenant_id,
            )
            return dict(application)
    except TrialError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def approve(application_id: str, decided_by: str) -> dict:
    """Approve a trial application and activate a 30-day trial subscription.

    Sets the application status to 'approved', records decided_at and decided_by,
    then creates a subscription with plan='trial', status='active', and
    expires_at = now() + 30 days.

    Args:
        application_id: UUID of the trial application to approve.
        decided_by: Identifier of the operator who approved (e.g., Discord username).

    Returns:
        Dict with keys 'application' and 'subscription' containing the updated records.

    Raises:
        TrialError: If the application is not found or not in 'pending' status.

    Validates: Requirement 6.3, 6.4
    """
    conn = _get_pg_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Lock and verify the application is pending
            cur.execute(
                """
                SELECT id, tenant_id, status
                FROM trial_applications
                WHERE id = %s
                FOR UPDATE
                """,
                (application_id,),
            )
            application = cur.fetchone()

            if not application:
                raise TrialError(
                    f"Trial application not found: {application_id}"
                )
            if application["status"] != "pending":
                raise TrialError(
                    f"Trial application is not pending (status={application['status']})"
                )

            tenant_id = application["tenant_id"]

            # Update the application to approved
            cur.execute(
                """
                UPDATE trial_applications
                SET status = 'approved',
                    decided_at = now(),
                    decided_by = %s
                WHERE id = %s
                RETURNING id, tenant_id, status, applied_at, decided_at, decided_by
                """,
                (decided_by, application_id),
            )
            updated_application = cur.fetchone()

            # Create trial subscription: 30-day active trial with Base_Plan features
            cur.execute(
                """
                INSERT INTO subscriptions (tenant_id, plan, status, started_at, expires_at)
                VALUES (%s, 'trial', 'active', now(), now() + %s)
                RETURNING id, tenant_id, plan, addons, status, started_at, expires_at, created_at
                """,
                (tenant_id, timedelta(days=TRIAL_DURATION_DAYS)),
            )
            subscription = cur.fetchone()

            conn.commit()

            log.info(
                "Trial approved: application_id=%s tenant_id=%s subscription_id=%s expires_at=%s",
                application_id,
                tenant_id,
                subscription["id"],
                subscription["expires_at"],
            )
            return {
                "application": dict(updated_application),
                "subscription": dict(subscription),
            }
    except TrialError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def deny(application_id: str, decided_by: str) -> dict:
    """Deny a trial application.

    Sets the application status to 'rejected' with decided_at and decided_by.

    Args:
        application_id: UUID of the trial application to deny.
        decided_by: Identifier of the operator who denied (e.g., Discord username).

    Returns:
        Dict with the updated trial_application record.

    Raises:
        TrialError: If the application is not found or not in 'pending' status.

    Validates: Requirement 6.7
    """
    conn = _get_pg_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Lock and verify the application is pending
            cur.execute(
                """
                SELECT id, tenant_id, status
                FROM trial_applications
                WHERE id = %s
                FOR UPDATE
                """,
                (application_id,),
            )
            application = cur.fetchone()

            if not application:
                raise TrialError(
                    f"Trial application not found: {application_id}"
                )
            if application["status"] != "pending":
                raise TrialError(
                    f"Trial application is not pending (status={application['status']})"
                )

            # Update the application to rejected
            cur.execute(
                """
                UPDATE trial_applications
                SET status = 'rejected',
                    decided_at = now(),
                    decided_by = %s
                WHERE id = %s
                RETURNING id, tenant_id, status, applied_at, decided_at, decided_by
                """,
                (decided_by, application_id),
            )
            updated_application = cur.fetchone()
            conn.commit()

            log.info(
                "Trial denied: application_id=%s tenant_id=%s decided_by=%s",
                application_id,
                application["tenant_id"],
                decided_by,
            )
            return dict(updated_application)
    except TrialError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def expire_trials() -> list[dict]:
    """Expire all trial subscriptions that have passed their expires_at date.

    Sets subscription status to 'expired' for any active trial subscriptions
    where expires_at <= now().

    Returns:
        List of expired subscription dicts.

    Validates: Requirement 6.4
    """
    conn = _get_pg_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE subscriptions
                SET status = 'expired'
                WHERE plan = 'trial'
                  AND status = 'active'
                  AND expires_at <= now()
                RETURNING id, tenant_id, plan, status, started_at, expires_at
                """
            )
            expired = cur.fetchall()
            conn.commit()

            if expired:
                log.info(
                    "Expired %d trial subscription(s): %s",
                    len(expired),
                    [str(row["id"]) for row in expired],
                )
            return [dict(row) for row in expired]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_pending_applications() -> list[dict]:
    """Retrieve all pending trial applications ordered by application date (oldest first).

    Returns:
        List of pending trial application dicts.

    Validates: Requirement 6.2 (Admin_Panel visibility)
    """
    conn = _get_pg_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT ta.id, ta.tenant_id, ta.status, ta.applied_at,
                       ta.decided_at, ta.decided_by,
                       t.discord_user_id, t.discord_username
                FROM trial_applications ta
                JOIN tenants t ON t.id = ta.tenant_id
                WHERE ta.status = 'pending'
                ORDER BY ta.applied_at ASC
                """
            )
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()
