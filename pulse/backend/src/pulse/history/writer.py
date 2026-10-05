"""Alert history writer (triggered by the ``pulse-alerts`` DynamoDB Stream).

On every ``pulse-alerts`` create or status change, this Lambda appends a
versioned, append-only record to ``pulse-alert-history`` within 5 seconds
(Requirement 14.1), each carrying an ``expiresAt`` TTL of ``createdAt + 90 days``
so history rolls off automatically (Requirements 14.5, 14.6). A failed history
write is retried up to 3 times; on total failure the error is recorded and the
record is preserved for the next retry cycle (Requirement 14.2).

Following PYQUALITY-05, :func:`lambda_handler` is a thin dispatcher: it
deserializes each stream record's new image and delegates to the pure builders
(:func:`compute_expires_at`, :func:`build_history_item`) and the injectable
persistence seam. The monotonic ``version`` sort key is supplied by a version
provider seam so the pure item builder needs no I/O.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from boto3.dynamodb.types import TypeDeserializer

from pulse.common.config import load_config
from pulse.common.dynamo import get_table
from pulse.common.logging import get_logger
from pulse.common.tracing import get_tracer

logger = get_logger("pulse-history-writer")
tracer = get_tracer("pulse-history-writer")

_deserializer = TypeDeserializer()

# Rolling retention window for shift-handover history (Requirements 14.5, 14.6).
HISTORY_RETENTION_DAYS = 90

# Requirement 14.2 retry policy for a failed history write.
HISTORY_WRITE_MAX_ATTEMPTS = 3
HISTORY_WRITE_RETRY_INTERVAL_SEC = 1

# Attributes copied verbatim from the alert item onto the history record when
# present (all camelCase, NAMING-05).
_COPIED_ATTRIBUTES = (
    "propertyId",
    "gmAlias",
    "tier",
    "type",
    "status",
    "escalationStatus",
    "escalationChain",
    "acknowledgedBy",
    "acknowledgedAt",
    "resolvedBy",
    "resolvedAt",
    "lastStatusChangeAt",
)

# Seam: returns the next monotonic version number for an alert's history.
VersionProviderFn = Callable[[str], int]
# Seam: persists a fully-built history item.
HistoryWriterFn = Callable[[dict[str, Any]], None]


def compute_expires_at(
    created_at_iso: str, retention_days: int = HISTORY_RETENTION_DAYS
) -> int:
    """Compute the TTL epoch for a history record from its creation timestamp.

    Args:
        created_at_iso: The alert ``createdAt`` in ISO 8601 (``Z`` or offset).
        retention_days: The retention window in days (default 90).

    Returns:
        The ``expiresAt`` value as epoch seconds (``createdAt + retention``).

    Raises:
        ValueError: If ``created_at_iso`` is not a parseable ISO 8601 timestamp.
    """
    created = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return int((created + timedelta(days=retention_days)).timestamp())


def build_history_item(alert_item: dict[str, Any], version: int) -> dict[str, Any]:
    """Build a versioned ``pulse-alert-history`` item from an alert item (pure).

    Copies the audit-relevant attributes, stamps the monotonic ``version`` sort
    key, records the status-change time, and sets the ``expiresAt`` TTL.

    Args:
        alert_item: The ``pulse-alerts`` item (post-change new image).
        version: The monotonic version number for this record.

    Returns:
        A ``pulse-alert-history`` item ready to persist.
    """
    created_at = alert_item.get("createdAt", "")
    status_change_at = alert_item.get("lastStatusChangeAt") or created_at
    item: dict[str, Any] = {
        "alertId": alert_item["alertId"],
        "version": version,
        "createdAt": created_at,
        "statusChangeAt": status_change_at,
    }
    for attribute in _COPIED_ATTRIBUTES:
        if attribute in alert_item and alert_item[attribute] is not None:
            item[attribute] = alert_item[attribute]
    if created_at:
        item["expiresAt"] = compute_expires_at(created_at)
    return item


def write_history_with_retry(
    item: dict[str, Any],
    writer: HistoryWriterFn,
    *,
    max_attempts: int = HISTORY_WRITE_MAX_ATTEMPTS,
    interval_sec: int = HISTORY_WRITE_RETRY_INTERVAL_SEC,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Persist a history item, retrying up to ``max_attempts`` on failure.

    Each failure is logged with the ``alertId`` and ``version``; on exhaustion an
    error is recorded and ``False`` is returned so the caller can preserve the
    record for the next retry cycle (Requirement 14.2).

    Args:
        item: The history item to write.
        writer: The persistence seam.
        max_attempts: Maximum write attempts (default 3).
        interval_sec: Seconds between attempts.
        sleep: Sleep function, injectable for tests.

    Returns:
        ``True`` if the write succeeded, ``False`` if all attempts were
        exhausted.
    """
    alert_id = item.get("alertId")
    version = item.get("version")
    for attempt in range(1, max_attempts + 1):
        try:
            writer(item)
            return True
        except Exception as exc:  # noqa: BLE001 - retry policy needs any failure
            logger.error(
                "History write attempt failed",
                extra={
                    "alertId": alert_id,
                    "version": version,
                    "attempt": attempt,
                    "maxAttempts": max_attempts,
                    "error": str(exc),
                },
            )
            if attempt < max_attempts:
                sleep(interval_sec)
    logger.error(
        "History write exhausted; record preserved for next retry cycle",
        extra={"alertId": alert_id, "version": version},
    )
    return False


