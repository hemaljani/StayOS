"""Time-based alert auto-resolve sweeper (``pulse-alert-timeout-sweeper``).

A scheduled Lambda (EventBridge Scheduler, every few minutes) that resolves any
still-open alert older than a fixed age (default 30 minutes). This complements
the Rule Engine's condition-cleared safety net (``loop_guard``): demo-generated
alerts (and any alert whose condition never formally clears) do not linger
forever - they stay visible in the live feed for the age window, then move to
the resolved history once swept.

Design notes:
    * **Idempotent, guarded resolve.** Each resolve is a single ``UpdateItem``
      guarded on ``status`` being in the open set (mirrors ``loop_guard``), so a
      terminal alert is a no-op and re-runs never mutate a resolved alert
      (monotonic RESOLVED, Property 27). ``resolvedBy`` is ``system-timeout`` to
      distinguish a timeout sweep from an executor/manual resolve.
    * **Best-effort realtime.** An ``ALERT_RESOLVED`` event is published per
      resolution behind the injectable publisher seam; a publish failure never
      fails the sweep (realtime is best-effort, non-blocking).
    * **Thin handler (PYQUALITY-05).** ``lambda_handler`` loads config and
      delegates to :func:`sweep_expired_alerts`, which takes the table getter,
      publisher, clock, and age as parameters so it is fully unit-testable with
      an in-memory fake table.
    * **Resource names from env (PYQUALITY-06 / NAMING-03).** The table name is
      read from ``ALERTS_TABLE_NAME``; the age window from the optional
      ``ALERT_TIMEOUT_MINUTES`` (default 30).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Optional

from boto3.dynamodb.conditions import Attr

from pulse.common.config import (
    ENV_ALERTS_TABLE,
    _get_required_env,
    get_optional_env,
)
from pulse.common.dynamo import get_table
from pulse.common.logging import get_logger
from pulse.common.models import AlertStatus
from pulse.common.tracing import get_tracer
from pulse.delivery import realtime_publish as rt
from pulse.rule_engine.alert_factory import utc_now_iso

logger = get_logger("pulse-alert-timeout-sweeper")
tracer = get_tracer("pulse-alert-timeout-sweeper")

# Optional override for the auto-resolve age window, in minutes. Defaults to 30.
ENV_ALERT_TIMEOUT_MINUTES = "ALERT_TIMEOUT_MINUTES"
DEFAULT_TIMEOUT_MINUTES = 30

# ``status`` is a DynamoDB reserved word; alias it in the update/condition.
_STATUS_NAME_MAP = {"#status": "status"}

# The statuses a timeout resolution may transition from. RESOLVED and
# ESCALATION_EXHAUSTED are terminal and are left untouched.
_OPEN_STATUSES = (
    AlertStatus.UNACKNOWLEDGED.value,
    AlertStatus.ACKNOWLEDGED.value,
    AlertStatus.ESCALATED.value,
)


def _resolve_timeout_minutes() -> int:
    """Resolve the auto-resolve age window in minutes.

    Reads ``ALERT_TIMEOUT_MINUTES`` and falls back to
    :data:`DEFAULT_TIMEOUT_MINUTES` when unset or not a positive integer.

    Returns:
        The positive integer age window in minutes.
    """
    raw = get_optional_env(ENV_ALERT_TIMEOUT_MINUTES)
    if raw is None:
        return DEFAULT_TIMEOUT_MINUTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_MINUTES
    return value if value > 0 else DEFAULT_TIMEOUT_MINUTES


def _cutoff_iso(now: str, timeout_minutes: int) -> str:
    """Compute the ISO 8601 cutoff: alerts created before this are expired.

    Args:
        now: The current time as an ISO 8601 string (``...Z`` accepted).
        timeout_minutes: The age window in minutes.

    Returns:
        The cutoff timestamp as an ISO 8601 string with a ``Z`` suffix.
    """
    current = datetime.fromisoformat(now.replace("Z", "+00:00"))
    cutoff = current - timedelta(minutes=timeout_minutes)
    return cutoff.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _scan_open_expired(table: Any, cutoff: str) -> list[dict[str, Any]]:
    """Scan ``pulse-alerts`` for still-open alerts created before the cutoff.

    A table Scan is acceptable here: this is a low-frequency scheduled sweep and
    the demo/pilot table is small. The filter narrows to open statuses and
    ``createdAt < cutoff`` so only genuinely expired, still-open alerts return.

    Args:
        table: The ``pulse-alerts`` DynamoDB table resource.
        cutoff: The ISO 8601 cutoff; alerts with ``createdAt`` before it expire.

    Returns:
        The list of expired, still-open alert items.
    """
    filter_expr = Attr("status").is_in(list(_OPEN_STATUSES)) & Attr("createdAt").lt(
        cutoff
    )
    items: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {"FilterExpression": filter_expr}
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return items


def _resolve_timed_out(
    alert_id: str, table: Any, now: str
) -> Optional[dict[str, Any]]:
    """Resolve one still-open alert as a timeout, guarded and idempotent.

    Args:
        alert_id: The alert to resolve.
        table: The ``pulse-alerts`` DynamoDB table resource.
        now: The ISO 8601 UTC resolution timestamp.

    Returns:
        The updated alert item when a still-open alert was resolved, or ``None``
        when it did not exist or was already terminal (a no-op).
    """
    try:
        response = table.update_item(
            Key={"alertId": alert_id},
            UpdateExpression=(
                "SET #status = :resolved, resolvedBy = :user, resolvedAt = :ts, "
                "lastStatusChangeAt = :ts"
            ),
            ConditionExpression=(
                Attr("alertId").exists() & Attr("status").is_in(list(_OPEN_STATUSES))
            ),
            ExpressionAttributeNames=dict(_STATUS_NAME_MAP),
            ExpressionAttributeValues={
                ":resolved": AlertStatus.RESOLVED.value,
                ":user": "system-timeout",
                ":ts": now,
            },
            ReturnValues="ALL_NEW",
        )
        return response.get("Attributes")
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        # Already terminal or gone between the scan and the update: benign no-op.
        return None


def sweep_expired_alerts(
    alerts_table_name: str,
    *,
    table_getter: Any = get_table,
    realtime_publisher: Optional[rt.PublisherFn] = None,
    now: Optional[str] = None,
    timeout_minutes: Optional[int] = None,
) -> list[str]:
    """Resolve every still-open alert older than the timeout window.

    Args:
        alerts_table_name: The ``pulse-alerts`` table name.
        table_getter: Table-resource getter seam (injectable for tests).
        realtime_publisher: The realtime publisher seam (default resolved when
            omitted).
        now: ISO 8601 timestamp override (injectable for tests).
        timeout_minutes: Age-window override (falls back to env/default).

    Returns:
        The alert ids that were auto-resolved (empty when none were expired).
    """
    timestamp = now or utc_now_iso()
    window = (
        timeout_minutes
        if timeout_minutes is not None
        else _resolve_timeout_minutes()
    )
    cutoff = _cutoff_iso(timestamp, window)
    table = table_getter(alerts_table_name)

    resolved_ids: list[str] = []
    for item in _scan_open_expired(table, cutoff):
        alert_id = item.get("alertId")
        if not alert_id:
            continue
        updated = _resolve_timed_out(alert_id, table, timestamp)
        if updated is None:
            continue
        resolved_ids.append(alert_id)
        logger.info(
            "Auto-resolved timed-out alert",
            extra={
                "alertId": alert_id,
                "propertyId": updated.get("propertyId"),
                "timeoutMinutes": window,
            },
        )
        # Best-effort realtime; a publish failure must not fail the sweep.
        try:
            rt.realtime_publish(
                rt.EVENT_ALERT_RESOLVED, updated, publisher=realtime_publisher
            )
        except Exception as exc:  # noqa: BLE001 - realtime is best-effort
            logger.warning(
                "Realtime publish failed for timed-out alert; continuing",
                extra={"alertId": alert_id, "error": str(exc)},
            )
    logger.info(
        "Timeout sweep complete",
        extra={"resolvedCount": len(resolved_ids), "cutoff": cutoff},
    )
    return resolved_ids


@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Scheduled sweeper Lambda entry point (thin dispatcher, PYQUALITY-05).

    Loads the alerts table name from the environment and delegates to
    :func:`sweep_expired_alerts`. Invoked on a fixed EventBridge schedule; the
    event payload is unused.

    Args:
        event: The scheduled invocation event (unused).
        context: The Lambda context (unused; present for the handler contract).

    Returns:
        A summary dict with the number of alerts auto-resolved.
    """
    alerts_table_name = _get_required_env(ENV_ALERTS_TABLE)
    resolved_ids = sweep_expired_alerts(alerts_table_name)
    return {"resolvedCount": len(resolved_ids)}


__all__ = [
    "ENV_ALERT_TIMEOUT_MINUTES",
    "DEFAULT_TIMEOUT_MINUTES",
    "sweep_expired_alerts",
    "lambda_handler",
]
