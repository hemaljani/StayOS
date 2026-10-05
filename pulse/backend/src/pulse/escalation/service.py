"""Escalation Service (``pulse-escalation-service``): orchestration + seams.

This module wires the pure escalation state machine
(:mod:`pulse.escalation.state_machine`) to its side effects: loading and
persisting alert escalation state, delivering an escalated alert to the current
recipient with bounded retries, and scheduling the next timeout checkpoint via
EventBridge Scheduler. Following PYQUALITY-05 the :func:`lambda_handler` is a
thin dispatcher that delegates every decision to pure functions and injectable
seams, so the orchestration is unit-testable without a live Lambda, DynamoDB, or
Scheduler.

Seams (all injectable for tests):
    * ``EscalationStore`` -- loads an alert's escalation snapshot and persists
      the escalated / exhausted / acknowledged transitions.
    * ``DeliverFn`` -- delivers an alert to a recipient; raises on failure so
      :func:`deliver_with_retry` can apply the retry policy (Requirement 6.7).
    * ``SchedulerGateway`` -- creates a one-shot checkpoint schedule and cancels
      a pending one (design Decision 2: EventBridge Scheduler one-shot per
      checkpoint).

Timing:
    * The next checkpoint is scheduled at ``now + escalationTimeoutMin`` after an
      advance (Requirement 6.3 / 6.4).
    * A separate low-frequency safety-net sweep (:func:`sweep_due_escalations`)
      catches any missed one-shot by re-firing checkpoints for alerts whose next
      check time is due; the sweep is invoked at an interval no greater than 30
      seconds (Requirement 6.2).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Optional, Protocol

from pulse.common.config import get_optional_env
from pulse.common.logging import get_logger
from pulse.common.models import AlertStatus
from pulse.common.tracing import get_tracer
from pulse.escalation.state_machine import (
    EscalationChainState,
    acknowledge,
    advance_on_timeout,
)

logger = get_logger("pulse-escalation-service")
tracer = get_tracer("pulse-escalation-service")

# Retry policy for delivering an escalated alert to the next recipient
# (Requirement 6.7): up to 3 attempts at 30-second intervals.
DELIVERY_MAX_ATTEMPTS = 3
DELIVERY_RETRY_INTERVAL_SEC = 30

# Environment variables for the EventBridge Scheduler one-shot target (never
# hardcoded, PYQUALITY-06 / NAMING-03).
ENV_SCHEDULER_GROUP = "ESCALATION_SCHEDULE_GROUP"
ENV_SCHEDULER_TARGET_ARN = "ESCALATION_TARGET_ARN"
ENV_SCHEDULER_ROLE_ARN = "ESCALATION_SCHEDULER_ROLE_ARN"

# A delivery callable takes (alertId, recipientAlias) and raises on failure.
DeliverFn = Callable[[str, str], None]


@dataclass(frozen=True)
class AlertEscalationState:
    """The escalation snapshot the service loads for one alert.

    Attributes:
        alert_id: The alert identifier.
        status: The current alert status.
        escalation_chain: The ordered recipient aliases ``[GM, AGM, MOD]``.
        escalation_position: 0-based index of the current recipient.
        escalation_timeout_min: The effective timeout in minutes used to
            schedule the next checkpoint.
    """

    alert_id: str
    status: AlertStatus
    escalation_chain: list[str]
    escalation_position: int
    escalation_timeout_min: int


class EscalationStore(Protocol):
    """Persistence seam for escalation state (implemented over DynamoDB)."""

    def load(self, alert_id: str) -> Optional[AlertEscalationState]:
        """Return the alert's escalation snapshot, or ``None`` if not found."""
        ...

    def mark_escalated(self, alert_id: str, position: int, next_check_at: str) -> None:
        """Persist an advance to ``ESCALATED`` at ``position``."""
        ...

    def mark_exhausted(self, alert_id: str) -> None:
        """Persist the ``ESCALATION_EXHAUSTED`` terminal state."""
        ...

    def mark_acknowledged(
        self, alert_id: str, user_id: str, acknowledged_at: str
    ) -> None:
        """Persist the ``ACKNOWLEDGED`` state with the acknowledging user."""
        ...


class SchedulerGateway(Protocol):
    """Seam over EventBridge Scheduler one-shot checkpoint schedules."""

    def schedule_checkpoint(self, alert_id: str, when: datetime) -> None:
        """Create/replace a one-shot schedule firing a checkpoint at ``when``."""
        ...

    def cancel_checkpoint(self, alert_id: str) -> None:
        """Cancel a pending checkpoint schedule for ``alert_id`` if present."""
        ...


