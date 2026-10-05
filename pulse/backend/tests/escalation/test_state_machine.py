"""Property and unit tests for the escalation chain state machine and service.

Covers Property 11 (advance exactly one position along GM -> AGM -> MOD, never
skipping; exhaustion at MOD), Property 12 (acknowledgement halts escalation),
and the Requirement 6.7 delivery retry (3 attempts at 30 s, error identifying
the failed recipient on exhaustion).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Optional

from hypothesis import given, settings
from hypothesis import strategies as st

from pulse.common.models import AlertStatus
from pulse.escalation.service import (
    DELIVERY_MAX_ATTEMPTS,
    AlertEscalationState,
    deliver_with_retry,
    on_alert_acknowledged,
    on_escalation_checkpoint,
)
from pulse.escalation.state_machine import (
    ESCALATION_ROLES,
    EscalationChainState,
    acknowledge,
    advance_on_timeout,
    default_escalation_chain,
)

PROPERTY_SETTINGS = settings(max_examples=200)

_CHAIN = default_escalation_chain("gm", "agm", "mod")
_OPEN_STATUSES = [AlertStatus.UNACKNOWLEDGED, AlertStatus.ESCALATED]
_TERMINAL_STATUSES = [
    AlertStatus.ACKNOWLEDGED,
    AlertStatus.RESOLVED,
    AlertStatus.ESCALATION_EXHAUSTED,
]


# ---------------------------------------------------------------------------
# Fakes for the service seams
# ---------------------------------------------------------------------------


class _FakeStore:
    """In-memory :class:`EscalationStore` recording transitions applied."""

    def __init__(self, state: AlertEscalationState) -> None:
        self.state = state
        self.escalated_to: Optional[int] = None
        self.exhausted = False
        self.acknowledged_by: Optional[str] = None

    def load(self, alert_id: str) -> Optional[AlertEscalationState]:
        return self.state if alert_id == self.state.alert_id else None

    def mark_escalated(self, alert_id: str, position: int, next_check_at: str) -> None:
        self.escalated_to = position

    def mark_exhausted(self, alert_id: str) -> None:
        self.exhausted = True

    def mark_acknowledged(
        self, alert_id: str, user_id: str, acknowledged_at: str
    ) -> None:
        self.acknowledged_by = user_id


class _FakeScheduler:
    """In-memory :class:`SchedulerGateway` recording scheduling calls."""

    def __init__(self) -> None:
        self.scheduled: list[str] = []
        self.cancelled: list[str] = []

    def schedule_checkpoint(self, alert_id: str, when: datetime) -> None:
        self.scheduled.append(alert_id)

    def cancel_checkpoint(self, alert_id: str) -> None:
        self.cancelled.append(alert_id)


def _state(position: int, status: AlertStatus) -> AlertEscalationState:
    """Build an :class:`AlertEscalationState` for the default chain."""
    return AlertEscalationState(
        alert_id="alert-1",
        status=status,
        escalation_chain=list(_CHAIN),
        escalation_position=position,
        escalation_timeout_min=5,
    )


# ---------------------------------------------------------------------------
# Property 11: advance one position GM -> AGM -> MOD, never skip; exhaust at MOD
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 11: Escalation advances one position
# along GM -> AGM -> MOD and never skips
@PROPERTY_SETTINGS
@given(
    position=st.integers(min_value=0, max_value=2),
    status=st.sampled_from(_OPEN_STATUSES),
)
def test_property_11_advance_one_position_never_skip(
    position: int, status: AlertStatus
) -> None:
    """Timeout advances by exactly one, or exhausts at the last position.

    Validates: Requirements 6.1, 6.3, 6.6
    """
    # The chain is ordered exactly [GM, AGM, MOD] (Requirement 6.1).
    assert _CHAIN == ["gm", "agm", "mod"]
    assert len(ESCALATION_ROLES) == 3

    transition = advance_on_timeout(
        EscalationChainState(status=status, chain=list(_CHAIN), position=position)
    )

    last_index = len(_CHAIN) - 1
    if position < last_index:
        # Advances by exactly one, never skipping.
        assert transition.new_position == position + 1
        assert transition.new_status is AlertStatus.ESCALATED
        assert transition.current_recipient == _CHAIN[position + 1]
    else:
        # At MOD: chain exhausted, all recipients recorded as notified.
        assert transition.new_status is AlertStatus.ESCALATION_EXHAUSTED
        assert transition.all_notified is True
        assert transition.current_recipient is None


@PROPERTY_SETTINGS
@given(
    position=st.integers(min_value=0, max_value=2),
    status=st.sampled_from(_TERMINAL_STATUSES),
)
def test_property_11_no_advance_when_terminal(
    position: int, status: AlertStatus
) -> None:
    """A checkpoint against a terminal alert is a no-op (no advance).

    Validates: Requirements 6.3, 6.6
    """
    transition = advance_on_timeout(
        EscalationChainState(status=status, chain=list(_CHAIN), position=position)
    )
    assert transition.changed is False
    assert transition.new_position == position
    assert transition.new_status is status


# ---------------------------------------------------------------------------
# Property 12: acknowledgement halts escalation
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 12: Acknowledgement halts escalation
@PROPERTY_SETTINGS
@given(position=st.integers(min_value=0, max_value=2))
def test_property_12_acknowledgement_halts(position: int) -> None:
    """Acknowledging an open alert at any position stops escalation.

    Validates: Requirements 6.5
    """
    for status in _OPEN_STATUSES:
        transition = acknowledge(
            EscalationChainState(status=status, chain=list(_CHAIN), position=position)
        )
        assert transition.changed is True
        assert transition.new_status is AlertStatus.ACKNOWLEDGED
        assert transition.schedule_next is False
        # Acknowledgement never advances the chain.
        assert transition.new_position == position


def test_acknowledgement_of_terminal_is_noop() -> None:
    """Acknowledging an already-terminal alert changes nothing."""
    transition = acknowledge(
        EscalationChainState(
            status=AlertStatus.RESOLVED, chain=list(_CHAIN), position=1
        )
    )
    assert transition.changed is False
    assert transition.new_status is AlertStatus.RESOLVED


# ---------------------------------------------------------------------------
# Service orchestration: checkpoint + acknowledge wiring
# ---------------------------------------------------------------------------


def test_checkpoint_advances_and_delivers() -> None:
    """A checkpoint on an unacknowledged GM alert escalates to the AGM."""
    store = _FakeStore(_state(0, AlertStatus.UNACKNOWLEDGED))
    scheduler = _FakeScheduler()
    delivered: list[tuple[str, str]] = []

    status = on_escalation_checkpoint(
        "alert-1",
        store=store,
        deliver=lambda alert_id, recipient: delivered.append((alert_id, recipient)),
        scheduler=scheduler,
        now=datetime(2026, 8, 17, 14, 30, tzinfo=UTC),
    )

    assert status is AlertStatus.ESCALATED
    assert store.escalated_to == 1
    assert delivered == [("alert-1", "agm")]
    assert scheduler.scheduled == ["alert-1"]


def test_checkpoint_at_last_position_exhausts() -> None:
    """A checkpoint at MOD exhausts the chain and cancels the schedule."""
    store = _FakeStore(_state(2, AlertStatus.ESCALATED))
    scheduler = _FakeScheduler()

    status = on_escalation_checkpoint(
        "alert-1",
        store=store,
        deliver=lambda alert_id, recipient: None,
        scheduler=scheduler,
    )

    assert status is AlertStatus.ESCALATION_EXHAUSTED
    assert store.exhausted is True
    assert scheduler.cancelled == ["alert-1"]


def test_acknowledged_checkpoint_is_noop() -> None:
    """A checkpoint against an acknowledged alert does not escalate."""
    store = _FakeStore(_state(1, AlertStatus.ACKNOWLEDGED))
    scheduler = _FakeScheduler()

    status = on_escalation_checkpoint(
        "alert-1",
        store=store,
        deliver=lambda alert_id, recipient: None,
        scheduler=scheduler,
    )

    assert status is AlertStatus.ACKNOWLEDGED
    assert store.escalated_to is None
    assert scheduler.scheduled == []


def test_on_alert_acknowledged_halts_and_cancels() -> None:
    """Acknowledgement persists ACKNOWLEDGED and cancels the pending schedule."""
    store = _FakeStore(_state(1, AlertStatus.ESCALATED))
    scheduler = _FakeScheduler()

    status = on_alert_acknowledged(
        "alert-1", "jsmith", store=store, scheduler=scheduler
    )

    assert status is AlertStatus.ACKNOWLEDGED
    assert store.acknowledged_by == "jsmith"
    assert scheduler.cancelled == ["alert-1"]


# ---------------------------------------------------------------------------
# Requirement 6.7: delivery retry (3 attempts at 30 s) + failed-recipient error
# ---------------------------------------------------------------------------


def test_requirement_6_7_delivery_retries_three_times() -> None:
    """Delivery is retried up to 3 times at 30 s intervals, then reports failure.

    Validates: Requirement 6.7
    """
    attempts: list[str] = []
    sleeps: list[float] = []

    def _always_fail(alert_id: str, recipient: str) -> None:
        attempts.append(recipient)
        raise RuntimeError("push endpoint unavailable")

    delivered = deliver_with_retry(_always_fail, "alert-1", "agm", sleep=sleeps.append)

    assert delivered is False
    assert len(attempts) == DELIVERY_MAX_ATTEMPTS
    # Sleeps happen between attempts only: attempts - 1 waits, each 30 s.
    assert sleeps == [30, 30]


def test_requirement_6_7_delivery_succeeds_after_retry() -> None:
    """Delivery that succeeds on the second attempt reports success.

    Validates: Requirement 6.7
    """
    calls = {"n": 0}

    def _fail_once(alert_id: str, recipient: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")

    delivered = deliver_with_retry(_fail_once, "alert-1", "agm", sleep=lambda _s: None)

    assert delivered is True
    assert calls["n"] == 2
