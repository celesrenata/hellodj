"""Unit tests for the Subject_Rewriter Lambda handler IO wrapper (R7).

These exercise the *IO glue* only — parsing an SNS alarm record, delivering via
an injected SES client, and the fail-open behaviour. The pure rewrite decisions
(subject prefix, body preservation, fail-open outcome) are covered by Property 7
in ``hellodj_platform_logic/tests/test_alarm_subject_property.py``; here we only
assert the handler wires those through SES ``SendEmail`` correctly and never
drops an alarm.

Requirements: 7.2, 7.3, 7.5
"""

from __future__ import annotations

import json

import pytest
from subject_rewriter.handler import (
    ENV_EMAIL_FROM,
    ENV_EMAIL_TO,
    handler,
    parse_sns_record,
    process_record,
)


class FakeSes:
    """Records ``send_email`` calls for assertion; controllable failure."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    def send_email(self, **kwargs):
        if self.fail:
            raise RuntimeError("SES unavailable")
        self.calls.append(kwargs)
        return {"MessageId": "test"}


def _alarm_message(
    alarm_name: str = "HelloDJ: cpu-transcode-pressure",
    old_state: str = "OK",
    new_state: str = "ALARM",
) -> str:
    return json.dumps(
        {
            "AlarmName": alarm_name,
            "OldStateValue": old_state,
            "NewStateValue": new_state,
            "NewStateReason": "threshold crossed",
        }
    )


def _sns_event(message: str, subject: str = "ALARM: cpu pressure") -> dict:
    return {
        "Records": [
            {"Sns": {"Subject": subject, "Message": message}},
        ]
    }


def _set_env(monkeypatch) -> None:
    monkeypatch.setenv(ENV_EMAIL_FROM, "alarms@hellodj.bot")
    monkeypatch.setenv(ENV_EMAIL_TO, "celes+hellodj@celestium.life")


# ---------------------------------------------------------------------------
# parse_sns_record
# ---------------------------------------------------------------------------


def test_parse_sns_record_extracts_alarm_fields() -> None:
    record = _sns_event(_alarm_message())["Records"][0]
    notification = parse_sns_record(record)
    assert notification.alarm_name == "HelloDJ: cpu-transcode-pressure"
    assert notification.previous_state == "OK"
    assert notification.new_state == "ALARM"
    assert notification.original_subject == "ALARM: cpu pressure"
    assert notification.original_body == _alarm_message()


def test_parse_sns_record_tolerates_non_json_message() -> None:
    record = {"Sns": {"Subject": "plain", "Message": "not json"}}
    notification = parse_sns_record(record)
    # Alarm fields empty, but the original body is preserved verbatim.
    assert notification.alarm_name == ""
    assert notification.previous_state == ""
    assert notification.new_state == ""
    assert notification.original_body == "not json"


# ---------------------------------------------------------------------------
# Success path (R7.2 / R7.3): subject prefixed, body preserves state verbatim
# ---------------------------------------------------------------------------


def test_process_record_success_prefixes_subject_and_preserves_state() -> None:
    ses = FakeSes()
    record = _sns_event(_alarm_message(old_state="OK", new_state="ALARM"))[
        "Records"
    ][0]

    delivery = process_record(
        record, ses, source="from@x", to_address="to@x"
    )

    assert delivery.rewritten is True
    assert delivery.subject.startswith("HelloDJ:")  # R7.2
    # R7.3: the delivered body reproduces the alarm name + both states verbatim.
    assert "HelloDJ: cpu-transcode-pressure" in delivery.body
    assert "OK" in delivery.body
    assert "ALARM" in delivery.body

    # It went out via SES SendEmail with the controllable subject.
    assert len(ses.calls) == 1
    sent = ses.calls[0]
    assert sent["Message"]["Subject"]["Data"].startswith("HelloDJ:")
    assert sent["Destination"]["ToAddresses"] == ["to@x"]
    assert sent["Source"] == "from@x"


# ---------------------------------------------------------------------------
# Fail-open (R7.5)
# ---------------------------------------------------------------------------


def test_process_record_failopen_delivers_original_on_parse_failure() -> None:
    ses = FakeSes()
    # Missing the "Sns" key entirely -> parse_sns_record raises -> fail open.
    bad_record = {"unexpected": "shape", "Sns": None}

    delivery = process_record(
        bad_record, ses, source="from@x", to_address="to@x"
    )

    # Fail-open: an EmailDelivery is still produced and sent (not dropped),
    # marked as not rewritten.
    assert delivery.rewritten is False
    assert len(ses.calls) == 1


def test_handler_delivers_original_when_body_unparseable(monkeypatch) -> None:
    """A record with a non-JSON body still delivers (fail-open, never drop)."""
    _set_env(monkeypatch)
    ses = FakeSes()
    event = {"Records": [{"Sns": {"Subject": "raw", "Message": "totally raw"}}]}

    # Parsing succeeds (empty alarm fields), so rewriter_outcome runs on success;
    # the original raw body is preserved inside the rewritten body.
    result = handler(event, None, client=ses)

    assert result["delivered"] == 1
    assert len(ses.calls) == 1
    assert "totally raw" in ses.calls[0]["Message"]["Body"]["Text"]["Data"]


# ---------------------------------------------------------------------------
# handler entry point
# ---------------------------------------------------------------------------


def test_handler_processes_all_records_and_counts_rewrites(monkeypatch) -> None:
    _set_env(monkeypatch)
    ses = FakeSes()
    event = {
        "Records": [
            {"Sns": {"Subject": "a", "Message": _alarm_message()}},
            {"Sns": {"Subject": "b", "Message": _alarm_message()}},
        ]
    }

    result = handler(event, None, client=ses)

    assert result == {"delivered": 2, "rewritten": 2}
    assert len(ses.calls) == 2


def test_handler_continues_when_one_record_send_fails(monkeypatch) -> None:
    """One record's SES failure must not block the others or crash (R7.5)."""
    _set_env(monkeypatch)

    class FlakySes(FakeSes):
        def send_email(self, **kwargs):
            # Fail the first call, succeed afterwards.
            if not self.calls and not getattr(self, "_failed_once", False):
                self._failed_once = True
                raise RuntimeError("transient SES error")
            self.calls.append(kwargs)
            return {"MessageId": "ok"}

    ses = FlakySes()
    event = {
        "Records": [
            {"Sns": {"Subject": "a", "Message": _alarm_message()}},
            {"Sns": {"Subject": "b", "Message": _alarm_message()}},
        ]
    }

    result = handler(event, None, client=ses)

    # The second record was still delivered despite the first failing.
    assert result["delivered"] == 1
    assert len(ses.calls) == 1


def test_handler_requires_email_env(monkeypatch) -> None:
    monkeypatch.delenv(ENV_EMAIL_FROM, raising=False)
    monkeypatch.delenv(ENV_EMAIL_TO, raising=False)
    ses = FakeSes()
    with pytest.raises(RuntimeError):
        handler({"Records": []}, None, client=ses)
