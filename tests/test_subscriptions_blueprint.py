"""Unit tests for the subscription management blueprint.

Tests cover:
- GET /api/v1/subscriptions: list tenant subscriptions
- POST /api/v1/subscriptions: create subscription with PayPal redirect
- DELETE /api/v1/subscriptions/{id}: cancel subscription with ownership verification
- POST /api/v1/trials/apply: submit trial application
- GET /api/v1/trials/status: check trial application status

Requirements: 7.5, 7.6, 6.1
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import fakeredis
import pytest
from flask import Flask

# Add web-ui directory to path
_webui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _webui_dir not in sys.path:
    sys.path.insert(0, _webui_dir)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def redis_client():
    """Provide a fresh fakeredis instance per test."""
    client = fakeredis.FakeRedis(decode_responses=True)
    yield client
    client.flushall()
    client.close()


@pytest.fixture
def tenant_id():
    """A fixed tenant UUID for testing."""
    return uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


@pytest.fixture
def session_token():
    """A fixed session token for testing."""
    return "test-session-token-for-subscriptions"


@pytest.fixture
def app(redis_client, tenant_id, session_token):
    """Create a Flask test app with the subscriptions blueprint registered."""
    from blueprints.auth import auth_bp
    from blueprints.subscriptions import subscriptions_bp

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    flask_app.secret_key = "test-secret"

    flask_app.register_blueprint(auth_bp)
    flask_app.register_blueprint(subscriptions_bp)

    # Store a valid session in Redis
    session_data = json.dumps({
        "tenant_id": str(tenant_id),
        "id": str(tenant_id),
        "discord_user_id": "123456789",
        "discord_username": "TestUser",
    })
    redis_client.set(f"session:{session_token}", session_data, ex=604800)

    yield flask_app


@pytest.fixture
def client(app, redis_client, session_token):
    """Flask test client with an authenticated session."""
    from auth_middleware import set_redis_client

    set_redis_client(redis_client)

    with app.test_client() as c:
        c.set_cookie("session_token", session_token, domain="localhost")
        yield c


@pytest.fixture
def unauthenticated_client(app, redis_client):
    """Flask test client without authentication."""
    from auth_middleware import set_redis_client

    set_redis_client(redis_client)
    return app.test_client()


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/subscriptions
# ---------------------------------------------------------------------------


class TestListSubscriptions:
    """Tests for GET /api/v1/subscriptions."""

    def test_list_returns_subscriptions(self, client, tenant_id):
        """Should return the tenant's subscriptions."""
        mock_subs = [
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "plan": "base",
                "addons": [],
                "status": "active",
                "started_at": "2026-01-01T00:00:00+00:00",
                "expires_at": None,
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        ]

        with patch(
            "blueprints.subscriptions._get_subscription_manager"
        ) as mock_sm_factory:
            mock_sm = MagicMock()
            mock_sm.get_tenant_subscriptions.return_value = mock_subs
            mock_sm_factory.return_value = mock_sm

            response = client.get("/api/v1/subscriptions")

        assert response.status_code == 200
        data = response.get_json()
        assert "subscriptions" in data
        assert len(data["subscriptions"]) == 1
        assert data["subscriptions"][0]["plan"] == "base"
        assert data["subscriptions"][0]["status"] == "active"

    def test_list_empty_when_no_subscriptions(self, client, tenant_id):
        """Should return empty list when tenant has no subscriptions."""
        with patch(
            "blueprints.subscriptions._get_subscription_manager"
        ) as mock_sm_factory:
            mock_sm = MagicMock()
            mock_sm.get_tenant_subscriptions.return_value = []
            mock_sm_factory.return_value = mock_sm

            response = client.get("/api/v1/subscriptions")

        assert response.status_code == 200
        data = response.get_json()
        assert data["subscriptions"] == []

    def test_list_with_status_filter(self, client, tenant_id):
        """Should pass status filter to the subscription manager."""
        with patch(
            "blueprints.subscriptions._get_subscription_manager"
        ) as mock_sm_factory:
            mock_sm = MagicMock()
            mock_sm.get_tenant_subscriptions.return_value = []
            mock_sm_factory.return_value = mock_sm

            client.get("/api/v1/subscriptions?status=active")

        mock_sm.get_tenant_subscriptions.assert_called_once_with(
            tenant_id, status="active"
        )

    def test_list_requires_auth(self, unauthenticated_client):
        """Should redirect to login when not authenticated."""
        response = unauthenticated_client.get("/api/v1/subscriptions")
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]


