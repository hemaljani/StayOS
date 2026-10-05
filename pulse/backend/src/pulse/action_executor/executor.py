"""Closed-loop Action Executor (``pulse-action-executor``), Component 8.

Turns a GM-approved ranked option into a **real write-back mutation** on the
operational tables that clears the triggering condition, and reflects the
resolution on the originating alert -- both in a single DynamoDB
``TransactWriteItems`` guarded by ``status <> RESOLVED``. This is what makes the
PULSE lifecycle a closed loop (Requirement 12.3, design Decision 6).

Guarantees enforced here:
    * **All-or-nothing (Requirement 12.6, Property 26).** The condition-clearing
      operational write and the ``pulse-alerts`` ``status -> RESOLVED`` update
      share one transaction. A failed write-back commits nothing: the alert
      status and resolution timestamps are left unchanged and a
      :class:`~pulse.common.errors.WriteBackError` is raised.
    * **Once-only / idempotent (Property 27).** The transaction condition
      (``status <> RESOLVED``) makes a duplicate approval a no-op -- the action
      executes at most once.
    * **Correlation, not authorship.** Resolution targets the *originating*
      alert; the subsequent Stream re-evaluation of the write-back is a no-op
      because the condition is now false and the alert is already terminal
      (loop-guard, :mod:`pulse.rule_engine.loop_guard`).

Design points (PYQUALITY):
    * The transaction-item construction (:func:`build_transaction_items` and the
      per-type builders) is **pure** and unit-testable without AWS.
    * The ``TransactWriteItems`` call sits behind the injectable
      :data:`TransactWriter` seam; the default writer uses the shared
      auto-marshalling DynamoDB client.
    * The realtime ``ALERT_RESOLVED`` publish is **post-commit** and best-effort
      (via :mod:`pulse.delivery.realtime_publish`), so a publish failure never
      affects the committed resolution.
    * :func:`make_action_executor` returns an adapter matching the
      :data:`pulse.api.alert_lifecycle.ActionExecutorFn` seam, so the API wires
      the real executor into the approval gate.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Optional

from pulse.common import operational_schema as ops
from pulse.common.config import load_config
from pulse.common.dynamo import get_dynamo_client
from pulse.common.errors import WriteBackError
from pulse.common.logging import get_logger
from pulse.common.models import AlertStatus, AlertType, ApprovalState
from pulse.common.tracing import get_tracer
from pulse.delivery import realtime_publish as rt

logger = get_logger("pulse-action-executor")
tracer = get_tracer("pulse-action-executor")

# ``status`` and (nested) ``state`` are DynamoDB reserved words; alias both in
# every expression that references them.
_ALERT_NAME_MAP = {"#status": "status", "#astate": "state"}

# The room status value that means a room is ready (must match the evaluator's
# ROOM_STATUS_READY so the VIP write-back actually clears the condition).
ROOM_STATUS_READY = "Ready"

# A writer takes a list of TransactWriteItems entries and performs the
# transaction, raising on failure. Injectable so tests never touch AWS.
TransactWriter = Callable[[Sequence[Mapping[str, Any]]], None]

# The alert types the executor can resolve with a write-back (design Component
# 8). INFO types (premium cancellation, VIP check-in) are informational and are
# never executed. FORECAST_OVERSELL is intentionally excluded (advisory-only):
# it is preserved unchanged and rejected as non-resolvable by write-back.
RESOLVABLE_TYPES = frozenset(
    {
        AlertType.WALK_RISK,
        AlertType.VIP_ROOM_NOT_READY,
        AlertType.COMPLAINT_ESCALATION,
        AlertType.OOO_CLUSTER,
    }
)


@dataclass(frozen=True)
class ExecutionResult:
    """The outcome of an executed, approved resolving action.

    Attributes:
        executed: Whether the write-back + resolution transaction committed.
        alert_id: The resolved alert identifier.
        new_status: The alert status after execution (``RESOLVED`` on success).
        resolved_at: The ISO 8601 UTC resolution timestamp, when resolved.
    """

    executed: bool
    alert_id: str
    new_status: AlertStatus
    resolved_at: Optional[str] = None


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a ``Z`` suffix.

    Returns:
        The current time, e.g. ``"2026-08-17T14:40:00Z"``.
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _source_ref(alert_item: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the alert's source-entity correlation reference.

    Args:
        alert_item: The ``pulse-alerts`` item.

    Returns:
        The ``sourceEntityRef`` mapping.

    Raises:
        WriteBackError: If the alert carries no source-entity reference (it
            cannot be correlated back to an operational item to clear).
    """
    ref = alert_item.get("sourceEntityRef")
    if not isinstance(ref, Mapping) or not ref.get("entityKey"):
        raise WriteBackError(
            "Alert has no sourceEntityRef.entityKey to write back to",
            alert_id=str(alert_item.get("alertId")),
            detail="missing-source-ref",
        )
    return ref


