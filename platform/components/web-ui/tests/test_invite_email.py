"""Tests for the branded SES invitation sender in ``invite_email``.

Task 5 covers rendering + sending the invitation email: the recipient is the
invited email, the sender is the configured verified identity, the link carries
the raw token under ``<public_base_url>/invite/<raw_token>``, both HTML and text
bodies are present, and a send failure (or unconfigured SES) raises
:class:`InviteEmailError` so the caller can roll the invite back (R1.1, R7.4).
"""

from __future__ import annotations

from typing import Any

import pytest

from invite_email import (
    INVITE_SUBJECT,
    InviteEmailError,
    InviteEmailService,
)

_SENDER = "invites@beta.hellodj.bot"
_BASE = "https://beta.us-east-1.hellodj.bot"
_EMAIL = "invitee@example.com"
_TOKEN = "opaque-raw-token-abc123"


class _FakeSES:
    """In-memory SES fake recording the last ``send_email`` kwargs."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def send_email(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"MessageId": "msg-1"}


class _BrokenSES:
    """An SES fake whose ``send_email`` always fails."""

    def send_email(self, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("ses down")


def _service(ses: Any) -> InviteEmailService:
    return InviteEmailService(ses, sender=_SENDER, public_base_url=_BASE)


def test_send_uses_configured_sender_and_recipient() -> None:
    ses = _FakeSES()

    _service(ses).send(_EMAIL, _TOKEN)

    assert len(ses.calls) == 1
    call = ses.calls[0]
    assert call["Source"] == _SENDER
    assert call["Destination"]["ToAddresses"] == [_EMAIL]
    assert call["Message"]["Subject"]["Data"] == INVITE_SUBJECT


def test_send_link_contains_raw_token_and_base_url() -> None:
    ses = _FakeSES()

    _service(ses).send(_EMAIL, _TOKEN)

    body = ses.calls[0]["Message"]["Body"]
    link = f"{_BASE}/invite/{_TOKEN}"
    assert link in body["Html"]["Data"]
    assert link in body["Text"]["Data"]


def test_send_includes_both_html_and_text_bodies() -> None:
    ses = _FakeSES()

    _service(ses).send(_EMAIL, _TOKEN)

    body = ses.calls[0]["Message"]["Body"]
    assert body["Html"]["Data"].strip()
    assert body["Text"]["Data"].strip()
    # HTML is actual markup; text is not.
    assert "<html" in body["Html"]["Data"].lower()
    assert "<html" not in body["Text"]["Data"].lower()


def test_trailing_slash_in_base_url_is_normalized() -> None:
    ses = _FakeSES()

    InviteEmailService(
        ses, sender=_SENDER, public_base_url=_BASE + "/"
    ).send(_EMAIL, _TOKEN)

    link = f"{_BASE}/invite/{_TOKEN}"
    assert link in ses.calls[0]["Message"]["Body"]["Text"]["Data"]


def test_send_failure_raises_invite_email_error() -> None:
    with pytest.raises(InviteEmailError, match="failed to send"):
        _service(_BrokenSES()).send(_EMAIL, _TOKEN)


def test_unconfigured_ses_client_raises_invite_email_error() -> None:
    svc = InviteEmailService(None, sender=_SENDER, public_base_url=_BASE)

    assert svc.configured is False
    with pytest.raises(InviteEmailError, match="not configured"):
        svc.send(_EMAIL, _TOKEN)


def test_missing_sender_identity_raises_invite_email_error() -> None:
    svc = InviteEmailService(_FakeSES(), sender="", public_base_url=_BASE)

    assert svc.configured is False
    with pytest.raises(InviteEmailError, match="not configured"):
        svc.send(_EMAIL, _TOKEN)


def test_error_message_never_contains_the_raw_token() -> None:
    try:
        _service(_BrokenSES()).send(_EMAIL, _TOKEN)
    except InviteEmailError as error:
        assert _TOKEN not in str(error)
    else:  # pragma: no cover - the send must fail
        pytest.fail("expected InviteEmailError")
