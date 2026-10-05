"""INFO alert batcher (``pulse-info-batcher``).

INFO alerts are not delivered one-by-one; they are accumulated and flushed on a
coarse interval (Requirement 13.3, default 15 minutes, valid range 5-60) or
immediately once an accumulating property reaches the 50-alert cap (Requirement
13.4). This Lambda is driven by an EventBridge Scheduler rate schedule (design
Decision 3): on each tick it loads the undelivered INFO alerts, batches them per
property (at most 50 per batch), publishes each batch to the property's AppSync
Events channel, and marks the alerts delivered.

Following PYQUALITY-05 the batching itself is pure and separated from I/O:
:func:`batch_alerts` and :func:`group_by_property` carry the Requirement
13.3/13.4 guarantees (every batch <= 50; the union of all batches equals the
accumulated set with no alert dropped or duplicated -- Property 21), while
:func:`lambda_handler` wires the DynamoDB loader/marker and the realtime
publisher seams around them.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Optional

from pulse.common.config import (
    DEFAULT_INFO_BATCH_INTERVAL_MIN,
    load_config,
)
from pulse.common.dynamo import get_table
from pulse.common.logging import get_logger
from pulse.common.tracing import get_tracer
from pulse.delivery import realtime_publish as rt

logger = get_logger("pulse-info-batcher")
tracer = get_tracer("pulse-info-batcher")

# Requirement 13.4: an INFO batch never exceeds 50 alerts.
INFO_BATCH_MAX = 50

# Requirement 13.3: the batching interval is configurable within 5-60 minutes.
INFO_BATCH_INTERVAL_MIN_BOUND = 5
INFO_BATCH_INTERVAL_MAX_BOUND = 60

# Seam: loads the currently-undelivered INFO alert items.
UndeliveredLoaderFn = Callable[[], list[dict[str, Any]]]
# Seam: marks a set of alert ids delivered at a timestamp.
MarkerFn = Callable[[Sequence[str], str], None]


def resolve_batch_interval_min(requested: Optional[int]) -> int:
    """Clamp a requested batching interval into the accepted 5-60 range.

    Args:
        requested: The requested interval in minutes, or ``None`` to use the
            documented default.

    Returns:
        The interval clamped to ``[5, 60]`` minutes (Requirement 13.3).
    """
    value = requested if requested is not None else DEFAULT_INFO_BATCH_INTERVAL_MIN
    return max(
        INFO_BATCH_INTERVAL_MIN_BOUND,
        min(INFO_BATCH_INTERVAL_MAX_BOUND, value),
    )


def group_by_property(
    alerts: Sequence[dict[str, Any]],
) -> OrderedDict[str, list[dict[str, Any]]]:
    """Group alert items by ``propertyId`, preserving first-seen order (pure).

    Args:
        alerts: The accumulated INFO alert items.

    Returns:
        An ordered mapping of ``propertyId`` to the alerts for that property, in
        the order the properties were first encountered.
    """
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for alert in alerts:
        property_id = alert.get("propertyId", "")
        grouped.setdefault(property_id, []).append(alert)
    return grouped


def batch_alerts(
    alerts: Sequence[dict[str, Any]], max_batch: int = INFO_BATCH_MAX
) -> list[list[dict[str, Any]]]:
    """Split alerts into order-preserving batches no larger than ``max_batch``.

    Guarantees, for Property 21: every returned batch has at most ``max_batch``
    alerts; a new batch begins whenever accumulation reaches the cap; and the
    concatenation of all batches equals the input exactly (no alert dropped or
    duplicated).

    Args:
        alerts: The alerts to batch.
        max_batch: The maximum batch size (default 50).

    Returns:
        A list of batches, each with at most ``max_batch`` alerts.
    """
    if max_batch < 1:
        raise ValueError("max_batch must be >= 1")
    return [
        list(alerts[i : i + max_batch]) for i in range(0, len(alerts), max_batch)
    ]


def _utc_now_iso() -> str:
    """Return the current UTC time in ISO 8601 with a ``Z`` suffix.

    Returns:
        The current timestamp string.
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def flush_info_alerts(
    alerts: Sequence[dict[str, Any]],
    *,
    marker: MarkerFn,
    publisher: Optional[rt.PublisherFn] = None,
    now_iso: Optional[str] = None,
) -> dict[str, Any]:
    """Batch and deliver accumulated INFO alerts per property (orchestration).

    For each property, the alerts are split into <=50-alert batches; each batch
    is published to the property's AppSync Events channel and then marked
    delivered. Publishing is best-effort (see :func:`realtime_publish.publish`);
    the marker records delivery so the same alert is not re-batched.

    Args:
        alerts: The undelivered INFO alert items.
        marker: Seam that marks a set of alert ids delivered at a timestamp.
        publisher: Realtime publisher seam (default resolved when omitted).
        now_iso: The delivery timestamp; defaults to UTC now.

    Returns:
        A summary dict with counts of properties, batches, and alerts delivered.
    """
    timestamp = now_iso or _utc_now_iso()
    grouped = group_by_property(alerts)
    batches_delivered = 0
    alerts_delivered = 0
    for property_id, items in grouped.items():
        for batch in batch_alerts(items):
            events = [
                rt.event_from_item(item, rt.EVENT_ALERT_UPDATED) for item in batch
            ]
            rt.publish(
                rt.broadcast_channel(property_id), events, publisher=publisher
            )
            marker([item["alertId"] for item in batch], timestamp)
            batches_delivered += 1
            alerts_delivered += len(batch)
    summary = {
        "propertiesFlushed": len(grouped),
        "batchesDelivered": batches_delivered,
        "alertsDelivered": alerts_delivered,
    }
    logger.info("INFO batch flush complete", extra=summary)
    return summary


# ---------------------------------------------------------------------------
# Default seams (DynamoDB loader + delivered marker)
# ---------------------------------------------------------------------------


def _default_undelivered_loader(
    alerts_table_name: str, property_id: Optional[str] = None
) -> UndeliveredLoaderFn:
    """Build the default loader of undelivered INFO alerts.

    When ``property_id`` is supplied, the ``propertyId-status-index`` is queried
    for that property; otherwise the table is scanned (acceptable at prototype
    scale). In both cases the result is filtered to INFO alerts that have not yet
    been marked delivered.

    Args:
        alerts_table_name: The ``pulse-alerts`` table name.
        property_id: Optional property to scope the query to.

    Returns:
        A loader returning the undelivered INFO alert items.
    """
    from boto3.dynamodb.conditions import Attr, Key

    table = get_table(alerts_table_name)
    # INFO alerts stay UNACKNOWLEDGED until batched; exclude any already marked.
    undelivered = Attr("tier").eq("INFO") & Attr("infoDeliveredAt").not_exists()

    def _load() -> list[dict[str, Any]]:
        if property_id:
            response = table.query(
                IndexName="propertyId-status-index",
                KeyConditionExpression=(
                    Key("propertyId").eq(property_id) & Key("status").eq(
                        "UNACKNOWLEDGED"
                    )
                ),
                FilterExpression=undelivered,
            )
        else:
            response = table.scan(
                FilterExpression=Attr("status").eq("UNACKNOWLEDGED") & undelivered
            )
        return list(response.get("Items", []))

    return _load


def _default_marker(alerts_table_name: str) -> MarkerFn:
    """Build the default marker that stamps ``infoDeliveredAt`` on alerts.

    Args:
        alerts_table_name: The ``pulse-alerts`` table name.

    Returns:
        A marker that sets ``infoDeliveredAt`` for each alert id.
    """
    table = get_table(alerts_table_name)

    def _mark(alert_ids: Sequence[str], delivered_at: str) -> None:
        for alert_id in alert_ids:
            # Record the INFO delivery time so the alert is not re-batched on
            # the next interval; leaves the lifecycle status untouched.
            table.update_item(
                Key={"alertId": alert_id},
                UpdateExpression="SET infoDeliveredAt = :at",
                ExpressionAttributeValues={":at": delivered_at},
            )

    return _mark


@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """INFO batcher Lambda handler (thin dispatcher).

    Loads the undelivered INFO alerts (optionally scoped to a ``propertyId`` in
    the event), then batches, publishes, and marks them via the pure
    orchestration in :func:`flush_info_alerts`.

    Args:
        event: The invocation event; may carry a ``propertyId`` to scope the
            load.
        context: The Lambda context (unused; present for the handler contract).

    Returns:
        A flush summary dict.
    """
    config = load_config()
    loader = _default_undelivered_loader(
        config.alerts_table, property_id=event.get("propertyId")
    )
    marker = _default_marker(config.alerts_table)
    alerts = loader()
    if not alerts:
        logger.info("No undelivered INFO alerts to flush")
        return {"propertiesFlushed": 0, "batchesDelivered": 0, "alertsDelivered": 0}
    return flush_info_alerts(alerts, marker=marker)


__all__ = [
    "INFO_BATCH_MAX",
    "INFO_BATCH_INTERVAL_MIN_BOUND",
    "INFO_BATCH_INTERVAL_MAX_BOUND",
    "UndeliveredLoaderFn",
    "MarkerFn",
    "resolve_batch_interval_min",
    "group_by_property",
    "batch_alerts",
    "flush_info_alerts",
    "lambda_handler",
]