def _require_table(name: str, table_kind: str, alert_id: str) -> str:
    """Return a required operational table name or raise a write-back error.

    Args:
        name: The resolved table name (may be empty when unconfigured).
        table_kind: A short description for the error (e.g. ``"reservations"``).
        alert_id: The alert being executed, for error context.

    Returns:
        The non-empty table name.

    Raises:
        WriteBackError: If the table name is not configured.
    """
    if not name:
        raise WriteBackError(
            f"Operational {table_kind} table name is not configured",
            alert_id=alert_id,
            detail=f"missing-{table_kind}-table",
        )
    return name


def build_walk_risk_writeback(
    alert_item: Mapping[str, Any], user_id: str
) -> dict[str, Any]:
    """Build the Walk Risk operational write-back (relocate to sister property).

    Sets the origin arrival aggregate's ``confirmedReservations`` down to the
    current ``availableRooms`` value, so confirmed no longer exceeds available
    and the Walk Risk trigger evaluates false on re-evaluation.

    Args:
        alert_item: The ``pulse-alerts`` item (carries ``sourceEntityRef``).
        user_id: The approving GM, recorded on the aggregate.

    Returns:
        A ``TransactWriteItems`` ``Update`` entry for ``stayos-reservations``.
    """
    ref = _source_ref(alert_item)
    alert_id = str(alert_item.get("alertId"))
    table = _require_table(ops.reservations_table_name(), "reservations", alert_id)
    return {
        "Update": {
            "TableName": table,
            "Key": ops.walk_reservation_key(ref["propertyId"], ref["entityKey"]),
            # Relocating the walked reservations reduces confirmed to available.
            "UpdateExpression": (
                "SET confirmedReservations = availableRooms, "
                "walkRelocatedBy = :user"
            ),
            "ConditionExpression": "attribute_exists(propertyId)",
            "ExpressionAttributeValues": {":user": user_id},
        }
    }


def build_vip_room_writeback(
    alert_item: Mapping[str, Any], option: Mapping[str, Any], user_id: str
) -> dict[str, Any]:
    """Build the VIP Room Not Ready write-back (room-move or rush-clean).

    Both option kinds clear the condition by setting the arrival record's
    ``assignedRoomStatus`` to ``Ready`` (the attribute the evaluator checks). A
    room-move option additionally reassigns ``assignedRoomId`` when the option
    names a target room.

    Args:
        alert_item: The ``pulse-alerts`` item (carries ``sourceEntityRef``).
        option: The approved ranked option (may name a target room via
            ``assignedRoomId``/``newRoomId``).
        user_id: The approving GM, recorded on the arrival record.

    Returns:
        A ``TransactWriteItems`` ``Update`` entry for ``stayos-reservations``.
    """
    ref = _source_ref(alert_item)
    alert_id = str(alert_item.get("alertId"))
    table = _require_table(ops.reservations_table_name(), "reservations", alert_id)
    values: dict[str, Any] = {":ready": ROOM_STATUS_READY, ":user": user_id}
    set_clauses = ["assignedRoomStatus = :ready", "vipResolvedBy = :user"]
    new_room = option.get("assignedRoomId") or option.get("newRoomId")
    if new_room:
        set_clauses.append("assignedRoomId = :room")
        values[":room"] = str(new_room)
    return {
        "Update": {
            "TableName": table,
            "Key": ops.vip_arrival_key(ref["propertyId"], ref["entityKey"]),
            "UpdateExpression": "SET " + ", ".join(set_clauses),
            "ConditionExpression": "attribute_exists(propertyId)",
            "ExpressionAttributeValues": values,
        }
    }


