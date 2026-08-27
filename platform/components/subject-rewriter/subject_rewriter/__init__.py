"""Subject_Rewriter AWS Lambda component (R7.2/R7.3/R7.5).

Thin IO wrapper around the pure ``hellodj_platform_logic.alarm_subject``
decision functions. The Lambda is subscribed on the email side of the alarm
SNS topic (wired in ``observability-stack.ts`` by task 17.2); it parses each
incoming SNS alarm message into an
:class:`~hellodj_platform_logic.types.AlarmNotification`, delegates every
decision to the pure ``rewriter_outcome`` function, and delivers the resulting
:class:`~hellodj_platform_logic.types.EmailDelivery` via SES ``SendEmail``.

All decision logic lives in ``hellodj_platform_logic``; this package holds only
the parse + SES-call + fail-open IO glue so the correctness property (P7) is
exercised against the pure core, not the handler.

Requirements: 7.2, 7.3, 7.5
"""

from __future__ import annotations

from .handler import handler

__all__ = ["handler"]