def _utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC ``datetime``.

    Returns:
        The current UTC time.
    """
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    """Format a ``datetime`` as an ISO 8601 string with a ``Z`` suffix.

    Args:
        moment: The moment to format.

    Returns:
        The ISO 8601 string, e.g. ``"2026-08-17T14:35:00Z"``.
    """
    return (
        moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )


def deliver_with_retry(
    deliver: DeliverFn,
    alert_id: str,
    recipient: str,
    *,
    max_attempts: int = DELIVERY_MAX_ATTEMPTS,
    interval_sec: int = DELIVERY_RETRY_INTERVAL_SEC,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Deliver an escalated alert to a recipient with bounded retries.

    Retries delivery up to ``max_attempts`` times at ``interval_sec`` intervals.
    Each failure is logged with the alert id; when all attempts are exhausted an
    error is logged identifying the failed recipient and ``False`` is returned so
    the caller retains the ``ESCALATED`` status (Requirement 6.7).

    Args:
        deliver: The delivery callable; raises on failure.
        alert_id: The alert being delivered.
        recipient: The recipient alias to deliver to.
        max_attempts: Maximum delivery attempts (default 3).
        interval_sec: Seconds to wait between attempts (default 30).
        sleep: Sleep function, injectable for tests.

    Returns:
        ``True`` if delivery succeeded on some attempt, ``False`` if all attempts
        were exhausted.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            deliver(alert_id, recipient)
            return True
        except Exception as exc:  # noqa: BLE001 - retry policy needs any failure
            logger.error(
                "Escalation delivery attempt failed",
                extra={
                    "alertId": alert_id,
                    "recipient": recipient,
                    "attempt": attempt,
                    "maxAttempts": max_attempts,
                    "error": str(exc),
                },
            )
            if attempt < max_attempts:
                sleep(interval_sec)
    # Requirement 6.7: all retries exhausted -> error identifying the recipient.
    logger.error(
        "Escalation delivery exhausted; recipient could not be notified",
        extra={"alertId": alert_id, "failedRecipient": recipient},
    )
    return False


def on_escalation_checkpoint(
    alert_id: str,
    *,
    store: EscalationStore,
    deliver: DeliverFn,
    scheduler: SchedulerGateway,
    now: Optional[datetime] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> AlertStatus:
    """Handle a fired escalation checkpoint for one alert.

    Loads the alert's escalation snapshot and applies
    :func:`advance_on_timeout`. On an advance to a non-last recipient it delivers
    to the new recipient (with retry), persists the ``ESCALATED`` state, and
    schedules the next checkpoint. On exhaustion it persists
    ``ESCALATION_EXHAUSTED`` and cancels any pending schedule. A checkpoint
    against a terminal/acknowledged alert is a no-op.

    Args:
        alert_id: The alert whose checkpoint fired.
        store: Escalation persistence seam.
        deliver: Delivery seam.
        scheduler: Scheduler seam.
        now: The current time; defaults to UTC now (injectable for tests).
        sleep: Sleep function passed to the delivery retry (injectable).

    Returns:
        The alert's resulting status (unchanged for a no-op).
    """
    state = store.load(alert_id)
    if state is None:
        logger.warning(
            "Checkpoint for unknown alert; ignoring", extra={"alertId": alert_id}
        )
        return AlertStatus.UNACKNOWLEDGED

    transition = advance_on_timeout(
        EscalationChainState(
            status=state.status,
            chain=state.escalation_chain,
            position=state.escalation_position,
        )
    )
    if not transition.changed:
        logger.info(
            "Checkpoint no-op; alert no longer open for escalation",
            extra={"alertId": alert_id, "status": state.status.value},
        )
        return state.status

    moment = now or _utc_now()

    if transition.new_status is AlertStatus.ESCALATION_EXHAUSTED:
        store.mark_exhausted(alert_id)
        scheduler.cancel_checkpoint(alert_id)
        logger.info(
            "Escalation exhausted; all recipients notified without acknowledgement",
            extra={"alertId": alert_id, "chain": state.escalation_chain},
        )
        return AlertStatus.ESCALATION_EXHAUSTED

    # Advance to the next recipient (Requirement 6.3).
    recipient = transition.current_recipient or ""
    next_check_at = _iso(moment + timedelta(minutes=state.escalation_timeout_min))
    delivered = deliver_with_retry(deliver, alert_id, recipient, sleep=sleep)
    store.mark_escalated(alert_id, transition.new_position, next_check_at)
    # Schedule the next checkpoint regardless of this delivery's outcome: the
    # alert is now ESCALATED and must continue to be chased until acknowledged
    # or the chain is exhausted.
    scheduler.schedule_checkpoint(
        alert_id, moment + timedelta(minutes=state.escalation_timeout_min)
    )
    logger.info(
        "Alert escalated to next recipient",
        extra={
            "alertId": alert_id,
            "position": transition.new_position,
            "recipient": recipient,
            "delivered": delivered,
            "nextCheckAt": next_check_at,
        },
    )
    return AlertStatus.ESCALATED


def on_alert_acknowledged(
    alert_id: str,
    user_id: str,
    *,
    store: EscalationStore,
    scheduler: SchedulerGateway,
    now: Optional[datetime] = None,
) -> AlertStatus:
    """Handle an acknowledgement: halt escalation and set ``ACKNOWLEDGED``.

    Applies :func:`acknowledge`; on a real transition it persists the
    ``ACKNOWLEDGED`` state with the acknowledging user and cancels any pending
    checkpoint schedule (Requirement 6.5, Property 12). An acknowledgement of an
    already-terminal alert is a no-op.

    Args:
        alert_id: The acknowledged alert.
        user_id: The acknowledging user identifier.
        store: Escalation persistence seam.
        scheduler: Scheduler seam.
        now: The current time; defaults to UTC now (injectable for tests).

    Returns:
        The alert's resulting status.
    """
    state = store.load(alert_id)
    if state is None:
        logger.warning(
            "Acknowledgement for unknown alert; ignoring", extra={"alertId": alert_id}
        )
        return AlertStatus.UNACKNOWLEDGED

    transition = acknowledge(
        EscalationChainState(
            status=state.status,
            chain=state.escalation_chain,
            position=state.escalation_position,
        )
    )
    if not transition.changed:
        logger.info(
            "Acknowledgement no-op; alert already terminal",
            extra={"alertId": alert_id, "status": state.status.value},
        )
        return state.status

    store.mark_acknowledged(alert_id, user_id, _iso(now or _utc_now()))
    scheduler.cancel_checkpoint(alert_id)
    logger.info(
        "Alert acknowledged; escalation halted",
        extra={"alertId": alert_id, "acknowledgedBy": user_id},
    )
    return AlertStatus.ACKNOWLEDGED


def sweep_due_escalations(
    due_alert_ids: list[str],
    *,
    store: EscalationStore,
    deliver: DeliverFn,
    scheduler: SchedulerGateway,
    now: Optional[datetime] = None,
) -> int:
    """Safety-net sweep: re-fire checkpoints for alerts whose next check is due.

    Catches any one-shot schedule that failed to fire. The caller supplies the
    due alert ids (queried from the ``escalationStatus-escalationNextCheckAt``
    index); this runs at an interval no greater than 30 seconds (Requirement
    6.2).

    Args:
        due_alert_ids: Alert ids whose next check time is at or before now.
        store: Escalation persistence seam.
        deliver: Delivery seam.
        scheduler: Scheduler seam.
        now: The current time; defaults to UTC now.

    Returns:
        The number of alerts whose checkpoint was processed.
    """
    processed = 0
    for alert_id in due_alert_ids:
        on_escalation_checkpoint(
            alert_id, store=store, deliver=deliver, scheduler=scheduler, now=now
        )
        processed += 1
    return processed


# ---------------------------------------------------------------------------
# Default EventBridge Scheduler gateway (one-shot checkpoints, Decision 2)
# ---------------------------------------------------------------------------


class EventBridgeSchedulerGateway:
    """Default :class:`SchedulerGateway` backed by EventBridge Scheduler.

    Creates a one-shot ``at(...)`` schedule per checkpoint targeting the
    escalation Lambda, and deletes it on acknowledgement/exhaustion. The
    scheduler client is created lazily via the shared cached factory so importing
    this module never requires AWS configuration (tests inject a fake gateway).

    Attributes:
        group_name: The scheduler group the one-shot schedules live in.
        target_arn: The escalation Lambda ARN the schedule invokes.
        role_arn: The IAM role ARN the scheduler assumes to invoke the target.
    """

    def __init__(
        self,
        group_name: Optional[str] = None,
        target_arn: Optional[str] = None,
        role_arn: Optional[str] = None,
    ) -> None:
        """Initialize the gateway from explicit values or the environment.

        Args:
            group_name: Scheduler group name; falls back to
                ``ESCALATION_SCHEDULE_GROUP``.
            target_arn: Target Lambda ARN; falls back to
                ``ESCALATION_TARGET_ARN``.
            role_arn: Scheduler role ARN; falls back to
                ``ESCALATION_SCHEDULER_ROLE_ARN``.
        """
        self.group_name = group_name or get_optional_env(ENV_SCHEDULER_GROUP, "default")
        self.target_arn = target_arn or get_optional_env(ENV_SCHEDULER_TARGET_ARN)
        self.role_arn = role_arn or get_optional_env(ENV_SCHEDULER_ROLE_ARN)

    @staticmethod
    def _schedule_name(alert_id: str) -> str:
        """Return the deterministic schedule name for an alert's checkpoint.

        Args:
            alert_id: The alert id.

        Returns:
            The schedule name (``pulse-esc-<alertId>``).
        """
        return f"pulse-esc-{alert_id}"

    def _client(self) -> Any:
        """Return the cached EventBridge Scheduler client.

        Imported lazily so module import never constructs an AWS client.

        Returns:
            The boto3 ``scheduler`` client.
        """
        from pulse.common.aws import get_client

        return get_client("scheduler")

    def schedule_checkpoint(self, alert_id: str, when: datetime) -> None:
        """Create/replace a one-shot checkpoint schedule for an alert.

        Args:
            alert_id: The alert to schedule a checkpoint for.
            when: The UTC time the checkpoint should fire.
        """
        import json

        client = self._client()
        name = self._schedule_name(alert_id)
        # EventBridge Scheduler at() expressions use a non-offset ISO timestamp.
        expression = f"at({when.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S')})"
        params: dict[str, Any] = {
            "Name": name,
            "GroupName": self.group_name,
            "ScheduleExpression": expression,
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "Target": {
                "Arn": self.target_arn,
                "RoleArn": self.role_arn,
                "Input": json.dumps({"action": "checkpoint", "alertId": alert_id}),
            },
            # One-shot: let the schedule delete itself after firing.
            "ActionAfterCompletion": "DELETE",
        }
        try:
            client.create_schedule(**params)
        except client.exceptions.ConflictException:
            # A schedule already exists for this alert; replace it so the next
            # checkpoint reflects the latest timeout.
            client.update_schedule(**params)

    def cancel_checkpoint(self, alert_id: str) -> None:
        """Delete a pending checkpoint schedule for an alert if it exists.

        Args:
            alert_id: The alert whose pending checkpoint to cancel.
        """
        client = self._client()
        try:
            client.delete_schedule(
                Name=self._schedule_name(alert_id), GroupName=self.group_name
            )
        except client.exceptions.ResourceNotFoundException:
            # Nothing pending (already fired/deleted): acknowledgement is still
            # successful, so this is an expected, benign outcome.
            logger.info(
                "No pending escalation schedule to cancel",
                extra={"alertId": alert_id},
            )


# ---------------------------------------------------------------------------
# Thin Lambda entry point
# ---------------------------------------------------------------------------


@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Escalation Service Lambda handler (thin dispatcher).

    Dispatches on the event's ``action`` to the appropriate pure-logic
    orchestration. Supported actions:
        * ``checkpoint`` -- a fired one-shot schedule; requires ``alertId``.
        * ``acknowledge`` -- an acknowledgement; requires ``alertId`` and
          ``userId``.

    The default DynamoDB store and EventBridge Scheduler gateway are constructed
    here; the delivery seam is provided by the delivery layer (wired in a later
    task) and defaults to a no-op that records the intent so escalation state
    still advances during that interim.

    Args:
        event: The invocation event carrying ``action`` and identifiers.
        context: The Lambda context (unused; present for the handler contract).

    Returns:
        A summary dict with the action handled and the resulting status.
    """
    from pulse.escalation.store import DynamoEscalationStore

    action = event.get("action")
    store: EscalationStore = DynamoEscalationStore()
    scheduler: SchedulerGateway = EventBridgeSchedulerGateway()

    def _deliver(alert_id: str, recipient: str) -> None:
        """Interim delivery seam until the delivery layer is wired in.

        Records the delivery intent; the real Web Push / realtime delivery is
        injected by the delivery layer in a later task.
        """
        logger.info(
            "Escalation delivery requested (delivery layer not yet wired)",
            extra={"alertId": alert_id, "recipient": recipient},
        )

    if action == "checkpoint":
        alert_id = event["alertId"]
        status = on_escalation_checkpoint(
            alert_id, store=store, deliver=_deliver, scheduler=scheduler
        )
        return {"action": action, "alertId": alert_id, "status": status.value}

    if action == "acknowledge":
        alert_id = event["alertId"]
        user_id = event["userId"]
        status = on_alert_acknowledged(
            alert_id, user_id, store=store, scheduler=scheduler
        )
        return {"action": action, "alertId": alert_id, "status": status.value}

    logger.warning("Unknown escalation action; ignoring", extra={"action": action})
    return {"action": action, "handled": False}


__all__ = [
    "DELIVERY_MAX_ATTEMPTS",
    "DELIVERY_RETRY_INTERVAL_SEC",
    "DeliverFn",
    "AlertEscalationState",
    "EscalationStore",
    "SchedulerGateway",
    "deliver_with_retry",
    "on_escalation_checkpoint",
    "on_alert_acknowledged",
    "sweep_due_escalations",
    "EventBridgeSchedulerGateway",
    "lambda_handler",
]