def build_complaint_writeback(
    alert_item: Mapping[str, Any], option: Mapping[str, Any], user_id: str
) -> dict[str, Any]:
    """Build the Complaint Escalation write-back (record remedy, clear flag).

    Persists the chosen remedy on the complaint record and clears the SPOG
    ``complaintEscalationFlag`` so the trigger evaluates false on re-evaluation.

    Args:
        alert_item: The ``pulse-alerts`` item (carries ``sourceEntityRef``).
        option: The approved ranked option (its label/title is the remedy).
        user_id: The approving GM, recorded on the complaint record.

    Returns:
        A ``TransactWriteItems`` ``Update`` entry for ``stayos-guests``.
    """
    ref = _source_ref(alert_item)
    alert_id = str(alert_item.get("alertId"))
    table = _require_table(ops.guests_table_name(), "guests", alert_id)
    remedy = str(option.get("title") or option.get("label") or "resolved")
    return {
        "Update": {
            "TableName": table,
            "Key": ops.complaint_key(ref["propertyId"], ref["entityKey"]),
            "UpdateExpression": (
                "SET complaintEscalationFlag = :false, remedy = :remedy, "
                "remedyBy = :user"
            ),
            "ConditionExpression": "attribute_exists(propertyId)",
            "ExpressionAttributeValues": {
                ":false": False,
                ":remedy": remedy,
                ":user": user_id,
            },
        }
    }


def build_ooo_writeback(
    alert_item: Mapping[str, Any], user_id: str
) -> dict[str, Any]:
    """Build the OOO Cluster write-back (reassign the group to replacements).

    Clears the out-of-order cluster on the property snapshot (``oooRooms`` set
    empty), so no cluster overlaps the group block on re-evaluation.

    Args:
        alert_item: The ``pulse-alerts`` item (carries ``sourceEntityRef``).
        user_id: The approving GM, recorded on the snapshot.

    Returns:
        A ``TransactWriteItems`` ``Update`` entry for ``stayos-rooms``.
    """
    ref = _source_ref(alert_item)
    alert_id = str(alert_item.get("alertId"))
    table = _require_table(ops.rooms_table_name(), "rooms", alert_id)
    return {
        "Update": {
            "TableName": table,
            "Key": ops.ooo_snapshot_key(ref["propertyId"]),
            "UpdateExpression": "SET oooRooms = :empty, reassignedBy = :user",
            "ConditionExpression": "attribute_exists(propertyId)",
            "ExpressionAttributeValues": {":empty": [], ":user": user_id},
        }
    }


