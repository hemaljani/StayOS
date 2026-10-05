"""Property and unit tests for alert lifecycle transitions and the approval gate.

Covers Property 19 (valid status transitions, RESOLVED terminal/monotonic) and
Property 7 (no CRITICAL ranked-option action executes without a recorded GM
approval), plus orchestration unit tests over a moto-backed ``pulse-alerts``
table for acknowledge/resolve/approve and the already-resolved rejection
(Requirement 12.5).
"""

from __future__ import annotations

from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

from pulse.api import alert_lifecycle as lifecycle
from pulse.common.models import AlertStatus, ApprovalState
from tests.api.conftest import (
    ALERTS_TABLE_NAME,
    create_alerts_table,
    make_alert_item,
    table_getter,
)

PROPERTY_SETTINGS = settings(max_examples=200)

_ALL_STATUSES = list(AlertStatus)
_ACK_TARGETS = {AlertStatus.UNACKNOWLEDGED, AlertStatus.ESCALATED}
_RESOLVE_TARGETS = {
    AlertStatus.UNACKNOWLEDGED,
    AlertStatus.ACKNOWLEDGED,
    AlertStatus.ESCALATED,
}


# ---------------------------------------------------------------------------
# Property 19: valid transitions, RESOLVED terminal (monotonic)
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 19: Alert status transitions are
# valid and RESOLVED is terminal (monotonic)
@PROPERTY_SETTINGS
@given(current=st.sampled_from(_ALL_STATUSES))
def test_property_19_acknowledge_transitions_are_valid(
    current: AlertStatus,
) -> None:
    """Acknowledge only moves open alerts to ACKNOWLEDGED; RESOLVED is terminal.

    Validates: Requirements 12.1, 12.5
    """
    result = lifecycle.plan_acknowledge(current)
    if current is AlertStatus.RESOLVED:
        # RESOLVED is terminal: rejected, status unchanged (monotonic).
        assert result.changed is False
        assert result.rejected is True
        assert result.new_status is AlertStatus.RESOLVED
    elif current in _ACK_TARGETS:
        assert result.changed is True
        assert result.new_status is AlertStatus.ACKNOWLEDGED
    else:
        # No valid acknowledge transition; status left unchanged.
        assert result.changed is False
        assert result.new_status is current


# Feature: initial-pulse-project, Property 19: Alert status transitions are
# valid and RESOLVED is terminal (monotonic)
@PROPERTY_SETTINGS
@given(current=st.sampled_from(_ALL_STATUSES))
def test_property_19_resolve_transitions_are_valid(current: AlertStatus) -> None:
    """Resolve moves open alerts to RESOLVED; a resolved alert cannot change.

    Validates: Requirements 12.2, 12.3, 12.5
    """
    result = lifecycle.plan_resolve(current)
    if current is AlertStatus.RESOLVED:
        assert result.changed is False
        assert result.rejected is True
        assert result.new_status is AlertStatus.RESOLVED
    elif current in _RESOLVE_TARGETS:
        assert result.changed is True
        assert result.new_status is AlertStatus.RESOLVED
    else:
        assert result.changed is False
        assert result.new_status is current


def test_property_19_resolved_is_monotonic() -> None:
    """Neither acknowledge nor resolve ever leaves the RESOLVED state.

    Validates: Requirements 12.5
    """
    assert lifecycle.plan_acknowledge(AlertStatus.RESOLVED).new_status is (
        AlertStatus.RESOLVED
    )
    assert lifecycle.plan_resolve(AlertStatus.RESOLVED).new_status is (
        AlertStatus.RESOLVED
    )


# ---------------------------------------------------------------------------
# Property 7: no action executes without a recorded GM approval
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 7: No CRITICAL ranked-option action
# executes without a recorded GM approval
@PROPERTY_SETTINGS
@given(
    state=st.sampled_from(list(ApprovalState)),
    decision=st.sampled_from(["approve", "reject", "APPROVE", "noop", ""]),
)
def test_property_7_execution_requires_recorded_approval(
    state: ApprovalState, decision: str
) -> None:
    """``should_execute`` holds only for an accepted approval from PENDING.

    Validates: Requirements 3.7, 10.3, 10.7
    """
    plan = lifecycle.plan_approval_decision(state, decision)
    if plan.should_execute:
        # Execution is authorized only by a fresh, accepted GM approval.
        assert plan.accepted is True
        assert plan.new_state is ApprovalState.APPROVED
        assert state is ApprovalState.PENDING
        assert decision.strip().lower() == "approve"
    # A non-PENDING approval can never authorize execution.
    if state is not ApprovalState.PENDING:
        assert plan.should_execute is False
    # A reject decision never authorizes execution and never approves.
    if decision.strip().lower() == "reject":
        assert plan.should_execute is False
        if state is ApprovalState.PENDING:
            assert plan.new_state is ApprovalState.REJECTED


def test_property_7_reject_leaves_alert_unexecuted() -> None:
    """Rejecting all options records REJECTED and never calls the executor.

    Validates: Requirements 10.7
    """
    executed: list[str] = []

    with mock_aws():
        table = create_alerts_table()
        item = make_alert_item(
            "alert-1",
            "ALOHA-CHI-001",
            options=[{"label": "A", "rank": 1, "recommended": True}],
        )
        table.put_item(Item=item)

        result = lifecycle.decide_approval(
            "alert-1",
            "jsmith",
            "reject",
            "A",
            item=item,
            alerts_table_name=ALERTS_TABLE_NAME,
            table_getter=table_getter,
            action_executor=lambda *_a: executed.append("x") or {"executed": True},
        )

    assert result["accepted"] is True
    assert result["approvalState"] == ApprovalState.REJECTED.value
    assert result["executed"] is False
    assert executed == []


