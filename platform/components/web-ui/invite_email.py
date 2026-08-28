"""Branded invitation email sender (Amazon SES).

Renders and sends the HelloDJ invitation email that carries the single-use
registration link ``<PUBLIC_BASE_URL>/invite/<raw_token>`` to an invitee. The
email is sent via SES ``send_email`` from the stage's verified sender identity
(``INVITE_SENDER`` config) and is the ONLY email the invite flow produces —
Cognito is suppressed at account-creation time (see ``invite_service``).

The **raw token appears solely in the link** inside the rendered bodies; it is
never logged and never included in any error surfaced to the admin panel
(R7.4). When SES is not configured (no client, or no sender identity) the
service raises :class:`InviteEmailError` so the caller can roll back the
just-created invite record and leave no half-created invite behind (R1.1 /
task 6 rollback).

Mirrors the other service modules: a :class:`SESClient` Protocol pins the
boto3 subset used, the constructor takes keyword-only config, and the service
degrades to a clear error rather than crashing when unconfigured.

Requirements: 1.1, 7.4
"""

from __future__ import annotations

import html
import logging
from typing import Any, Protocol

__all__ = [
    "SESClient",
    "InviteEmailService",
    "InviteEmailError",
    "INVITE_SUBJECT",
]

log = logging.getLogger(__name__)

#: Subject line for the branded invitation email.
INVITE_SUBJECT = "You're invited to HelloDJ"


class InviteEmailError(Exception):
    """Raised when the invitation email cannot be sent.

    Covers an unconfigured SES sender (no client / no sender identity) and any
    SES ``send_email`` failure, so the caller can roll back the pending invite
    and surface a clean error to the admin panel. The message never contains
    the raw Invite_Token (R7.4).
    """


class SESClient(Protocol):
    """Subset of the boto3 ``ses`` client the invitation sender uses."""

    def send_email(self, **kwargs: Any) -> dict[str, Any]: ...


def _render_html(link: str) -> str:
    """Return the branded HTML invitation body for a registration ``link``.

    The link is HTML-escaped for the visible label but used verbatim in the
    ``href`` so the raw token round-trips intact.
    """
    safe_link = html.escape(link, quote=True)
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><body style="margin:0;background:#0b0b12;'
        'font-family:Inter,-apple-system,Segoe UI,system-ui,sans-serif;'
        'color:#e9e9f2;padding:32px">'
        '<div style="max-width:520px;margin:0 auto;background:#15151f;'
        "border:1px solid #2a2a3a;border-radius:16px;padding:32px;"
        'box-shadow:0 4px 24px rgba(0,0,0,.4)">'
        '<h1 style="margin:0 0 16px;font-size:22px;font-weight:600;'
        'color:#c7b3ff">HelloDJ</h1>'
        '<p style="margin:0 0 12px;font-size:16px;line-height:1.6">'
        "You've been invited to join HelloDJ. Click the button below to "
        "create your account. This is a single-use link — it stops working "
        "once you register or after it expires.</p>"
        f'<p style="margin:24px 0"><a href="{safe_link}" '
        'style="display:inline-block;background:#7c4dff;color:#fff;'
        "text-decoration:none;padding:12px 24px;border-radius:12px;"
        'font-weight:600">Accept your invitation</a></p>'
        '<p style="margin:16px 0 0;font-size:13px;color:#8a8aa0;'
        'line-height:1.6">If the button does not work, paste this link into '
        f'your browser:<br><span style="color:#a99bff">{safe_link}</span></p>'
        "</div></body></html>"
    )


def _render_text(link: str) -> str:
    """Return the plain-text invitation body for a registration ``link``."""
    return (
        "You've been invited to join HelloDJ.\n\n"
        "Open this single-use link to create your account:\n"
        f"{link}\n\n"
        "The link stops working once you register or after it expires.\n"
    )


class InviteEmailService:
    """Render + send the branded HelloDJ invitation email via SES.

    The ``ses_client`` is the boto3 ``ses`` client (or ``None`` in a degraded
    deploy); ``sender`` is the stage's verified SES sender identity
    (``INVITE_SENDER``); ``public_base_url`` is the site origin used to build
    the ``/invite/<raw_token>`` link.
    """

    def __init__(
        self,
        ses_client: SESClient | None,
        *,
        sender: str,
        public_base_url: str,
    ) -> None:
        self._ses = ses_client
        self._sender = (sender or "").strip()
        self._public_base_url = (public_base_url or "").rstrip("/")

    @property
    def configured(self) -> bool:
        """Return whether SES is fully configured (client + sender identity)."""
        return self._ses is not None and bool(self._sender)

    def send(self, email: str, raw_token: str) -> None:
        """Send the branded invitation to ``email`` carrying ``raw_token``.

        Builds the ``<public_base_url>/invite/<raw_token>`` link, renders the
        HTML + text bodies, and calls SES ``send_email`` from the configured
        sender identity. The raw token lives only inside the rendered link.

        Raises:
            InviteEmailError: If SES is not configured, or the ``send_email``
                call fails. The error message never contains the raw token so
                the caller can safely surface it to the admin panel (R7.4).
        """
        if not self.configured:
            raise InviteEmailError(
                "invitation email is not configured (no SES sender identity)"
            )
        link = f"{self._public_base_url}/invite/{raw_token}"
        try:
            self._ses.send_email(  # type: ignore[union-attr]
                Source=self._sender,
                Destination={"ToAddresses": [email]},
                Message={
                    "Subject": {"Data": INVITE_SUBJECT, "Charset": "UTF-8"},
                    "Body": {
                        "Html": {"Data": _render_html(link), "Charset": "UTF-8"},
                        "Text": {"Data": _render_text(link), "Charset": "UTF-8"},
                    },
                },
            )
        except Exception as error:  # noqa: BLE001 - normalize to a clean error
            # Log enough to DEBUG the failure server-side — the exception class
            # and (for botocore ClientErrors) the SES error code — but never the
            # recipient, link, or raw token (R7.4). The admin-facing message
            # stays generic.
            code = ""
            response = getattr(error, "response", None)
            if isinstance(response, dict):
                code = response.get("Error", {}).get("Code", "")
            log.warning(
                "invitation email send failed: %s%s",
                type(error).__name__,
                f" ({code})" if code else "",
            )
            # Deliberately omit the exception detail from the message path that
            # could echo the recipient/link; never include the raw token.
            raise InviteEmailError(
                "failed to send the invitation email"
            ) from error
