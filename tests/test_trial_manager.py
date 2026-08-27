"""Unit tests for the Trial Manager service.

Tests cover:
- apply(): creates pending application, rejects duplicates
- approve(): activates 30-day trial subscription, rejects non-pending
- deny(): sets application to rejected, rejects non-pending
- expire_trials(): expires past-due trial subscriptions
- get_pending_applications(): lists pending applications ordered by date

Uses unittest.mock to patch psycopg2 connections.
"""

from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add web-ui directory to path so services package is importable
_webui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _webui_dir not in sys.path:
    sys.path.insert(0, _webui_dir)

from services.trial_manager import TrialError, apply, approve, deny, expire_trials


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cursor(fetchone_returns=None, fetchall_returns=None):
    """Create a mock cursor that supports context manager and basic operations."""
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchone = MagicMock(return_value=fetchone_returns)
    cursor.fetchall = MagicMock(return_value=fetchall_returns or [])
    return cursor


def _make_conn(cursor):
    """Create a mock connection that returns the given cursor."""
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)
    return conn


# ---------------------------------------------------------------------------
# apply() tests
# ---------------------------------------------------------------------------


class TestApply:
    """Tests for trial_manager.apply()."""

    @patch("services.trial_manager._get_pg_conn")
    def test_creates_pending_application(self, mock_conn_fn):
        """apply() should INSERT a pending trial_application and return it."""
        tenant_id = str(uuid.uuid4())
        app_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)

        # First fetchone: no active subscription
        # Second fetchone: no pending application
        # Third fetchone: the created application
        call_count = [0]
        created_app = {
            "id": app_id,
            "tenant_id": tenant_id,
            "status": "pending",
            "applied_at": now,
            "decided_at": None,
            "decided_by": None,
        }

        def side_effect_fetchone():
            call_count[0] += 1
            if call_count[0] <= 2:
                return None  # No active sub, no pending app
            return created_app

        cursor.fetchone = MagicMock(side_effect=side_effect_fetchone)

        conn = _make_conn(cursor)
        mock_conn_fn.return_value = conn

        result = apply(tenant_id)

        assert result["id"] == app_id
        assert result["status"] == "pending"
        assert result["tenant_id"] == tenant_id
        conn.commit.assert_called_once()
        conn.close.assert_called_once()

    @patch("services.trial_manager._get_pg_conn")
    def test_rejects_if_active_subscription_exists(self, mock_conn_fn):
        """apply() should raise TrialError if tenant has active subscription."""
        tenant_id = str(uuid.uuid4())

        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)

        # First fetchone: active subscription found
        cursor.fetchone = MagicMock(return_value={"id": str(uuid.uuid4())})

        conn = _make_conn(cursor)
        mock_conn_fn.return_value = conn

        with pytest.raises(TrialError, match="already has an active trial or subscription"):
            apply(tenant_id)

        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()
        conn.close.assert_called_once()

    @patch("services.trial_manager._get_pg_conn")
    def test_rejects_if_pending_application_exists(self, mock_conn_fn):
        """apply() should raise TrialError if tenant already has a pending application."""
        tenant_id = str(uuid.uuid4())

        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)

        call_count = [0]

        def side_effect_fetchone():
            call_count[0] += 1
            if call_count[0] == 1:
                return None  # No active subscription
            return {"id": str(uuid.uuid4())}  # Pending application exists

        cursor.fetchone = MagicMock(side_effect=side_effect_fetchone)

        conn = _make_conn(cursor)
        mock_conn_fn.return_value = conn

        with pytest.raises(TrialError, match="pending trial application"):
            apply(tenant_id)

        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()
        conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# approve() tests
# ---------------------------------------------------------------------------