# ---------------------------------------------------------------------------
# Tests: POST /api/v1/subscriptions
# ---------------------------------------------------------------------------


class TestCreateSubscription:
    """Tests for POST /api/v1/subscriptions."""

    def test_create_subscription_success(self, client, tenant_id):
        """Should create a subscription and return PayPal URL."""
        sub_id = uuid.uuid4()
        mock_subscription = {
            "id": sub_id,
            "tenant_id": tenant_id,
            "plan": "base",
            "addons": [],
            "status": "pending_payment",
            "started_at": "2026-01-01T00:00:00+00:00",
            "expires_at": None,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        mock_payment_url = "https://www.sandbox.paypal.com/cgi-bin/webscr?cmd=_xclick&amount=6.99"

        with patch(
            "blueprints.subscriptions._get_subscription_manager"
        ) as mock_sm_factory, patch(
            "blueprints.subscriptions.generate_payment_url",
            return_value=mock_payment_url,
        ):
            mock_sm = MagicMock()
            mock_sm.create_subscription.return_value = mock_subscription
            mock_sm_factory.return_value = mock_sm

            response = client.post(
                "/api/v1/subscriptions",
                json={"plan": "base", "addons": []},
                content_type="application/json",
            )

        assert response.status_code == 201
        data = response.get_json()
        assert "subscription" in data
        assert "payment_url" in data
        assert data["subscription"]["plan"] == "base"
        assert data["subscription"]["status"] == "pending_payment"
        assert data["payment_url"] == mock_payment_url

    def test_create_subscription_with_addons(self, client, tenant_id):
        """Should pass addons to the subscription manager."""
        sub_id = uuid.uuid4()
        mock_subscription = {
            "id": sub_id,
            "tenant_id": tenant_id,
            "plan": "base",
            "addons": ["video", "premium"],
            "status": "pending_payment",
            "started_at": "2026-01-01T00:00:00+00:00",
            "expires_at": None,
            "created_at": "2026-01-01T00:00:00+00:00",
        }

        with patch(
            "blueprints.subscriptions._get_subscription_manager"
        ) as mock_sm_factory, patch(
            "blueprints.subscriptions.generate_payment_url",
            return_value="https://paypal.example.com",
        ):
            mock_sm = MagicMock()
            mock_sm.create_subscription.return_value = mock_subscription
            mock_sm_factory.return_value = mock_sm

            response = client.post(
                "/api/v1/subscriptions",
                json={"plan": "base", "addons": ["video", "premium"]},
                content_type="application/json",
            )

        assert response.status_code == 201
        mock_sm.create_subscription.assert_called_once_with(
            tenant_id, "base", ["video", "premium"]
        )

    def test_create_missing_plan(self, client):
        """Should return 400 when plan is missing."""
        response = client.post(
            "/api/v1/subscriptions",
            json={"addons": []},
            content_type="application/json",
        )
        assert response.status_code == 400
        assert "plan" in response.get_json()["error"].lower()

    def test_create_no_json_body(self, client):
        """Should return 400 when request body is not JSON."""
        response = client.post(
            "/api/v1/subscriptions",
            data="not json",
            content_type="text/plain",
        )
        assert response.status_code == 400
        assert "json" in response.get_json()["error"].lower()

    def test_create_invalid_plan(self, client, tenant_id):
        """Should return 400 for invalid plan name."""
        from services.subscription_manager import InvalidPlanError

        with patch(
            "blueprints.subscriptions._get_subscription_manager"
        ) as mock_sm_factory:
            mock_sm = MagicMock()
            mock_sm.create_subscription.side_effect = InvalidPlanError("Invalid plan: 'fake'")
            mock_sm_factory.return_value = mock_sm

            response = client.post(
                "/api/v1/subscriptions",
                json={"plan": "fake"},
                content_type="application/json",
            )

        assert response.status_code == 400
        assert "invalid plan" in response.get_json()["error"].lower()

    def test_create_addon_prerequisite_error(self, client, tenant_id):
        """Should return 409 when addons requested without active Base_Plan."""
        from services.subscription_manager import AddonPrerequisiteError

        with patch(
            "blueprints.subscriptions._get_subscription_manager"
        ) as mock_sm_factory:
            mock_sm = MagicMock()
            mock_sm.create_subscription.side_effect = AddonPrerequisiteError(
                "An active Base_Plan subscription is required"
            )
            mock_sm_factory.return_value = mock_sm

            response = client.post(
                "/api/v1/subscriptions",
                json={"plan": "base", "addons": ["video"]},
                content_type="application/json",
            )

        assert response.status_code == 409
        assert "base_plan" in response.get_json()["error"].lower()

    def test_create_requires_auth(self, unauthenticated_client):
        """Should redirect to login when not authenticated."""
        response = unauthenticated_client.post(
            "/api/v1/subscriptions",
            json={"plan": "base"},
            content_type="application/json",
        )
        assert response.status_code == 302


# ---------------------------------------------------------------------------
# Tests: DELETE /api/v1/subscriptions/{id}
# ---------------------------------------------------------------------------


class TestCancelSubscription:
    """Tests for DELETE /api/v1/subscriptions/{id}."""

    def test_cancel_success(self, client, tenant_id):
        """Should cancel a subscription owned by the tenant."""
        sub_id = uuid.uuid4()
        mock_existing = {
            "id": sub_id,
            "tenant_id": tenant_id,
            "plan": "base",
            "addons": [],
            "status": "active",
        }
        mock_cancelled = {
            "id": sub_id,
            "tenant_id": tenant_id,
            "plan": "base",
            "addons": [],
            "status": "cancelled",
            "started_at": "2026-01-01T00:00:00+00:00",
            "expires_at": None,
            "created_at": "2026-01-01T00:00:00+00:00",
        }

        with patch(
            "blueprints.subscriptions._get_subscription_manager"
        ) as mock_sm_factory:
            mock_sm = MagicMock()
            mock_sm.get_subscription.return_value = mock_existing
            mock_sm.cancel.return_value = mock_cancelled
            mock_sm_factory.return_value = mock_sm

            response = client.delete(f"/api/v1/subscriptions/{sub_id}")

        assert response.status_code == 200
        data = response.get_json()
        assert data["subscription"]["status"] == "cancelled"

    def test_cancel_not_found(self, client, tenant_id):
        """Should return 404 for nonexistent subscription."""
        sub_id = uuid.uuid4()

        with patch(
            "blueprints.subscriptions._get_subscription_manager"
        ) as mock_sm_factory:
            mock_sm = MagicMock()
            mock_sm.get_subscription.return_value = None
            mock_sm_factory.return_value = mock_sm

            response = client.delete(f"/api/v1/subscriptions/{sub_id}")

        assert response.status_code == 404

    def test_cancel_wrong_owner(self, client, tenant_id):
        """Should return 403 if subscription belongs to another tenant."""
        sub_id = uuid.uuid4()
        other_tenant = uuid.uuid4()
        mock_existing = {
            "id": sub_id,
            "tenant_id": other_tenant,
            "plan": "base",
            "status": "active",
        }

        with patch(
            "blueprints.subscriptions._get_subscription_manager"
        ) as mock_sm_factory:
            mock_sm = MagicMock()
            mock_sm.get_subscription.return_value = mock_existing
            mock_sm_factory.return_value = mock_sm

            response = client.delete(f"/api/v1/subscriptions/{sub_id}")

        assert response.status_code == 403
        assert "does not belong" in response.get_json()["error"].lower()

    def test_cancel_invalid_uuid_format(self, client):
        """Should return 400 for malformed subscription ID."""
        response = client.delete("/api/v1/subscriptions/not-a-uuid")
        assert response.status_code == 400
        assert "invalid" in response.get_json()["error"].lower()

    def test_cancel_already_terminated(self, client, tenant_id):
        """Should return 409 for already cancelled/expired subscription."""
        from services.subscription_manager import InvalidStateTransitionError

        sub_id = uuid.uuid4()
        mock_existing = {
            "id": sub_id,
            "tenant_id": tenant_id,
            "plan": "base",
            "status": "cancelled",
        }

        with patch(
            "blueprints.subscriptions._get_subscription_manager"
        ) as mock_sm_factory:
            mock_sm = MagicMock()
            mock_sm.get_subscription.return_value = mock_existing
            mock_sm.cancel.side_effect = InvalidStateTransitionError(
                "Cannot cancel subscription in 'cancelled' status."
            )
            mock_sm_factory.return_value = mock_sm

            response = client.delete(f"/api/v1/subscriptions/{sub_id}")

        assert response.status_code == 409

    def test_cancel_requires_auth(self, unauthenticated_client):
        """Should redirect to login when not authenticated."""
        sub_id = uuid.uuid4()
        response = unauthenticated_client.delete(f"/api/v1/subscriptions/{sub_id}")
        assert response.status_code == 302


# ---------------------------------------------------------------------------
# Tests: POST /api/v1/trials/apply
# ---------------------------------------------------------------------------


class TestApplyForTrial:
    """Tests for POST /api/v1/trials/apply."""

    def test_apply_success(self, client, tenant_id):
        """Should create a trial application."""
        mock_application = {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "status": "pending",
            "applied_at": "2026-01-01T00:00:00+00:00",
            "decided_at": None,
            "decided_by": None,
        }

        with patch("blueprints.subscriptions.trial_manager") as mock_tm:
            mock_tm.apply.return_value = mock_application
            response = client.post("/api/v1/trials/apply")

        assert response.status_code == 201
        data = response.get_json()
        assert "application" in data
        assert data["application"]["status"] == "pending"

    def test_apply_already_has_subscription(self, client, tenant_id):
        """Should return 409 if tenant already has active trial or subscription."""
        from services.trial_manager import TrialError

        with patch("blueprints.subscriptions.trial_manager") as mock_tm:
            mock_tm.apply.side_effect = TrialError(
                "Tenant already has an active trial or subscription"
            )
            response = client.post("/api/v1/trials/apply")

        assert response.status_code == 409
        assert "already" in response.get_json()["error"].lower()

    def test_apply_requires_auth(self, unauthenticated_client):
        """Should redirect to login when not authenticated."""
        response = unauthenticated_client.post("/api/v1/trials/apply")
        assert response.status_code == 302


# ---------------------------------------------------------------------------
# Tests: GET /api/v1/trials/status
# ---------------------------------------------------------------------------


class TestTrialStatus:
    """Tests for GET /api/v1/trials/status."""

    def test_status_returns_applications(self, client, tenant_id):
        """Should return the tenant's trial applications."""
        mock_applications = [
            {
                "id": uuid.uuid4(),
                "tenant_id": tenant_id,
                "status": "pending",
                "applied_at": "2026-01-15T12:00:00+00:00",
                "decided_at": None,
                "decided_by": None,
            }
        ]

        with patch(
            "blueprints.subscriptions._get_tenant_trial_applications",
            return_value=mock_applications,
        ):
            response = client.get("/api/v1/trials/status")

        assert response.status_code == 200
        data = response.get_json()
        assert "applications" in data
        assert len(data["applications"]) == 1
        assert data["applications"][0]["status"] == "pending"

    def test_status_empty_when_no_applications(self, client, tenant_id):
        """Should return empty list when tenant has no trial applications."""
        with patch(
            "blueprints.subscriptions._get_tenant_trial_applications",
            return_value=[],
        ):
            response = client.get("/api/v1/trials/status")

        assert response.status_code == 200
        data = response.get_json()
        assert data["applications"] == []

    def test_status_requires_auth(self, unauthenticated_client):
        """Should redirect to login when not authenticated."""
        response = unauthenticated_client.get("/api/v1/trials/status")
        assert response.status_code == 302
