"""Unit tests for IPN verification and payment flow.

Tests cover:
1. IPN verification: mock httpx to PayPal, test VERIFIED/INVALID/TIMEOUT responses
2. Timeout handling: test that a 30s timeout returns "TIMEOUT" status
3. Consecutive failure flagging: verify 3 consecutive failures flags for manual review
4. Payment record creation: on VERIFIED + Completed, payment is inserted in DB
5. Cancel/success redirects: test the Flask routes redirect correctly
6. Duplicate payment handling (unique constraint on paypal_txn_id)

Requirements: 8.2, 8.3, 8.7, 8.8, 8.9
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from flask import Flask

# Add web-ui directory to path
_webui_dir = str(Path(__file__).resolve().parent.parent / "web-ui")
if _webui_dir not in sys.path:
    sys.path.insert(0, _webui_dir)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_failure_state():
    """Reset the module-level failure tracking state between tests."""
    from services.payment_gateway import _failure_counts, _flagged_txns

    _failure_counts.clear()
    _flagged_txns.clear()
    yield
    _failure_counts.clear()
    _flagged_txns.clear()


@pytest.fixture
def app():
    """Create a Flask test app with the payments blueprint registered."""
    from blueprints.payments import payments_bp

    flask_app = Flask(__name__)
    flask_app.config["TESTING"] = True
    flask_app.secret_key = "test-secret"
    flask_app.register_blueprint(payments_bp)
    return flask_app


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


# ---------------------------------------------------------------------------
# 1. IPN Verification — VERIFIED, INVALID, TIMEOUT
# ---------------------------------------------------------------------------


class TestIPNVerification:
    """Tests for verify_ipn() function — mocks httpx calls to PayPal."""

    def test_verified_response(self):
        """PayPal returning VERIFIED should yield 'VERIFIED'."""
        from services.payment_gateway import verify_ipn

        mock_response = MagicMock()
        mock_response.text = "VERIFIED"

        with patch("services.payment_gateway.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = verify_ipn({"txn_id": "ABC123", "payment_status": "Completed"})

        assert result == "VERIFIED"

    def test_invalid_response(self):
        """PayPal returning INVALID should yield 'INVALID'."""
        from services.payment_gateway import verify_ipn

        mock_response = MagicMock()
        mock_response.text = "INVALID"

        with patch("services.payment_gateway.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = verify_ipn({"txn_id": "ABC123", "payment_status": "Completed"})

        assert result == "INVALID"

    def test_unexpected_response_treated_as_invalid(self):
        """An unexpected PayPal response body should yield 'INVALID'."""
        from services.payment_gateway import verify_ipn

        mock_response = MagicMock()
        mock_response.text = "SOMETHING_ELSE"

        with patch("services.payment_gateway.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = verify_ipn({"txn_id": "ABC123"})

        assert result == "INVALID"

    def test_verify_sends_correct_payload(self):
        """verify_ipn should echo back the IPN data with cmd=_notify-validate."""
        from services.payment_gateway import verify_ipn

        mock_response = MagicMock()
        mock_response.text = "VERIFIED"

        ipn_data = {"txn_id": "TXN999", "mc_gross": "6.99", "payment_status": "Completed"}

        with patch("services.payment_gateway.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            verify_ipn(ipn_data)

            # Check the posted payload starts with cmd=_notify-validate
            call_args = mock_client.post.call_args
            posted_content = call_args.kwargs.get("content") or call_args[1].get("content", "")
            assert posted_content.startswith("cmd=_notify-validate&")
            assert "txn_id=TXN999" in posted_content


# ---------------------------------------------------------------------------
# 2. Timeout handling
# ---------------------------------------------------------------------------


class TestTimeoutHandling:
    """Tests for timeout behavior in IPN verification."""

    def test_timeout_exception_returns_timeout(self):
        """httpx.TimeoutException should result in 'TIMEOUT' status."""
        from services.payment_gateway import verify_ipn

        with patch("services.payment_gateway.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = httpx.TimeoutException("timed out")
            mock_client_cls.return_value = mock_client

            result = verify_ipn({"txn_id": "TIMEOUT_TXN"})

        assert result == "TIMEOUT"

    def test_http_error_returns_timeout(self):
        """httpx.HTTPError should also result in 'TIMEOUT' status."""
        from services.payment_gateway import verify_ipn

        with patch("services.payment_gateway.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = httpx.HTTPError("connection refused")
            mock_client_cls.return_value = mock_client

            result = verify_ipn({"txn_id": "ERROR_TXN"})

        assert result == "TIMEOUT"

    def test_verify_uses_30s_timeout(self):
        """The httpx Client should be created with a 30s timeout."""
        from services.payment_gateway import IPN_VERIFY_TIMEOUT, verify_ipn

        assert IPN_VERIFY_TIMEOUT == 30.0

        with patch("services.payment_gateway.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_response = MagicMock()
            mock_response.text = "VERIFIED"
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            verify_ipn({"txn_id": "X"})

            # Check timeout was set to 30
            call_kwargs = mock_client_cls.call_args.kwargs
            assert call_kwargs.get("timeout") == 30.0


# ---------------------------------------------------------------------------
# 3. Consecutive failure flagging
# ---------------------------------------------------------------------------


class TestConsecutiveFailureFlagging:
    """Tests for the 3-consecutive-failure manual review flagging."""

    def test_first_failure_not_flagged(self):
        """First failure should not flag for manual review."""
        from services.payment_gateway import process_ipn

        with patch("services.payment_gateway.verify_ipn", return_value="TIMEOUT"):
            result = process_ipn({"txn_id": "TXN_FAIL", "payment_status": "Completed"})

        assert result["status"] == "timeout"
        assert result["txn_id"] == "TXN_FAIL"

    def test_second_failure_not_flagged(self):
        """Second consecutive failure should not flag for manual review."""
        from services.payment_gateway import process_ipn

        with patch("services.payment_gateway.verify_ipn", return_value="INVALID"):
            process_ipn({"txn_id": "TXN_FAIL2", "payment_status": "Completed"})
            result = process_ipn({"txn_id": "TXN_FAIL2", "payment_status": "Completed"})

        assert result["status"] == "invalid"

    def test_third_failure_flags_for_manual_review(self):
        """Third consecutive failure should flag for manual review."""
        from services.payment_gateway import _flagged_txns, process_ipn

        with patch("services.payment_gateway.verify_ipn", return_value="TIMEOUT"):
            process_ipn({"txn_id": "TXN_FLAG", "payment_status": "Completed"})
            process_ipn({"txn_id": "TXN_FLAG", "payment_status": "Completed"})
            result = process_ipn({"txn_id": "TXN_FLAG", "payment_status": "Completed"})

        assert result["status"] == "flagged"
        assert "TXN_FLAG" in _flagged_txns

    def test_flagged_txn_returns_flagged_status_on_subsequent(self):
        """Once flagged, subsequent IPNs for that txn should return 'flagged'."""
        from services.payment_gateway import _flagged_txns, process_ipn

        # Manually flag a txn
        _flagged_txns.add("TXN_ALREADY_FLAGGED")

        # Attempt to process — should short-circuit
        result = process_ipn({"txn_id": "TXN_ALREADY_FLAGGED", "payment_status": "Completed"})

        assert result["status"] == "flagged"

    def test_success_resets_failure_counter(self):
        """A successful verification should reset the failure counter."""
        from services.payment_gateway import _failure_counts, process_ipn

        # Simulate 2 failures first
        with patch("services.payment_gateway.verify_ipn", return_value="TIMEOUT"):
            process_ipn({"txn_id": "TXN_RESET", "payment_status": "Completed"})
            process_ipn({"txn_id": "TXN_RESET", "payment_status": "Completed"})

        assert _failure_counts.get("TXN_RESET") == 2

        # Now succeed
        with patch("services.payment_gateway.verify_ipn", return_value="VERIFIED"), \
             patch("services.payment_gateway._handle_verified_payment",
                   return_value={"status": "ok", "txn_id": "TXN_RESET"}):
            result = process_ipn({"txn_id": "TXN_RESET", "payment_status": "Completed"})

        assert result["status"] == "ok"
        assert "TXN_RESET" not in _failure_counts

    def test_mixed_failures_count_correctly(self):
        """INVALID and TIMEOUT both count toward the 3-failure threshold."""
        from services.payment_gateway import process_ipn

        with patch("services.payment_gateway.verify_ipn", return_value="INVALID"):
            process_ipn({"txn_id": "TXN_MIX", "payment_status": "Completed"})

        with patch("services.payment_gateway.verify_ipn", return_value="TIMEOUT"):
            process_ipn({"txn_id": "TXN_MIX", "payment_status": "Completed"})

        with patch("services.payment_gateway.verify_ipn", return_value="INVALID"):
            result = process_ipn({"txn_id": "TXN_MIX", "payment_status": "Completed"})

        assert result["status"] == "flagged"


# ---------------------------------------------------------------------------
# 4. Payment record creation on VERIFIED + Completed
# ---------------------------------------------------------------------------


class TestPaymentRecordCreation:
    """Tests for _handle_verified_payment and process_ipn success path."""

    def test_verified_completed_creates_payment(self):
        """VERIFIED + Completed should create a payment record and activate subscription."""
        from services.payment_gateway import process_ipn

        subscription_id = str(uuid.uuid4())
        tenant_id = uuid.uuid4()
        payment_id = uuid.uuid4()

        ipn_data = {
            "txn_id": "PAY_SUCCESS_1",
            "payment_status": "Completed",
            "mc_gross": "8.98",
            "mc_currency": "USD",
            "custom": subscription_id,
        }

        with patch("services.payment_gateway.verify_ipn", return_value="VERIFIED"), \
             patch("services.payment_gateway._resolve_tenant_from_subscription",
                   return_value=tenant_id), \
             patch("services.payment_gateway._create_payment_record",
                   return_value=payment_id) as mock_create, \
             patch("services.subscription_manager.SubscriptionManager") as mock_sm_cls:
            mock_sm = MagicMock()
            mock_sm_cls.return_value = mock_sm

            result = process_ipn(ipn_data)

        assert result["status"] == "ok"
        assert result["txn_id"] == "PAY_SUCCESS_1"
        assert result["payment_id"] == str(payment_id)

        # Verify payment record was created with correct args
        mock_create.assert_called_once_with(
            tenant_id=tenant_id,
            paypal_txn_id="PAY_SUCCESS_1",
            amount_cents=898,
            currency="USD",
            status="completed",
        )

        # Verify subscription was activated
        mock_sm.activate.assert_called_once_with(uuid.UUID(subscription_id))

    def test_verified_non_completed_skips_payment(self):
        """VERIFIED but payment_status != Completed should not create a record."""
        from services.payment_gateway import process_ipn

        ipn_data = {
            "txn_id": "PAY_PENDING",
            "payment_status": "Pending",
            "mc_gross": "6.99",
            "custom": str(uuid.uuid4()),
        }

        with patch("services.payment_gateway.verify_ipn", return_value="VERIFIED"):
            result = process_ipn(ipn_data)

        assert result["status"] == "ok"
        assert result.get("note") == "non-completed status"

    def test_verified_completed_invalid_amount(self):
        """VERIFIED + Completed with invalid mc_gross should return error."""
        from services.payment_gateway import process_ipn

        subscription_id = str(uuid.uuid4())
        tenant_id = uuid.uuid4()

        ipn_data = {
            "txn_id": "PAY_BAD_AMOUNT",
            "payment_status": "Completed",
            "mc_gross": "0.00",
            "mc_currency": "USD",
            "custom": subscription_id,
        }

        with patch("services.payment_gateway.verify_ipn", return_value="VERIFIED"), \
             patch("services.payment_gateway._resolve_tenant_from_subscription",
                   return_value=tenant_id):
            result = process_ipn(ipn_data)

        assert result["status"] == "error"
        assert "invalid amount" in result.get("error", "")

    def test_verified_completed_unknown_subscription(self):
        """VERIFIED + Completed with unknown subscription should return error."""
        from services.payment_gateway import process_ipn

        ipn_data = {
            "txn_id": "PAY_NO_SUB",
            "payment_status": "Completed",
            "mc_gross": "6.99",
            "mc_currency": "USD",
            "custom": str(uuid.uuid4()),
        }

        with patch("services.payment_gateway.verify_ipn", return_value="VERIFIED"), \
             patch("services.payment_gateway._resolve_tenant_from_subscription",
                   return_value=None):
            result = process_ipn(ipn_data)

        assert result["status"] == "error"
        assert "unknown subscription" in result.get("error", "")


# ---------------------------------------------------------------------------
# 5. Cancel/success redirects
# ---------------------------------------------------------------------------


class TestRedirects:
    """Tests for the GET /api/v1/payments/success and /cancel redirect routes."""

    def test_success_redirect(self, client):
        """Success route should redirect to /dashboard?payment=success."""
        response = client.get("/api/v1/payments/success")

        assert response.status_code == 302
        assert "/dashboard?payment=success" in response.headers["Location"]

    def test_cancel_redirect(self, client):
        """Cancel route should redirect to /dashboard?payment=cancelled."""
        response = client.get("/api/v1/payments/cancel")

        assert response.status_code == 302
        assert "/dashboard?payment=cancelled" in response.headers["Location"]


# ---------------------------------------------------------------------------
# 6. Duplicate payment handling
# ---------------------------------------------------------------------------


class TestDuplicatePaymentHandling:
    """Tests for duplicate paypal_txn_id handling (unique constraint)."""

    def test_duplicate_txn_id_returns_duplicate_status(self):
        """When _create_payment_record returns None (duplicate), status is 'duplicate'."""
        from services.payment_gateway import process_ipn

        subscription_id = str(uuid.uuid4())
        tenant_id = uuid.uuid4()

        ipn_data = {
            "txn_id": "PAY_DUPE",
            "payment_status": "Completed",
            "mc_gross": "6.99",
            "mc_currency": "USD",
            "custom": subscription_id,
        }

        with patch("services.payment_gateway.verify_ipn", return_value="VERIFIED"), \
             patch("services.payment_gateway._resolve_tenant_from_subscription",
                   return_value=tenant_id), \
             patch("services.payment_gateway._create_payment_record",
                   return_value=None):
            result = process_ipn(ipn_data)

        assert result["status"] == "duplicate"
        assert result["txn_id"] == "PAY_DUPE"


# ---------------------------------------------------------------------------
# IPN Webhook endpoint test
# ---------------------------------------------------------------------------


class TestIPNWebhookEndpoint:
    """Tests for POST /api/v1/payments/ipn Flask route."""

    def test_ipn_endpoint_returns_200(self, client):
        """IPN endpoint should always return 200 to PayPal."""
        ipn_data = {
            "txn_id": "WEB_TXN_001",
            "payment_status": "Completed",
            "mc_gross": "6.99",
        }

        with patch("blueprints.payments.process_ipn",
                   return_value={"status": "ok", "txn_id": "WEB_TXN_001"}):
            response = client.post(
                "/api/v1/payments/ipn",
                data=ipn_data,
                content_type="application/x-www-form-urlencoded",
            )

        assert response.status_code == 200

    def test_ipn_endpoint_empty_body(self, client):
        """Empty IPN body should still return 200 (PayPal expects it)."""
        response = client.post(
            "/api/v1/payments/ipn",
            data={},
            content_type="application/x-www-form-urlencoded",
        )

        assert response.status_code == 200

    def test_ipn_endpoint_calls_process_ipn(self, client):
        """IPN endpoint should call process_ipn with the form data."""
        ipn_data = {
            "txn_id": "WEB_TXN_002",
            "payment_status": "Completed",
            "mc_gross": "8.98",
            "mc_currency": "USD",
            "custom": "sub-id-123",
        }

        with patch("blueprints.payments.process_ipn",
                   return_value={"status": "ok"}) as mock_process:
            client.post(
                "/api/v1/payments/ipn",
                data=ipn_data,
                content_type="application/x-www-form-urlencoded",
            )

        mock_process.assert_called_once()
        call_args = mock_process.call_args[0][0]
        assert call_args["txn_id"] == "WEB_TXN_002"
        assert call_args["mc_gross"] == "8.98"