def build_operational_writeback(
    alert_item: Mapping[str, Any], option: Mapping[str, Any], user_id: str
) -> dict[str, Any]:
    """Dispatch to the per-alert-type operational write-back builder (pure).

    Args:
        alert_item: The ``pulse-alerts`` item.
        option: The approved ranked option.
        user_id: The approving GM.

    Returns:
        A single ``TransactWriteItems`` ``Update`` entry for the operational
        table the alert type touches.

    Raises:
        WriteBackError: If the alert type is not a resolvable type.
    """
    alert_type = AlertType(str(alert_item.get("type")))
    if alert_type is AlertType.WALK_RISK:
        return build_walk_risk_writeback(alert_item, user_id)
    if alert_type is AlertType.VIP_ROOM_NOT_READY:
        return build_vip_room_writeback(alert_item, option, user_id)
    if alert_type is AlertType.COMPLAINT_ESCALATION:
        return build_complaint_writeback(alert_item, option, user_id)
    if alert_type is AlertType.OOO_CLUSTER:
        return build_ooo_writeback(alert_item, user_id)
    raise WriteBackError(
        f"Alert type {alert_type.value} is not resolvable by write-back",
        alert_id=str(alert_item.get("alertId")),
        detail="non-resolvable-type",
    )


def build_alert_resolution(
    alert_item: Mapping[str, Any],
    user_id: str,
    now: str,
    *,
    alerts_table_name: str,
    selected_option: Optional[str] = None,
) -> dict[str, Any]:
    """Build the guarded ``pulse-alerts`` RESOLVED update (pure).

    Guarded by ``status <> RESOLVED`` so a repeated execution is a no-op
    (Property 27). Records the resolving user and UTC timestamp and marks the
    approval ``APPROVED`` (Requirements 12.3).

    Args:
        alert_item: The ``pulse-alerts`` item (for its ``alertId``).
        user_id: The approving/resolving GM.
        now: The ISO 8601 UTC resolution timestamp.
        alerts_table_name: The ``pulse-alerts`` physical table name.
        selected_option: The approved option label, recorded on the approval.

    Returns:
        A ``TransactWriteItems`` ``Update`` entry for ``pulse-alerts``.
    """
    values: dict[str, Any] = {
        ":resolved": AlertStatus.RESOLVED.value,
        ":user": user_id,
        ":ts": now,
        ":approved": ApprovalState.APPROVED.value,
    }
    set_clause = (
        "#status = :resolved, resolvedBy = :user, resolvedAt = :ts, "
        "lastStatusChangeAt = :ts, approval.#astate = :approved"
    )
    if selected_option is not None:
        set_clause += ", approval.selectedOption = :option"
        values[":option"] = selected_option
    return {
        "Update": {
            "TableName": alerts_table_name,
            "Key": {"alertId": alert_item["alertId"]},
            "UpdateExpression": "SET " + set_clause,
            "ConditionExpression": "#status <> :resolved",
            "ExpressionAttributeNames": dict(_ALERT_NAME_MAP),
            "ExpressionAttributeValues": values,
        }
    }


def build_transaction_items(
    alert_item: Mapping[str, Any],
    option: Mapping[str, Any],
    user_id: str,
    now: str,
    *,
    alerts_table_name: str,
) -> list[dict[str, Any]]:
    """Build the full write-back + resolution transaction (pure).

    Produces exactly two items -- the condition-clearing operational write and
    the guarded ``pulse-alerts`` RESOLVED update -- so they commit atomically
    (Requirement 12.6, Property 26).

    Args:
        alert_item: The ``pulse-alerts`` item.
        option: The approved ranked option.
        user_id: The approving GM.
        now: The ISO 8601 UTC resolution timestamp.
        alerts_table_name: The ``pulse-alerts`` physical table name.

    Returns:
        The list of ``TransactWriteItems`` entries.

    Raises:
        WriteBackError: If the alert type is not resolvable or the alert lacks
            a source-entity reference.
    """
    operational = build_operational_writeback(alert_item, option, user_id)
    resolution = build_alert_resolution(
        alert_item,
        user_id,
        now,
        alerts_table_name=alerts_table_name,
        selected_option=str(option.get("label")) if option.get("label") else None,
    )
    return [operational, resolution]


# ---------------------------------------------------------------------------
# Default transaction writer (behind the TransactWriter seam)
# ---------------------------------------------------------------------------