class TestApprove:
    """Tests for trial_manager.approve()."""

    @patch("services.trial_manager._get_pg_conn")
    def test_approves_pending_application(self, mock_conn_fn):
        """approve() should update application and create trial subscription."""
        app_id = str(uuid.uuid4())
        tenant_id = str(uuid.uuid4())
        sub_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)

        call_count = [0]

        def side_effect_fetchone():
            call_count[0] += 1
            if call_count[0] == 1:
                # SELECT FOR UPDATE returns pending application
                return {"id": app_id, "tenant_id": tenant_id, "status": "pending"}
            elif call_count[0] == 2:
                # UPDATE RETURNING the approved application
                return {
                    "id": app_id,
                    "tenant_id": tenant_id,
                    "status": "approved",
                    "applied_at": now,
                    "decided_at": now,
                    "decided_by": "admin_user",
                }
            else:
                # INSERT subscription RETURNING
                return {
                    "id": sub_id,
                    "tenant_id": tenant_id,
                    "plan": "trial",
                    "addons": [],
                    "status": "active",
                    "started_at": now,
                    "expires_at": now + timedelta(days=30),
                    "created_at": now,
                }

        cursor.fetchone = MagicMock(side_effect=side_effect_fetchone)

        conn = _make_conn(cursor)
        mock_conn_fn.return_value = conn

        result = approve(app_id, "admin_user")

        assert result["application"]["status"] == "approved"
        assert result["application"]["decided_by"] == "admin_user"
        assert result["subscription"]["plan"] == "trial"
        assert result["subscription"]["status"] == "active"
        assert result["subscription"]["expires_at"] == now + timedelta(days=30)
        conn.commit.assert_called_once()
        conn.close.assert_called_once()

    @patch("services.trial_manager._get_pg_conn")
    def test_rejects_if_application_not_found(self, mock_conn_fn):
        """approve() should raise TrialError if application doesn't exist."""
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        cursor.fetchone = MagicMock(return_value=None)

        conn = _make_conn(cursor)
        mock_conn_fn.return_value = conn

        with pytest.raises(TrialError, match="not found"):
            approve(str(uuid.uuid4()), "admin")

        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()

    @patch("services.trial_manager._get_pg_conn")
    def test_rejects_if_application_not_pending(self, mock_conn_fn):
        """approve() should raise TrialError if application is already decided."""
        app_id = str(uuid.uuid4())

        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        cursor.fetchone = MagicMock(
            return_value={"id": app_id, "tenant_id": str(uuid.uuid4()), "status": "approved"}
        )

        conn = _make_conn(cursor)
        mock_conn_fn.return_value = conn

        with pytest.raises(TrialError, match="not pending"):
            approve(app_id, "admin")

        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# deny() tests
# ---------------------------------------------------------------------------


class TestDeny:
    """Tests for trial_manager.deny()."""

    @patch("services.trial_manager._get_pg_conn")
    def test_denies_pending_application(self, mock_conn_fn):
        """deny() should set application status to 'rejected'."""
        app_id = str(uuid.uuid4())
        tenant_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)

        call_count = [0]

        def side_effect_fetchone():
            call_count[0] += 1
            if call_count[0] == 1:
                return {"id": app_id, "tenant_id": tenant_id, "status": "pending"}
            else:
                return {
                    "id": app_id,
                    "tenant_id": tenant_id,
                    "status": "rejected",
                    "applied_at": now,
                    "decided_at": now,
                    "decided_by": "operator",
                }

        cursor.fetchone = MagicMock(side_effect=side_effect_fetchone)

        conn = _make_conn(cursor)
        mock_conn_fn.return_value = conn

        result = deny(app_id, "operator")

        assert result["status"] == "rejected"
        assert result["decided_by"] == "operator"
        conn.commit.assert_called_once()
        conn.close.assert_called_once()

    @patch("services.trial_manager._get_pg_conn")
    def test_rejects_if_application_not_found(self, mock_conn_fn):
        """deny() should raise TrialError if application doesn't exist."""
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        cursor.fetchone = MagicMock(return_value=None)

        conn = _make_conn(cursor)
        mock_conn_fn.return_value = conn

        with pytest.raises(TrialError, match="not found"):
            deny(str(uuid.uuid4()), "admin")

        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()

    @patch("services.trial_manager._get_pg_conn")
    def test_rejects_if_already_decided(self, mock_conn_fn):
        """deny() should raise TrialError if application already has a decision."""
        app_id = str(uuid.uuid4())

        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        cursor.fetchone = MagicMock(
            return_value={"id": app_id, "tenant_id": str(uuid.uuid4()), "status": "rejected"}
        )

        conn = _make_conn(cursor)
        mock_conn_fn.return_value = conn

        with pytest.raises(TrialError, match="not pending"):
            deny(app_id, "admin")

        conn.commit.assert_not_called()
        conn.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# expire_trials() tests
# ---------------------------------------------------------------------------


class TestExpireTrials:
    """Tests for trial_manager.expire_trials()."""

    @patch("services.trial_manager._get_pg_conn")
    def test_expires_overdue_trials(self, mock_conn_fn):
        """expire_trials() should update expired trials and return them."""
        sub_id = str(uuid.uuid4())
        tenant_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        cursor.fetchall = MagicMock(return_value=[
            {
                "id": sub_id,
                "tenant_id": tenant_id,
                "plan": "trial",
                "status": "expired",
                "started_at": now - timedelta(days=30),
                "expires_at": now,
            }
        ])

        conn = _make_conn(cursor)
        mock_conn_fn.return_value = conn

        result = expire_trials()

        assert len(result) == 1
        assert result[0]["id"] == sub_id
        assert result[0]["status"] == "expired"
        conn.commit.assert_called_once()
        conn.close.assert_called_once()

    @patch("services.trial_manager._get_pg_conn")
    def test_returns_empty_when_no_expired_trials(self, mock_conn_fn):
        """expire_trials() should return empty list when nothing to expire."""
        cursor = MagicMock()
        cursor.__enter__ = MagicMock(return_value=cursor)
        cursor.__exit__ = MagicMock(return_value=False)
        cursor.fetchall = MagicMock(return_value=[])

        conn = _make_conn(cursor)
        mock_conn_fn.return_value = conn

        result = expire_trials()

        assert result == []
        conn.commit.assert_called_once()