def test_property_7_approve_invokes_executor_once() -> None:
    """A recorded approval invokes the Action Executor exactly once.

    Validates: Requirements 3.7, 10.3
    """
    calls: list[tuple[str, str]] = []

    def _executor(alert_item: Any, option: str, user: str) -> dict[str, Any]:
        calls.append((option, user))
        return {"executed": True}

    with mock_aws():
        table = create_alerts_table()
        item = make_alert_item(
            "alert-1",
            "ALOHA-CHI-001",
            options=[{"label": "A", "rank": 1, "recommended": True}],
        )
        table.put_item(Item=item)

        result = lifecycle.decide_approval(
            "alert-1",
            "jsmith",
            "approve",
            "A",
            item=item,
            alerts_table_name=ALERTS_TABLE_NAME,
            table_getter=table_getter,
            action_executor=_executor,
        )

    assert result["accepted"] is True
    assert result["approvalState"] == ApprovalState.APPROVED.value
    assert result["executed"] is True
    assert calls == [("A", "jsmith")]


def test_approve_with_invalid_option_does_not_execute() -> None:
    """Approving an option not on the brief is rejected without executing.

    Validates: Requirements 10.3
    """
    executed: list[str] = []
    with mock_aws():
        table = create_alerts_table()
        item = make_alert_item(
            "alert-1",
            "ALOHA-CHI-001",
            options=[{"label": "A", "rank": 1, "recommended": True}],
        )
        table.put_item(Item=item)

        result = lifecycle.decide_approval(
            "alert-1",
            "jsmith",
            "approve",
            "Z",
            item=item,
            alerts_table_name=ALERTS_TABLE_NAME,
            table_getter=table_getter,
            action_executor=lambda *_a: executed.append("x") or {"executed": True},
        )

    assert result["accepted"] is False
    assert result["reason"] == "invalid-option"
    assert executed == []


# ---------------------------------------------------------------------------
# Orchestration over a moto-backed table
# ---------------------------------------------------------------------------


class _RecordingPublisher:
    """Records realtime publish calls (channel, events)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    def __call__(self, channel: str, events: Any) -> None:
        self.calls.append((channel, list(events)))


def test_acknowledge_persists_and_publishes() -> None:
    """Acknowledge sets ACKNOWLEDGED + user/timestamp and publishes ALERT_UPDATED.

    Validates: Requirements 12.1
    """
    publisher = _RecordingPublisher()
    with mock_aws():
        table = create_alerts_table()
        item = make_alert_item("alert-1", "ALOHA-CHI-001")
        table.put_item(Item=item)

        result = lifecycle.acknowledge_alert(
            "alert-1",
            "jsmith",
            item=item,
            alerts_table_name=ALERTS_TABLE_NAME,
            table_getter=table_getter,
            realtime_publisher=publisher,
            now="2026-08-17T14:33:10Z",
        )
        stored = table.get_item(Key={"alertId": "alert-1"})["Item"]

    assert result.changed is True
    assert result.new_status is AlertStatus.ACKNOWLEDGED
    assert stored["status"] == "ACKNOWLEDGED"
    assert stored["acknowledgedBy"] == "jsmith"
    assert stored["acknowledgedAt"] == "2026-08-17T14:33:10Z"
    # A single ALERT_UPDATED broadcast to the property channel.
    assert publisher.calls[0][0] == "/pulse/alerts/ALOHA-CHI-001"
    assert publisher.calls[0][1][0]["eventType"] == "ALERT_UPDATED"


def test_resolve_then_reresolve_is_rejected_unchanged() -> None:
    """Resolving succeeds once; a second resolve is rejected, state unchanged.

    Validates: Requirements 12.2, 12.5
    """
    with mock_aws():
        table = create_alerts_table()
        item = make_alert_item("alert-1", "ALOHA-CHI-001", status="ACKNOWLEDGED")
        table.put_item(Item=item)

        first = lifecycle.resolve_alert(
            "alert-1",
            "jsmith",
            item=item,
            alerts_table_name=ALERTS_TABLE_NAME,
            table_getter=table_getter,
            now="2026-08-17T14:40:00Z",
        )
        resolved_item = table.get_item(Key={"alertId": "alert-1"})["Item"]
        second = lifecycle.resolve_alert(
            "alert-1",
            "other",
            item=resolved_item,
            alerts_table_name=ALERTS_TABLE_NAME,
            table_getter=table_getter,
            now="2026-08-17T15:00:00Z",
        )
        final_item = table.get_item(Key={"alertId": "alert-1"})["Item"]

    assert first.changed is True
    assert first.new_status is AlertStatus.RESOLVED
    assert second.changed is False
    assert second.rejected is True
    # Timestamps and resolver are unchanged by the rejected second attempt.
    assert final_item["resolvedBy"] == "jsmith"
    assert final_item["resolvedAt"] == "2026-08-17T14:40:00Z"