def _default_transact_writer(items: Sequence[Mapping[str, Any]]) -> None:
    """Execute a ``TransactWriteItems`` via the shared auto-marshalling client.

    Args:
        items: The transaction entries (native Python types; auto-marshalled).

    Raises:
        WriteBackError: If the transaction is cancelled (e.g. the alert was
            already RESOLVED, or an operational item is missing).
    """
    client = get_dynamo_client()
    try:
        client.transact_write_items(TransactItems=list(items))
    except client.exceptions.TransactionCanceledException as exc:
        # A guard failed (already resolved) or an operational item was missing:
        # nothing committed, so the alert is unchanged (Requirement 12.6).
        raise WriteBackError(
            "Write-back transaction was cancelled; nothing committed",
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def execute_approved_action(
    alert_item: Mapping[str, Any],
    option: Mapping[str, Any],
    user_id: str,
    *,
    alerts_table_name: str,
    transact_writer: TransactWriter = _default_transact_writer,
    realtime_publisher: Optional[rt.PublisherFn] = None,
    now: Optional[str] = None,
) -> ExecutionResult:
    """Execute an approved resolving action as one atomic write-back + resolve.

    Performs the condition-clearing operational write and the ``pulse-alerts``
    ``status -> RESOLVED`` update in a single transaction, then publishes an
    ``ALERT_RESOLVED`` realtime event post-commit (best-effort). If the
    transaction fails, nothing commits and a :class:`WriteBackError` propagates
    with the alert left unchanged (Requirement 12.6, Property 26).

    Args:
        alert_item: The approved ``pulse-alerts`` item.
        option: The approved ranked option.
        user_id: The approving GM identifier.
        alerts_table_name: The ``pulse-alerts`` physical table name.
        transact_writer: The transaction-writer seam (default uses the shared
            DynamoDB client).
        realtime_publisher: The realtime publisher seam (default resolved when
            omitted).
        now: ISO 8601 timestamp override (injectable for tests).

    Returns:
        An :class:`ExecutionResult` describing the committed resolution.

    Raises:
        WriteBackError: If the write-back/resolution transaction fails.
    """
    timestamp = now or utc_now_iso()
    items = build_transaction_items(
        alert_item, option, user_id, timestamp, alerts_table_name=alerts_table_name
    )
    transact_writer(items)

    # Post-commit, best-effort realtime publish so open PWAs move the card to
    # resolved history instantly. A publish failure never affects the committed
    # transaction (design Component 4a / Decision 6).
    resolved_item = {
        **alert_item,
        "status": AlertStatus.RESOLVED.value,
        "resolvedBy": user_id,
        "resolvedAt": timestamp,
        "lastStatusChangeAt": timestamp,
    }
    rt.realtime_publish(
        rt.EVENT_ALERT_RESOLVED, resolved_item, publisher=realtime_publisher
    )
    logger.info(
        "Approved action executed; alert resolved",
        extra={
            "alertId": alert_item.get("alertId"),
            "type": alert_item.get("type"),
            "resolvedBy": user_id,
        },
    )
    return ExecutionResult(
        executed=True,
        alert_id=str(alert_item["alertId"]),
        new_status=AlertStatus.RESOLVED,
        resolved_at=timestamp,
    )


def _find_option(alert_item: Mapping[str, Any], label: str) -> dict[str, Any]:
    """Return the ranked option with a label from the alert's triage brief.

    Args:
        alert_item: The ``pulse-alerts`` item.
        label: The approved option label.

    Returns:
        The matching option mapping, or an empty dict when none matches (the
        write-back for types that do not depend on the option, e.g. Walk Risk,
        still proceeds).
    """
    brief = alert_item.get("triageBrief")
    options = brief.get("options", []) if isinstance(brief, Mapping) else []
    for option in options:
        if isinstance(option, Mapping) and str(option.get("label")) == label:
            return dict(option)
    return {}


def make_action_executor(
    alerts_table_name: Optional[str] = None,
    *,
    transact_writer: TransactWriter = _default_transact_writer,
    realtime_publisher: Optional[rt.PublisherFn] = None,
) -> Callable[[Mapping[str, Any], str, str], dict[str, Any]]:
    """Build an adapter matching the API's ``ActionExecutorFn`` seam.

    The returned callable has the signature the approval gate expects
    (``(alert_item, selected_option_label, user_id) -> dict``): it looks up the
    approved option on the alert's triage brief, executes the write-back +
    resolution, and returns a summary. A :class:`WriteBackError` is caught and
    surfaced as ``{"executed": False, ...}`` so the approval remains recorded
    while the failed action leaves the alert unchanged (Requirement 12.6).

    Args:
        alerts_table_name: The ``pulse-alerts`` table name; read from
            configuration when omitted.
        transact_writer: The transaction-writer seam.
        realtime_publisher: The realtime publisher seam.

    Returns:
        An ``ActionExecutorFn``-compatible callable.
    """
    table_name = alerts_table_name or load_config().alerts_table

    def _execute(
        alert_item: Mapping[str, Any], selected_option: str, user_id: str
    ) -> dict[str, Any]:
        option = _find_option(alert_item, selected_option)
        try:
            result = execute_approved_action(
                alert_item,
                option,
                user_id,
                alerts_table_name=table_name,
                transact_writer=transact_writer,
                realtime_publisher=realtime_publisher,
            )
        except WriteBackError as exc:
            logger.error(
                "Approved action write-back failed; alert left unchanged",
                extra={
                    "alertId": alert_item.get("alertId"),
                    "detail": exc.detail,
                    "error": exc.message,
                },
            )
            return {
                "executed": False,
                "reason": "write-back-failed",
                "error": exc.message,
            }
        return {
            "executed": result.executed,
            "alertId": result.alert_id,
            "status": result.new_status.value,
            "resolvedAt": result.resolved_at,
        }

    return _execute


@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Action Executor Lambda entry point (standalone invocation).

    A thin orchestrator (PYQUALITY-05): it reads ``{alertId, selectedOption,
    userId}`` from the event, loads the alert item, and executes the approved
    action via the shared executor logic. In the primary (in-process) design the
    API invokes the executor directly on approval; this handler makes the
    ``pulse-action-executor`` function independently invocable (e.g. async
    re-drive) using the same package code.

    Args:
        event: ``{"alertId": str, "selectedOption": str, "userId": str}``.
        context: The Lambda context (unused; present for the handler contract).

    Returns:
        The execution summary dict.

    Raises:
        WriteBackError: If the alert is missing or the write-back fails (the
            caller can inspect and re-drive).
    """
    from pulse.common.dynamo import get_table

    alert_id = str(event["alertId"])
    user_id = str(event.get("userId", "system"))
    selected_option = str(event.get("selectedOption", ""))

    # Correlate the resolve segment with the alert so the closed loop
    # (rule-eval -> triage -> delivery -> resolve) shares an X-Ray annotation.
    tracer.put_annotation(key="alertId", value=alert_id)

    config = load_config()
    item = (
        get_table(config.alerts_table)
        .get_item(Key={"alertId": alert_id})
        .get("Item")
    )
    if item is None:
        raise WriteBackError(
            "Alert not found for execution", alert_id=alert_id, detail="not-found"
        )
    executor = make_action_executor(config.alerts_table)
    return executor(item, selected_option, user_id)


__all__ = [
    "ROOM_STATUS_READY",
    "RESOLVABLE_TYPES",
    "TransactWriter",
    "ExecutionResult",
    "utc_now_iso",
    "build_walk_risk_writeback",
    "build_vip_room_writeback",
    "build_complaint_writeback",
    "build_ooo_writeback",
    "build_operational_writeback",
    "build_alert_resolution",
    "build_transaction_items",
    "execute_approved_action",
    "make_action_executor",
    "lambda_handler",
]
