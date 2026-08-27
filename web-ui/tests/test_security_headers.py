"""Tests for the security headers after_request handler (task 4.1)."""

import pytest


@pytest.fixture()
def client():
    """Create a Flask test client with the app in testing mode."""
    # Import here so test collection doesn't fail if deps are missing at import time
    import sys
    import os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

    # Minimal env so app.py doesn't crash looking for dirs
    os.environ.setdefault("FLASK_SECRET", "test-secret-key")
    os.environ.setdefault("HELLODJ_DATA_DIR", "/tmp/hellodj-test-data")
    os.environ.setdefault("HELLODJ_CONFIG_DIR", "/tmp/hellodj-test-config")
    os.environ.setdefault("HELLODJ_BACKUP_DIR", "/tmp/hellodj-test-backups")
    os.makedirs("/tmp/hellodj-test-data", exist_ok=True)
    os.makedirs("/tmp/hellodj-test-config", exist_ok=True)
    os.makedirs("/tmp/hellodj-test-backups", exist_ok=True)

    from app import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


class TestSecurityHeaders:
    """Verify security headers are present on all response types."""

    def test_headers_on_normal_route(self, client):
        """Security headers appear on a normal 200/302 response."""
        resp = client.get("/")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Strict-Transport-Security") == "max-age=31536000; includeSubDomains"

    def test_headers_on_404(self, client):
        """Security headers appear on a 404 error response."""
        resp = client.get("/nonexistent-page-xyz")
        assert resp.status_code == 404
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Strict-Transport-Security") == "max-age=31536000; includeSubDomains"

    def test_headers_on_favicon(self, client):
        """Security headers appear on static file responses."""
        resp = client.get("/favicon.ico")
        # Regardless of whether favicon exists, headers should be set
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Strict-Transport-Security") == "max-age=31536000; includeSubDomains"

    def test_does_not_override_existing_content_type(self, client):
        """The handler does not tamper with existing Content-Type headers."""
        resp = client.get("/")
        # Content-Type should still be set by Flask (html or redirect)
        ct = resp.headers.get("Content-Type")
        assert ct is not None
        assert "text/html" in ct or "text/plain" in ct or ct.startswith("text/")
