"""Conditional triage-brief attach and ALERT_UPDATED publish (Decision 8).

Because the alert is delivered immediately by the Rule Engine (Requirement 1.4),
the Triage Agent runs off the critical delivery path. When the brief is ready it
(1) attaches the ``triageBrief`` to the ``pulse-alerts`` item **conditional on
the alert not being terminal** (skip if RESOLVED -- idempotent), and (2)
publishes an ``ALERT_UPDATED`` event with ``hasTriageBrief=true`` to the
property channel via the shared ``pulse.delivery.realtime_publish`` helper
(best-effort: a publish failure never fails the invocation).

The conditional ``UpdateItem`` mirrors the ``pulse.api.alert_lifecycle`` idiom
(``ConditionExpression status <> RESOLVED``; catch
``ConditionalCheckFailedException``), so a brief is never attached to a resolved
alert and a duplicate/late invocation is a harmless no-op.

The persisted ``triageBrief`` uses the camelCase shape from the design Data
Models (matching the strict-JSON contract ``pulse.triage.validation`` parses and
the shape the PWA reads via ``GET /alerts/{alertId}``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from boto3.dynamodb.conditions import Attr

from pulse.common.dynamo import get_table
from pulse.common.logging import get_logger
from pulse.common.models import AlertStatus, RankedOption, TriageBrief, WalkStrategy
from pulse.delivery import realtime_publish as rt

logger = get_logger("pulse-triage-agent")

# The alert lifecycle's only terminal status (RESOLVED); the attach is guarded
# against it so a brief is never written to a resolved alert (idempotent).
_TERMINAL_STATUS = AlertStatus.RESOLVED.value


def _to_decimal(value: Any) -> Any:
    """Recursively convert Python floats to ``Decimal`` for DynamoDB writes.

    The boto3 DynamoDB *resource* interface rejects native ``float`` values with
    ``TypeError: Float types are not supported. Use Decimal types instead``. The
    triage brief carries floats (notably a Complaint option's ``estimatedCost``
    and Walk_Strategy compensation costs), so writing ``brief_item`` verbatim via
    ``update_item`` crashed the whole invocation and the brief never attached -
    only for the types that carry a cost (Complaint), which is why Complaint
    Escalation alone showed no "Agent ready" badge.

    Uses ``Decimal(str(value))`` so the stored number matches the human-readable
    float (e.g. 480.0) rather than its binary-float expansion. ``bool`` is left
    untouched (it is a valid DynamoDB type and a subclass of ``int``).

    Args:
        value: Any JSON-like value (dict, list, float, int, str, bool, None).

    Returns:
        The value with every nested ``float`` converted to ``Decimal``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {key: _to_decimal(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_decimal(item) for item in value]
    return value


@dataclass(frozen=True)
class AttachResult:
    """Outcome of the conditional attach + publish.

    Attributes:
        attached: Whether the triage brief was attached (``False`` when the
            alert was terminal, or not found).
        published: Whether an ``ALERT_UPDATED`` event was published.
        reason: A short machine-friendly reason when not attached, else ``None``.
    """

    attached: bool
    published: bool
    reason: Optional[str] = None


def _option_to_item(option: RankedOption) -> dict[str, Any]:
    """Serialize a ranked option to its camelCase persisted shape.

    Args:
        option: The ranked option.

    Returns:
        A camelCase dict; optional cost/review-risk are included only when set.
    """
    item: dict[str, Any] = {
        "label": option.label,
        "rank": option.rank,
        "title": option.title,
        "detail": option.detail,
        "recommended": option.recommended,
    }
    if option.estimated_cost is not None:
        item["estimatedCost"] = option.estimated_cost
    if option.review_risk is not None:
        item["reviewRisk"] = option.review_risk.value
    return item


def _walk_strategy_to_item(strategy: WalkStrategy) -> dict[str, Any]:
    """Serialize a Walk_Strategy to its camelCase persisted shape.

    Args:
        strategy: The walk strategy.

    Returns:
        A camelCase dict mirroring ``triageBrief.walkStrategy``.
    """
    return {
        "sisterPropertyId": strategy.sister_property_id,
        "sisterPropertyAvailable": strategy.sister_property_available,
        "walkableGuests": [
            {
                "guestId": guest.get("guest_id"),
                "loyaltyTier": guest.get("loyalty_tier"),
                "reservationId": guest.get("reservation_id"),
            }
            for guest in strategy.walkable_guests
        ],
        "compensation": [
            {
                "guestId": pkg.get("guest_id"),
                "description": pkg.get("description"),
                "estimatedCost": pkg.get("estimated_cost"),
            }
            for pkg in strategy.compensation
        ],
    }


def brief_to_item(brief: TriageBrief) -> dict[str, Any]:
    """Serialize a ``TriageBrief`` to the camelCase ``triageBrief`` DynamoDB shape.

    Args:
        brief: The validated triage brief.

    Returns:
        A camelCase dict suitable for the ``triageBrief`` attribute on
        ``pulse-alerts`` and the ``GET /alerts/{alertId}`` response.
    """
    item: dict[str, Any] = {
        "summary": brief.summary,
        "confidence": brief.confidence,
        "options": [_option_to_item(option) for option in brief.options],
    }
    if brief.walk_strategy is not None:
        item["walkStrategy"] = _walk_strategy_to_item(brief.walk_strategy)
    if brief.execute_label is not None:
        item["executeLabel"] = brief.execute_label
    return item


def attach_and_publish(
    alert_id: str,
    brief: TriageBrief,
    *,
    alerts_table_name: str,
    now: str,
    table_getter: Any = get_table,
    realtime_publisher: Optional[rt.PublisherFn] = None,
) -> AttachResult:
    """Attach the brief to a non-terminal alert and publish ALERT_UPDATED.

    Reads the current alert item (for the event payload and an early terminal
    check), then performs a conditional ``UpdateItem`` that only succeeds while
    the alert is not RESOLVED. On success, publishes a full-enough
    ``ALERT_UPDATED`` event with ``hasTriageBrief=true`` to the property channel
    (best-effort; a publish failure is logged and never raised).

    Args:
        alert_id: The alert to attach the brief to.
        brief: The validated triage brief.
        alerts_table_name: The ``pulse-alerts`` physical table name.
        now: ISO 8601 UTC timestamp to record as ``lastStatusChangeAt``.
        table_getter: Table-resource getter seam (injectable for tests).
        realtime_publisher: Realtime publisher seam (default resolved when
            omitted).

    Returns:
        An :class:`AttachResult` describing whether the attach and publish
        happened.
    """
    table = table_getter(alerts_table_name)

    # Load the current item: needed for the ALERT_UPDATED payload (title, tier,
    # status, escalationStatus) and to short-circuit when already terminal.
    existing = table.get_item(Key={"alertId": alert_id}).get("Item")
    if not existing:
        logger.info(
            "Alert not found; skipping triage brief attach",
            extra={"alertId": alert_id},
        )
        return AttachResult(attached=False, published=False, reason="not-found")
    if str(existing.get("status")) == _TERMINAL_STATUS:
        logger.info(
            "Alert already terminal; skipping triage brief attach (idempotent)",
            extra={"alertId": alert_id},
        )
        return AttachResult(attached=False, published=False, reason="already-resolved")

    brief_item = brief_to_item(brief)
    # DynamoDB's resource interface rejects native floats; convert every nested
    # float (e.g. a Complaint option's estimatedCost) to Decimal before writing.
    brief_item_ddb = _to_decimal(brief_item)
    try:
        # Conditional attach: only while the alert is not RESOLVED. This is the
        # authoritative idempotence guard against a race with a resolve.
        table.update_item(
            Key={"alertId": alert_id},
            UpdateExpression="SET triageBrief = :brief, lastStatusChangeAt = :ts",
            ConditionExpression=Attr("status").ne(_TERMINAL_STATUS),
            ExpressionAttributeValues={":brief": brief_item_ddb, ":ts": now},
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        logger.info(
            "Alert became terminal before attach; skipping (idempotent)",
            extra={"alertId": alert_id},
        )
        return AttachResult(attached=False, published=False, reason="already-resolved")

    # Build the full-enough event from the updated item (hasTriageBrief=true).
    # Use the Decimal-normalized brief so the published payload matches what was
    # persisted (the realtime encoder renders Decimal cleanly).
    updated_item: Mapping[str, Any] = {
        **existing,
        "triageBrief": brief_item_ddb,
        "lastStatusChangeAt": now,
    }
    rt.realtime_publish(
        rt.EVENT_ALERT_UPDATED, updated_item, publisher=realtime_publisher
    )
    logger.info(
        "Triage brief attached and ALERT_UPDATED published",
        extra={
            "alertId": alert_id,
            "optionCount": len(brief.options),
            "confidence": brief.confidence,
        },
    )
    return AttachResult(attached=True, published=True)


__all__ = ["AttachResult", "brief_to_item", "attach_and_publish"]
