"""In-memory fake DynamoDB table for orchestrator persistence tests.

Implements just enough of the boto3 resource ``Table`` surface consumed by the
shared ``SessionTable`` (``get_item`` / ``put_item`` with a ``version``
``ConditionExpression``) so the single-writer persistence layer can be tested
without boto3, moto, or a live DynamoDB. Items are keyed by their ``PK``/``SK``
attributes, matching the ``hellodj-session`` schema.
"""

from __future__ import annotations

from typing import Any


class FakeConditionalCheckError(Exception):
    """Mimics ``botocore.exceptions.ClientError`` for a failed condition.

    Carries the ``response`` payload the data-access error classifier inspects
    (``response["Error"]["Code"]``), so ``is_conditional_check_failure`` treats
    it exactly like the real conditional-check failure.
    """

    def __init__(self) -> None:
        super().__init__("The conditional request failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    """A minimal in-memory DynamoDB table keyed on ``PK``/``SK``."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        """Return ``{"Item": item}`` or ``{}`` for the given ``Key``."""
        key = kwargs["Key"]
        stored = self._items.get((key["PK"], key["SK"]))
        if stored is None:
            return {}
        return {"Item": dict(stored)}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        """Write an item, enforcing an optional ``version`` condition."""
        item = kwargs["Item"]
        composite = (item["PK"], item["SK"])
        condition = kwargs.get("ConditionExpression")
        values = kwargs.get("ExpressionAttributeValues", {})
        existing = self._items.get(composite)

        if condition == "attribute_not_exists(version)":
            if existing is not None:
                raise FakeConditionalCheckError()
        elif condition == "version = :expected":
            expected = values[":expected"]
            if existing is None or existing.get("version") != expected:
                raise FakeConditionalCheckError()

        self._items[composite] = dict(item)
        return {}
