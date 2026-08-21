"""JSON schema validation for Command_Object arrays.

Pure functions — no I/O, no side effects. Suitable for property-based testing.
"""

import json
import logging
import re
from typing import Any

log = logging.getLogger(__name__)


def strip_json_fences(text: str) -> str:
    """Strip markdown code fences and non-JSON preamble/trailing text.

    Handles:
    - ```json ... ```
    - ``` ... ```
    - Leading/trailing whitespace and prose
    """
    # Remove ```json or ``` fences
    text = re.sub(r"^```(?:json)?\s*\n?", "", text.strip())
    text = re.sub(r"\n?```\s*$", "", text.strip())

    # Try to find the JSON array boundaries
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]

    return text.strip()


def validate_command_object(obj: Any) -> dict[str, Any] | None:
    """Validate a single Command_Object against the schema.

    Schema:
        action: str (required, non-empty)
        source: str | None
        query: str | None
        arguments: dict | None (defaults to {})

    Returns the normalized Command_Object or None if invalid.
    """
    if not isinstance(obj, dict):
        return None

    # action is required and must be a non-empty string
    action = obj.get("action")
    if not isinstance(action, str) or not action.strip():
        return None

    # source: string or null
    source = obj.get("source")
    if source is not None and not isinstance(source, str):
        source = None
    if isinstance(source, str) and not source.strip():
        source = None

    # query: string or null
    query = obj.get("query")
    if query is not None and not isinstance(query, str):
        query = None

    # arguments: dict or null (default to {})
    arguments = obj.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}

    return {
        "action": action.strip().lower(),
        "source": source.strip().lower() if source else None,
        "query": query.strip() if query else None,
        "arguments": arguments,
    }


def validate_command_objects(objects: list[Any]) -> list[dict[str, Any]]:
    """Validate a list of objects, returning only valid Command_Objects.

    Invalid elements are discarded and logged (Requirement 11.4).
    """
    valid = []
    for i, obj in enumerate(objects):
        result = validate_command_object(obj)
        if result is not None:
            valid.append(result)
        else:
            log.warning("Discarding invalid Command_Object at index %d: %s", i, obj)
    return valid