def process_alert_image(
    alert_item: dict[str, Any],
    *,
    version_provider: VersionProviderFn,
    writer: HistoryWriterFn,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Build and persist a history record for one alert image.

    Args:
        alert_item: The post-change ``pulse-alerts`` image.
        version_provider: Seam returning the next monotonic version.
        writer: The persistence seam.
        sleep: Sleep function passed to the retry (injectable).

    Returns:
        ``True`` if the history record was written, ``False`` on exhaustion.
    """
    version = version_provider(alert_item["alertId"])
    item = build_history_item(alert_item, version)
    return write_history_with_retry(item, writer, sleep=sleep)


# ---------------------------------------------------------------------------
# Default seams (DynamoDB version provider + writer)
# ---------------------------------------------------------------------------


def _default_version_provider(history_table_name: str) -> VersionProviderFn:
    """Build a version provider that returns the next version for an alert.

    Queries the alert's existing history versions (newest first) and returns
    ``max + 1``, or ``1`` when no prior record exists.

    Args:
        history_table_name: The ``pulse-alert-history`` table name.

    Returns:
        A version provider seam.
    """
    from boto3.dynamodb.conditions import Key

    table = get_table(history_table_name)

    def _next(alert_id: str) -> int:
        response = table.query(
            KeyConditionExpression=Key("alertId").eq(alert_id),
            ScanIndexForward=False,
            Limit=1,
            ProjectionExpression="version",
        )
        items = response.get("Items", [])
        if not items:
            return 1
        return int(items[0]["version"]) + 1

    return _next


def _default_writer(history_table_name: str) -> HistoryWriterFn:
    """Build the default history writer over ``pulse-alert-history``.

    Args:
        history_table_name: The ``pulse-alert-history`` table name.

    Returns:
        A writer seam that puts an item into the history table.
    """
    table = get_table(history_table_name)

    def _write(item: dict[str, Any]) -> None:
        table.put_item(Item=item)

    return _write


def _deserialize_new_image(
    record: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Deserialize a stream record's ``NewImage`` into native Python types.

    Args:
        record: A single ``pulse-alerts`` stream record.

    Returns:
        The deserialized new image, or ``None`` for REMOVE events (no image).
    """
    raw = record.get("dynamodb", {}).get("NewImage")
    if not raw:
        return None
    return {key: _deserializer.deserialize(value) for key, value in raw.items()}


@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """History writer Lambda handler for a ``pulse-alerts`` stream batch.

    Deserializes each record's new image and appends a versioned history record.
    Records without a new image (REMOVE) are skipped. One failing record never
    blocks the batch: it is logged and the next record is processed.

    Args:
        event: The Lambda event with a ``Records`` list of stream records.
        context: The Lambda context (unused; present for the handler contract).

    Returns:
        A summary dict with the number of records processed and written.
    """
    config = load_config()
    version_provider = _default_version_provider(config.alert_history_table)
    writer = _default_writer(config.alert_history_table)

    records = event.get("Records", [])
    processed = 0
    written = 0
    for record in records:
        try:
            image = _deserialize_new_image(record)
            if image is None or "alertId" not in image:
                continue
            processed += 1
            if process_alert_image(
                image, version_provider=version_provider, writer=writer
            ):
                written += 1
        except Exception as exc:  # noqa: BLE001 - per-record isolation
            logger.error(
                "Failed to write history for stream record; continuing batch",
                extra={"eventID": record.get("eventID"), "error": str(exc)},
            )
    logger.info(
        "History batch processed",
        extra={"recordsProcessed": processed, "recordsWritten": written},
    )
    return {"recordsProcessed": processed, "recordsWritten": written}


__all__ = [
    "HISTORY_RETENTION_DAYS",
    "HISTORY_WRITE_MAX_ATTEMPTS",
    "HISTORY_WRITE_RETRY_INTERVAL_SEC",
    "VersionProviderFn",
    "HistoryWriterFn",
    "compute_expires_at",
    "build_history_item",
    "write_history_with_retry",
    "process_alert_image",
    "lambda_handler",
]
