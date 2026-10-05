"""DynamoDB-backed :class:`EscalationStore` for the Escalation Service.

Loads an alert's escalation snapshot from ``pulse-alerts`` and persists the
escalation state-machine transitions (escalated / exhausted / acknowledged).
All mutations are guarded so they are safe against the races the escalation
timing creates (for example an acknowledgement that lands between a checkpoint
firing and its persistence):

    * ``mark_escalated`` / ``mark_exhausted`` only apply while the alert is still
      open (``UNACKNOWLEDGED`` or ``ESCALATED``), so they never overwrite an
      acknowledgement.
    * ``mark_acknowledged`` only applies while the alert is still open, so it is
      idempotent and never revives a terminal alert.

A conditional-check failure on any of these is the expected "someone else
already moved this alert" outcome and is swallowed; any other error propagates.
Attribute keys are camelCase (NAMING-05); ``status`` is a DynamoDB reserved
word so every update aliases it via an expression name. Table names come from
configuration (PYQUALITY-06).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Optional

from boto3.dynamodb.conditions import Attr

from pulse.common.config import load_config
from pulse.common.dynamo import get_table
from pulse.common.logging import get_logger
from pulse.common.models import AlertStatus
from pulse.escalation.service import AlertEscalationState

logger = get_logger("pulse-escalation-service")

# Statuses in which an escalation mutation may still apply. Mirrors the state
# machine's notion of "open" so persistence agrees with the pure logic.
_OPEN_STATUS_VALUES = [AlertStatus.UNACKNOWLEDGED.value, AlertStatus.ESCALATED.value]

# Default escalation timeout in minutes when the item does not carry one
# (Requirement 6.4 default).
_DEFAULT_TIMEOUT_MIN = 5

# ``status`` is reserved in DynamoDB; alias it consistently in every update.
_STATUS_NAME_MAP = {"#s": "status"}


def _utc_now_iso() -> str:
    """Return the current UTC time in ISO 8601 with a ``Z`` suffix.

    Returns:
        The current UTC timestamp string.
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class DynamoEscalationStore:
    """An :class:`EscalationStore` backed by the ``pulse-alerts`` table.

    Attributes:
        table_name: The ``pulse-alerts`` physical table name.
    """

    def __init__(self, table_name: Optional[str] = None) -> None:
        """Initialize the store.

        Args:
            table_name: The ``pulse-alerts`` table name; read from configuration
                (``ALERTS_TABLE_NAME``) on first use when omitted.
        """
        self._table_name = table_name
        self._table: Any = None

    @property
    def table(self) -> Any:
        """Return the cached ``pulse-alerts`` table resource.

        Returns:
            The boto3 DynamoDB ``Table`` bound to the alerts table.
        """
        if self._table is None:
            name = self._table_name or load_config().alerts_table
            self._table = get_table(name)
        return self._table

    def load(self, alert_id: str) -> Optional[AlertEscalationState]:
        """Load an alert's escalation snapshot.

        Args:
            alert_id: The alert to load.

        Returns:
            The :class:`AlertEscalationState`, or ``None`` when the alert does
            not exist.
        """
        response = self.table.get_item(Key={"alertId": alert_id})
        item = response.get("Item")
        if item is None:
            return None
        return AlertEscalationState(
            alert_id=alert_id,
            status=AlertStatus(item["status"]),
            escalation_chain=list(item.get("escalationChain", [])),
            escalation_position=int(item.get("escalationPosition", 0)),
            escalation_timeout_min=int(
                item.get("escalationTimeoutMin", _DEFAULT_TIMEOUT_MIN)
            ),
        )

    def _update_if_open(
        self, alert_id: str, update_expression: str, values: dict[str, Any]
    ) -> bool:
        """Apply a guarded update that only succeeds while the alert is open.

        Args:
            alert_id: The alert to update.
            update_expression: The DynamoDB ``UpdateExpression`` (aliases
                ``status`` as ``#s``).
            values: The expression attribute values.

        Returns:
            ``True`` if the update applied, ``False`` when the guard failed (the
            alert was no longer open).
        """
        try:
            self.table.update_item(
                Key={"alertId": alert_id},
                UpdateExpression=update_expression,
                ConditionExpression=Attr("status").is_in(_OPEN_STATUS_VALUES),
                ExpressionAttributeNames=_STATUS_NAME_MAP,
                ExpressionAttributeValues=values,
            )
            return True
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            logger.info(
                "Escalation update skipped; alert no longer open",
                extra={"alertId": alert_id},
            )
            return False

    def mark_escalated(self, alert_id: str, position: int, next_check_at: str) -> None:
        """Persist an advance to ``ESCALATED`` at ``position``.

        Args:
            alert_id: The alert to escalate.
            position: The new 0-based chain position.
            next_check_at: ISO 8601 timestamp of the next scheduled checkpoint.
        """
        self._update_if_open(
            alert_id,
            (
                "SET #s = :escalated, escalationPosition = :pos, "
                "escalationNextCheckAt = :next, lastStatusChangeAt = :next"
            ),
            {
                ":escalated": AlertStatus.ESCALATED.value,
                ":pos": position,
                ":next": next_check_at,
            },
        )

    def mark_exhausted(self, alert_id: str) -> None:
        """Persist the ``ESCALATION_EXHAUSTED`` terminal state.

        Records that all recipients were notified without acknowledgement
        (Requirement 6.6).

        Args:
            alert_id: The alert whose chain is exhausted.
        """
        self._update_if_open(
            alert_id,
            (
                "SET #s = :exhausted, allRecipientsNotified = :true, "
                "lastStatusChangeAt = :now"
            ),
            {
                ":exhausted": AlertStatus.ESCALATION_EXHAUSTED.value,
                ":true": True,
                ":now": _utc_now_iso(),
            },
        )

    def mark_acknowledged(
        self, alert_id: str, user_id: str, acknowledged_at: str
    ) -> None:
        """Persist the ``ACKNOWLEDGED`` state with the acknowledging user.

        Args:
            alert_id: The acknowledged alert.
            user_id: The acknowledging user identifier.
            acknowledged_at: ISO 8601 UTC acknowledgement timestamp.
        """
        self._update_if_open(
            alert_id,
            (
                "SET #s = :ack, acknowledgedBy = :user, acknowledgedAt = :at, "
                "lastStatusChangeAt = :at"
            ),
            {
                ":ack": AlertStatus.ACKNOWLEDGED.value,
                ":user": user_id,
                ":at": acknowledged_at,
            },
        )


__all__ = ["DynamoEscalationStore"]
