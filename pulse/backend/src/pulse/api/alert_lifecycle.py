"""Alert lifecycle transitions and the Article 14 approval gate (Requirement 12).

This module owns the **pure** status-transition logic for an alert
(acknowledge, resolve, and the approval state machine) plus the thin
orchestration that persists a transition and, on approval, invokes the Action
Executor behind an injectable seam. Keeping the transition logic pure makes the
monotonic-status contract (Property 19) and the human-approval gate (Property 7)
unit-testable without DynamoDB.

Transition contract (Property 19):
    * ``UNACKNOWLEDGED``/``ESCALATED`` -> ``ACKNOWLEDGED`` on acknowledge.
    * ``UNACKNOWLEDGED``/``ACKNOWLEDGED``/``ESCALATED`` -> ``RESOLVED`` on resolve.
    * ``RESOLVED`` is terminal: any acknowledge/resolve on a resolved alert is
      rejected with status and timestamps left unchanged (monotonic).
    * Acknowledge sets an acknowledging user and UTC timestamp; resolve sets a
      resolving user and UTC timestamp.

Approval gate (Property 7, EU AI Act Article 14):
    * A ranked-option action executes **only** when a GM approval is recorded
      (``approval.state`` transitions ``PENDING`` -> ``APPROVED``). No action is
      ever executed without that recorded approval.
    * Rejecting leaves the alert unexecuted in a rejected-pending state
      (``approval.state`` -> ``REJECTED``); the executor seam is never called.

The Action Executor itself is delivered in Task 18. Until it is wired, the
default seam records the deferred intent and does not perform a write-back; the
approval state is still recorded so the gate behaves correctly (Requirement
10.3 / 10.7 are enforced here regardless of executor availability).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Optional

from boto3.dynamodb.conditions import Attr

from pulse.common.dynamo import get_table
from pulse.common.logging import get_logger
from pulse.common.models import AlertStatus, ApprovalState
from pulse.delivery import realtime_publish as rt

logger = get_logger("pulse-api")

# Statuses an acknowledge may transition from (Requirement 6.5, 12.1). ESCALATED
# is included because acknowledgement halts an in-flight escalation.
_ACK_FROM = frozenset({AlertStatus.UNACKNOWLEDGED, AlertStatus.ESCALATED})

# Statuses a resolve may transition from (Requirement 12.2).
_RESOLVE_FROM = frozenset(
    {AlertStatus.UNACKNOWLEDGED, AlertStatus.ACKNOWLEDGED, AlertStatus.ESCALATED}
)

# ``status`` is a DynamoDB reserved word; alias it in every update expression.
_STATUS_NAME_MAP = {"#s": "status"}

# The Action Executor seam: given the alert item, the approved option label, and
# the approving user, perform the write-back + resolution and return a summary.
# Defaults to a not-yet-wired no-op (Task 18 injects the real executor).
ActionExecutorFn = Callable[[Mapping[str, Any], str, str], dict[str, Any]]


@dataclass(frozen=True)
class TransitionResult:
    """The pure outcome of an acknowledge/resolve transition.

    Attributes:
        changed: Whether the transition changes the alert's status.
        new_status: The status to apply when ``changed`` is ``True``; otherwise
            the unchanged current status.
        rejected: Whether the transition was rejected (invalid or terminal).
        reason: A short machine-friendly reason when rejected, else ``None``.
    """

    changed: bool
    new_status: AlertStatus
    rejected: bool = False
    reason: Optional[str] = None


@dataclass(frozen=True)
class ApprovalDecisionResult:
    """The pure outcome of an approval-gate decision.

    Attributes:
        accepted: Whether the decision was applied to the approval record.
        new_state: The approval state to persist when ``accepted``; otherwise
            the unchanged current state.
        should_execute: Whether the Action Executor should be invoked. ``True``
            only for an accepted approval (a recorded GM approval), enforcing the
            Article 14 gate (Property 7).
        reason: A short machine-friendly reason when not accepted, else ``None``.
    """

    accepted: bool
    new_state: ApprovalState
    should_execute: bool = False
    reason: Optional[str] = None


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a ``Z`` suffix.

    Returns:
        The current time, e.g. ``"2026-08-17T14:33:10Z"``.
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Pure transitions (Property 19)
# ---------------------------------------------------------------------------


def plan_acknowledge(current: AlertStatus) -> TransitionResult:
    """Compute the acknowledge transition for a current status (pure).

    Args:
        current: The alert's current status.

    Returns:
        A :class:`TransitionResult`: a change to ``ACKNOWLEDGED`` from an open
        status, or a rejection (with the status unchanged) when the alert is
        already resolved or otherwise not acknowledgeable.
    """
    if current is AlertStatus.RESOLVED:
        return TransitionResult(
            False, current, rejected=True, reason="already-resolved"
        )
    if current in _ACK_FROM:
        return TransitionResult(True, AlertStatus.ACKNOWLEDGED)
    return TransitionResult(False, current, rejected=True, reason="invalid-transition")


def plan_resolve(current: AlertStatus) -> TransitionResult:
    """Compute the resolve transition for a current status (pure).

    Args:
        current: The alert's current status.

    Returns:
        A :class:`TransitionResult`: a change to ``RESOLVED`` from an open
        status, or a rejection (with the status unchanged) when the alert is
        already resolved or otherwise not resolvable.
    """
    if current is AlertStatus.RESOLVED:
        return TransitionResult(
            False, current, rejected=True, reason="already-resolved"
        )
    if current in _RESOLVE_FROM:
        return TransitionResult(True, AlertStatus.RESOLVED)
    return TransitionResult(False, current, rejected=True, reason="invalid-transition")


def plan_approval_decision(
    current_state: ApprovalState, decision: str
) -> ApprovalDecisionResult:
    """Compute the approval-gate decision (pure, Property 7).

    A decision applies only while the approval is ``PENDING``; a second decision
    on an already-decided approval is rejected. An ``approve`` decision yields
    ``should_execute = True`` (the recorded GM approval that authorizes the
    action); a ``reject`` decision records ``REJECTED`` and never authorizes
    execution.

    Args:
        current_state: The current approval state.
        decision: The requested decision, ``"approve"`` or ``"reject"``
            (case-insensitive).

    Returns:
        An :class:`ApprovalDecisionResult`.
    """
    normalized = decision.strip().lower()
    if normalized not in {"approve", "reject"}:
        return ApprovalDecisionResult(
            False, current_state, reason="invalid-decision"
        )
    if current_state is not ApprovalState.PENDING:
        return ApprovalDecisionResult(
            False, current_state, reason="already-decided"
        )
    if normalized == "approve":
        return ApprovalDecisionResult(True, ApprovalState.APPROVED, should_execute=True)
    return ApprovalDecisionResult(True, ApprovalState.REJECTED, should_execute=False)


# ---------------------------------------------------------------------------
# Orchestration (I/O behind seams)
# ---------------------------------------------------------------------------


def _current_status(item: Mapping[str, Any]) -> AlertStatus:
    """Read the current :class:`AlertStatus` from an alert item.

    Args:
        item: A ``pulse-alerts`` item.

    Returns:
        The parsed status (defaults to ``UNACKNOWLEDGED`` when absent).
    """
    return AlertStatus(item.get("status", AlertStatus.UNACKNOWLEDGED.value))


def acknowledge_alert(
    alert_id: str,
    user_id: str,
    *,
    item: Mapping[str, Any],
    alerts_table_name: str,
    table_getter: Callable[[str], Any] = get_table,
    realtime_publisher: Optional[rt.PublisherFn] = None,
    now: Optional[str] = None,
) -> TransitionResult:
    """Acknowledge an alert and publish an ``ALERT_UPDATED`` realtime event.

    Applies :func:`plan_acknowledge`; on a real transition it performs a guarded
    conditional update (only while the alert is still open) recording the
    acknowledging user and UTC timestamp (Requirement 12.1), then publishes an
    ``ALERT_UPDATED`` event to the property channel via
    :mod:`pulse.delivery.realtime_publish` (best-effort). A rejected transition
    leaves the item unchanged.

    Args:
        alert_id: The alert to acknowledge.
        user_id: The acknowledging user identifier.
        item: The current alert item (already loaded and scoped by the caller).
        alerts_table_name: The ``pulse-alerts`` physical table name.
        table_getter: Table-resource getter seam (injectable for tests).
        realtime_publisher: Realtime publisher seam (default resolved when
            omitted).
        now: ISO 8601 timestamp override (injectable for tests).

    Returns:
        The :class:`TransitionResult` describing the outcome.
    """
    result = plan_acknowledge(_current_status(item))
    if not result.changed:
        return result
    timestamp = now or utc_now_iso()
    applied = _guarded_status_update(
        alert_id,
        table=table_getter(alerts_table_name),
        update_expression=(
            "SET #s = :status, acknowledgedBy = :user, acknowledgedAt = :ts, "
            "lastStatusChangeAt = :ts"
        ),
        values={
            ":status": AlertStatus.ACKNOWLEDGED.value,
            ":user": user_id,
            ":ts": timestamp,
        },
        allowed_from=_ACK_FROM,
    )
    if not applied:
        return TransitionResult(
            False, _current_status(item), rejected=True, reason="already-resolved"
        )
    updated_item = {
        **item,
        "status": AlertStatus.ACKNOWLEDGED.value,
        "acknowledgedBy": user_id,
        "acknowledgedAt": timestamp,
        "lastStatusChangeAt": timestamp,
    }
    rt.realtime_publish(
        rt.EVENT_ALERT_UPDATED, updated_item, publisher=realtime_publisher
    )
    logger.info(
        "Alert acknowledged",
        extra={"alertId": alert_id, "acknowledgedBy": user_id},
    )
    return result


def resolve_alert(
    alert_id: str,
    user_id: str,
    *,
    item: Mapping[str, Any],
    alerts_table_name: str,
    table_getter: Callable[[str], Any] = get_table,
    now: Optional[str] = None,
) -> TransitionResult:
    """Resolve an alert, recording the resolving user and UTC timestamp.

    Applies :func:`plan_resolve`; on a real transition it performs a guarded
    conditional update (only while the alert is not already resolved) recording
    the resolving user and timestamp (Requirement 12.2). A resolve on an
    already-resolved alert is rejected with state unchanged (Requirement 12.5).

    Args:
        alert_id: The alert to resolve.
        user_id: The resolving user identifier.
        item: The current alert item (already loaded and scoped by the caller).
        alerts_table_name: The ``pulse-alerts`` physical table name.
        table_getter: Table-resource getter seam (injectable for tests).
        now: ISO 8601 timestamp override (injectable for tests).

    Returns:
        The :class:`TransitionResult` describing the outcome.
    """
    result = plan_resolve(_current_status(item))
    if not result.changed:
        return result
    timestamp = now or utc_now_iso()
    applied = _guarded_status_update(
        alert_id,
        table=table_getter(alerts_table_name),
        update_expression=(
            "SET #s = :status, resolvedBy = :user, resolvedAt = :ts, "
            "lastStatusChangeAt = :ts"
        ),
        values={
            ":status": AlertStatus.RESOLVED.value,
            ":user": user_id,
            ":ts": timestamp,
        },
        allowed_from=_RESOLVE_FROM,
    )
    if not applied:
        return TransitionResult(
            False, _current_status(item), rejected=True, reason="already-resolved"
        )
    logger.info(
        "Alert resolved", extra={"alertId": alert_id, "resolvedBy": user_id}
    )
    return result


def _default_action_executor(
    alert_item: Mapping[str, Any], selected_option: str, user_id: str
) -> dict[str, Any]:
    """Not-yet-wired Action Executor seam (Task 18 injects the real one).

    Records that a GM approval was accepted and that execution is deferred until
    the Action Executor is wired. Crucially, this is only ever reached *after* a
    recorded approval, so the Article 14 gate holds regardless of executor
    availability (Property 7).

    Args:
        alert_item: The approved alert item.
        selected_option: The approved ranked-option label.
        user_id: The approving user identifier.

    Returns:
        A summary indicating the execution was deferred.
    """
    logger.info(
        "Approval recorded; action execution deferred (executor not yet wired)",
        extra={
            "alertId": alert_item.get("alertId"),
            "selectedOption": selected_option,
            "approvedBy": user_id,
        },
    )
    return {"executed": False, "reason": "action-executor-not-wired"}


def _option_labels(item: Mapping[str, Any]) -> set[str]:
    """Return the set of ranked-option labels on an alert's triage brief.

    Args:
        item: The alert item.

    Returns:
        The option labels, or an empty set when no brief/options are present.
    """
    brief = item.get("triageBrief") or {}
    options = brief.get("options") or [] if isinstance(brief, Mapping) else []
    return {str(option.get("label")) for option in options if option.get("label")}


def decide_approval(
    alert_id: str,
    user_id: str,
    decision: str,
    selected_option: Optional[str],
    *,
    item: Mapping[str, Any],
    alerts_table_name: str,
    table_getter: Callable[[str], Any] = get_table,
    action_executor: ActionExecutorFn = _default_action_executor,
) -> dict[str, Any]:
    """Record an approval decision and, on approval, invoke the Action Executor.

    Enforces the human-approval gate (Property 7): the Action Executor is invoked
    only when a GM approval is accepted (``PENDING`` -> ``APPROVED``). Rejection
    records ``REJECTED`` and never executes. Approval requires a
    ``selected_option`` that exists on the alert's triage brief when the brief
    carries options.

    Args:
        alert_id: The alert being decided.
        user_id: The deciding GM identifier.
        decision: ``"approve"`` or ``"reject"`` (case-insensitive).
        selected_option: The chosen ranked-option label (required to approve).
        item: The current alert item (already loaded and scoped by the caller).
        alerts_table_name: The ``pulse-alerts`` physical table name.
        table_getter: Table-resource getter seam (injectable for tests).
        action_executor: The Action Executor seam (defaults to the not-yet-wired
            executor; Task 18 injects the real one).

    Returns:
        A result dict with ``accepted``, ``approvalState``, ``executed`` and,
        when relevant, ``reason``.
    """
    if _current_status(item) is AlertStatus.RESOLVED:
        return {
            "accepted": False,
            "approvalState": ApprovalState.APPROVED.value,
            "executed": False,
            "reason": "already-resolved",
        }

    approval = item.get("approval") or {}
    current_state = ApprovalState(
        approval.get("state", ApprovalState.PENDING.value)
        if isinstance(approval, Mapping)
        else ApprovalState.PENDING.value
    )
    plan = plan_approval_decision(current_state, decision)
    if not plan.accepted:
        return {
            "accepted": False,
            "approvalState": current_state.value,
            "executed": False,
            "reason": plan.reason,
        }

    # An approval must name a valid option when the brief carries options.
    if plan.should_execute:
        labels = _option_labels(item)
        if labels and (selected_option not in labels):
            return {
                "accepted": False,
                "approvalState": current_state.value,
                "executed": False,
                "reason": "invalid-option",
            }

    timestamp = utc_now_iso()
    _persist_approval(
        alert_id,
        table=table_getter(alerts_table_name),
        new_state=plan.new_state,
        selected_option=selected_option,
        user_id=user_id,
        timestamp=timestamp,
    )

    executed_summary: dict[str, Any] = {"executed": False}
    if plan.should_execute:
        # Article 14 gate: the executor runs only past a recorded approval.
        executed_summary = action_executor(item, selected_option or "", user_id)

    logger.info(
        "Approval decision recorded",
        extra={
            "alertId": alert_id,
            "approvalState": plan.new_state.value,
            "decidedBy": user_id,
            "executed": executed_summary.get("executed"),
        },
    )
    return {
        "accepted": True,
        "approvalState": plan.new_state.value,
        "selectedOption": selected_option,
        "executed": bool(executed_summary.get("executed")),
        "execution": executed_summary,
    }


def _guarded_status_update(
    alert_id: str,
    *,
    table: Any,
    update_expression: str,
    values: dict[str, Any],
    allowed_from: frozenset[AlertStatus],
) -> bool:
    """Apply a status update guarded to a set of source statuses.

    Args:
        alert_id: The alert to update.
        table: The ``pulse-alerts`` table resource.
        update_expression: The DynamoDB update expression (aliases ``status`` as
            ``#s``).
        values: The expression attribute values.
        allowed_from: The statuses the update is permitted to transition from.

    Returns:
        ``True`` when the update applied, ``False`` when the guard failed (the
        alert was no longer in an allowed source status).
    """
    allowed_values = [status.value for status in allowed_from]
    try:
        table.update_item(
            Key={"alertId": alert_id},
            UpdateExpression=update_expression,
            ConditionExpression=Attr("status").is_in(allowed_values),
            ExpressionAttributeNames=_STATUS_NAME_MAP,
            ExpressionAttributeValues=values,
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        logger.info(
            "Status update skipped; alert not in an allowed source status",
            extra={"alertId": alert_id},
        )
        return False


def _persist_approval(
    alert_id: str,
    *,
    table: Any,
    new_state: ApprovalState,
    selected_option: Optional[str],
    user_id: str,
    timestamp: str,
) -> None:
    """Persist an approval-record update on an alert, guarded against resolved.

    Args:
        alert_id: The alert to update.
        table: The ``pulse-alerts`` table resource.
        new_state: The approval state to persist.
        selected_option: The chosen option label (may be ``None`` on reject).
        user_id: The deciding user identifier.
        timestamp: The ISO 8601 UTC decision timestamp.
    """
    approval_value = {
        "state": new_state.value,
        "selectedOption": selected_option,
        "decidedBy": user_id,
        "decidedAt": timestamp,
    }
    try:
        table.update_item(
            Key={"alertId": alert_id},
            UpdateExpression="SET approval = :approval, lastStatusChangeAt = :ts",
            ConditionExpression=Attr("status").ne(AlertStatus.RESOLVED.value),
            ExpressionAttributeValues={
                ":approval": approval_value,
                ":ts": timestamp,
            },
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        # The alert was resolved between the read and the write: leave it be.
        logger.info(
            "Approval update skipped; alert already resolved",
            extra={"alertId": alert_id},
        )


__all__ = [
    "ActionExecutorFn",
    "TransitionResult",
    "ApprovalDecisionResult",
    "utc_now_iso",
    "plan_acknowledge",
    "plan_resolve",
    "plan_approval_decision",
    "acknowledge_alert",
    "resolve_alert",
    "decide_approval",
]
