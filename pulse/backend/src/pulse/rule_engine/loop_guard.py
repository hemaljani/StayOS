"""Closed-loop safety-net resolution for the Rule Engine (design Decision 6).

When a condition-clearing change re-enters the Rule Engine via Streams, two
things must hold (Properties 26, 27):

    * **No duplicate alert.** The evaluators already guarantee this: a cleared
      condition makes the evaluator return no draft, and the deterministic
      ``alertId`` + ``attribute_not_exists`` conditional write suppress any
      echo (Property 16). This module adds nothing there.
    * **The originating alert resolves.** On the primary path the Action
      Executor sets ``RESOLVED`` transactionally, so by the time the write-back
      echoes back the correlated alert is already terminal and this module is a
      no-op. On the safety-net path -- a condition cleared by a *non-executor*
      source (a real PMS write, a manual fix, or a demo ``reset``) -- this module
      resolves the still-open correlated alert.

The detection (:func:`pulse.rule_engine.correlation.detect_cleared_dedupe_keys`)
is pure; the resolution here is a single guarded ``UpdateItem`` per correlated
alert. The guard (``status`` in the open set) makes it idempotent: a terminal or
non-existent alert yields a no-op, so a repeated or echoed clear never changes a
resolved alert (Property 27). A best-effort ``ALERT_RESOLVED`` realtime event is
published on each auto-resolution.
"""

from __future__ import annotations

from typing import Any, Optional

from boto3.dynamodb.conditions import Attr

from pulse.common.logging import get_logger
from pulse.common.models import AlertStatus, OperationalChange, RuleDefinition
from pulse.delivery import realtime_publish as rt
from pulse.rule_engine.alert_factory import derive_alert_id, utc_now_iso
from pulse.rule_engine.correlation import detect_cleared_dedupe_keys

logger = get_logger("pulse-rule-evaluator")

# ``status`` is a DynamoDB reserved word; alias it in the update/condition.
_STATUS_NAME_MAP = {"#status": "status"}

# The statuses an auto-resolution may transition from. RESOLVED and
# ESCALATION_EXHAUSTED are terminal and are left untouched.
_OPEN_STATUSES = (
    AlertStatus.UNACKNOWLEDGED.value,
    AlertStatus.ACKNOWLEDGED.value,
    AlertStatus.ESCALATED.value,
)


def _auto_resolve(
    alert_id: str, table: Any, now: str
) -> Optional[dict[str, Any]]:
    """Resolve a still-open correlated alert, guarded and idempotent.

    Args:
        alert_id: The correlated alert to resolve.
        table: The ``pulse-alerts`` DynamoDB table resource.
        now: The ISO 8601 UTC resolution timestamp.

    Returns:
        The updated alert item when a still-open alert was resolved, or ``None``
        when the alert did not exist or was already terminal (a no-op).
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
                ":user": "system-auto",
                ":ts": now,
            },
            ReturnValues="ALL_NEW",
        )
        return response.get("Attributes")
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        # No open correlated alert to resolve (missing or already terminal):
        # a benign no-op that keeps echoed/duplicate clears idempotent.
        return None


def resolve_cleared_correlations(
    change: OperationalChange,
    rules: list[RuleDefinition],
    alerts_table: Any,
    *,
    realtime_publisher: Optional[rt.PublisherFn] = None,
    now: Optional[str] = None,
) -> list[str]:
    """Resolve open correlated alerts whose trigger condition has cleared.

    For each enabled resolvable rule, detects the correlated dedupe keys whose
    condition is now false (safety-net for non-executor clears) and resolves the
    still-open originating alert, publishing an ``ALERT_RESOLVED`` event
    best-effort. Terminal/absent alerts are no-ops, so this is safe to run on
    every change including the executor's own echoed write-back.

    Args:
        change: The normalized operational change.
        rules: The enabled rule definitions for the change's property.
        alerts_table: The ``pulse-alerts`` DynamoDB table resource.
        realtime_publisher: The realtime publisher seam (default resolved when
            omitted).
        now: ISO 8601 timestamp override (injectable for tests).

    Returns:
        The alert ids that were auto-resolved (empty when none were open).
    """
    timestamp = now or utc_now_iso()
    resolved_ids: list[str] = []
    seen: set[str] = set()
    for rule in rules:
        for dedupe_key in detect_cleared_dedupe_keys(change, rule):
            alert_id = derive_alert_id(dedupe_key)
            if alert_id in seen:
                continue
            seen.add(alert_id)
            updated = _auto_resolve(alert_id, alerts_table, timestamp)
            if updated is None:
                continue
            resolved_ids.append(alert_id)
            logger.info(
                "Auto-resolved correlated alert on cleared condition",
                extra={
                    "alertId": alert_id,
                    "ruleType": rule.rule_type.value,
                    "propertyId": change.property_id,
                },
            )
            rt.realtime_publish(
                rt.EVENT_ALERT_RESOLVED, updated, publisher=realtime_publisher
            )
    return resolved_ids


__all__ = ["resolve_cleared_correlations"]
