"""Legacy-export loaders for the clean-slate migration.

The migration reads a legacy export and materializes it as a list of
:class:`~hellodj_platform_logic.types.LegacyRecord` objects so the pure
:func:`hellodj_platform_logic.migration.filter_legacy` decision function can
select the records to carry forward. The export *source* is injectable — a
plain Python list, a local JSON file, or an S3 object — so the migration is
testable without touching AWS (R19.1, R19.2, R19.4).

Export JSON shape (a list of record objects)::

    [
      {"record_type": "admin_bootstrap_credential",
       "record_id": "owner", "payload": "{...}"},
      {"record_type": "playlist", "record_id": "p1", "payload": "..."},
      ...
    ]

Unknown ``record_type`` values are rejected so a malformed export fails loudly
rather than silently dropping data.

Requirements: 19.1, 19.2, 19.4
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

from hellodj_platform_logic.types import LegacyRecord, LegacyRecordType

__all__ = [
    "LegacySource",
    "InMemoryLegacySource",
    "JsonFileLegacySource",
    "S3JsonLegacySource",
    "S3Client",
    "build_s3_client",
    "parse_legacy_records",
]


class LegacySource(Protocol):
    """A source of legacy records for the migration to filter."""

    def load(self) -> list[LegacyRecord]:
        """Return the legacy export materialized as ``LegacyRecord`` objects."""
        ...


class S3Client(Protocol):
    """Minimal subset of the boto3 S3 client interface used here."""

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        """Return a response dict whose ``Body`` streams the object bytes."""
        ...


def build_s3_client(region_name: str | None = None) -> S3Client:
    """Create a real boto3 S3 client (imported lazily)."""
    import boto3

    return boto3.client("s3", region_name=region_name)


def _coerce_record(entry: Any, index: int) -> LegacyRecord:
    """Convert one raw export entry into a :class:`LegacyRecord`.

    Args:
        entry: A single decoded JSON element from the export list.
        index: Position of the entry, used only for error messages.

    Returns:
        The typed :class:`LegacyRecord`.

    Raises:
        ValueError: If the entry is not an object, is missing ``record_type``,
            or carries an unknown record type.
    """
    if not isinstance(entry, dict):
        raise ValueError(f"legacy record at index {index} must be an object")

    raw_type = entry.get("record_type")
    if not isinstance(raw_type, str):
        raise ValueError(
            f"legacy record at index {index} is missing a string 'record_type'"
        )
    try:
        record_type = LegacyRecordType(raw_type)
    except ValueError as error:
        valid = ", ".join(t.value for t in LegacyRecordType)
        raise ValueError(
            f"legacy record at index {index} has unknown record_type "
            f"{raw_type!r}; expected one of: {valid}"
        ) from error

    record_id = entry.get("record_id", "")
    payload = entry.get("payload", "")
    return LegacyRecord(
        record_type=record_type,
        record_id=str(record_id),
        payload=str(payload),
    )


def parse_legacy_records(raw: Iterable[Any]) -> list[LegacyRecord]:
    """Parse an iterable of raw export entries into ``LegacyRecord`` objects.

    Args:
        raw: An iterable of decoded JSON objects, each with a ``record_type``
            and optional ``record_id`` / ``payload`` fields.

    Returns:
        The parsed records in input order.

    Raises:
        ValueError: If any entry is malformed or has an unknown record type.
    """
    return [_coerce_record(entry, index) for index, entry in enumerate(raw)]


def _decode_export(text: str) -> list[LegacyRecord]:
    """Decode a JSON export document (a list) into ``LegacyRecord`` objects."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"legacy export is not valid JSON: {error}") from error
    if not isinstance(parsed, list):
        raise ValueError("legacy export JSON must be a list of records")
    return parse_legacy_records(parsed)


class InMemoryLegacySource:
    """A legacy source backed by an in-memory list of records.

    Useful for tests and for callers that have already materialized the export.

    Args:
        records: The legacy records to return from :meth:`load`.
    """

    def __init__(self, records: Iterable[LegacyRecord]) -> None:
        self._records = list(records)

    def load(self) -> list[LegacyRecord]:
        """Return a copy of the in-memory records."""
        return list(self._records)


class JsonFileLegacySource:
    """A legacy source that reads a JSON export from a local file.

    Args:
        path: Filesystem path to the JSON export document.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self) -> list[LegacyRecord]:
        """Read and parse the JSON export file."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError(
                f"failed to read legacy export {self._path}: {error}"
            ) from error
        return _decode_export(text)


class S3JsonLegacySource:
    """A legacy source that reads a JSON export from an S3 object.

    Args:
        bucket: The S3 bucket holding the export.
        key: The object key of the export document.
        client: An injected S3 client. Defaults to a real boto3 client created
            via :func:`build_s3_client`.
        region_name: Region used when creating the default client.
    """

    def __init__(
        self,
        bucket: str,
        key: str,
        *,
        client: S3Client | None = None,
        region_name: str | None = None,
    ) -> None:
        if not bucket or not key:
            raise ValueError("bucket and key are required")
        self._bucket = bucket
        self._key = key
        self._client = client or build_s3_client(region_name)

    def load(self) -> list[LegacyRecord]:
        """Fetch the S3 object and parse it into ``LegacyRecord`` objects."""
        response = self._client.get_object(Bucket=self._bucket, Key=self._key)
        body = response.get("Body")
        if body is None:
            raise ValueError("S3 get_object response has no Body")
        raw = body.read()
        text = raw.decode("utf-8") if isinstance(raw, bytes | bytearray) else str(raw)
        return _decode_export(text)
