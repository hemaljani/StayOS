"""Property and unit tests for the closed-loop Action Executor (Component 8).

Covers the design correctness properties for the resolving write-back:

    * **Property 26** (Validates Requirements 12.3, 12.6): an approved resolving
      action clears the triggering operational condition and resolves the
      originating alert in one atomic transaction, exactly once.
    * **Property 27** (Validates Requirements 8.4, 9.4, 12.3): re-evaluating the
      write-back never creates a duplicate alert and resolution is idempotent --
      ``resolve_cleared_correlations`` is a no-op against an already-terminal
      alert, and a repeated ``execute_approved_action`` is a no-op via the
      ``status <> RESOLVED`` transaction guard.

Plus a unit test for Requirement 12.6: a cancelled ``TransactWriteItems`` (the
alert already resolved so the guard fails) leaves the alert unchanged, raises
:class:`WriteBackError`, and performs no realtime publish; the
:func:`make_action_executor` adapter surfaces the same failure as
``{"executed": False}`` while still leaving the alert unchanged.

The tests exercise the real default transaction writer against moto (so the
DynamoDB transaction semantics -- atomicity and the conditional guard -- are
genuinely tested); the realtime publisher is injected as a spy so no network
call is made.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pulse.action_executor.executor import (
    execute_approved_action,
    make_action_executor,
)
from pulse.common.errors import WriteBackError
from pulse.common.models import AlertStatus, AlertTier, AlertType
from pulse.common.operational_schema import RESERVATIONS_SK
from pulse.rule_engine.alert_factory import derive_alert_id
from pulse.rule_engine.correlation import walk_risk_dedupe_key
from pulse.rule_engine.loop_guard import resolve_cleared_correlations
from tests.action_executor.conftest import (
    ALERTS_TABLE_NAME,
    DynamoEnv,
    SpyPublisher,
)
from tests.rule_engine.conftest import make_change, make_rule

# Hypothesis settings: share the function-scoped moto fixture across generated
# examples on purpose (each example resets the two items it touches) and drop
# the deadline since moto operations are variable-latency.
FIXTURE_PROPERTY_SETTINGS = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

# Deterministic option the GM "approved" (label drives approval.selectedOption).
_APPROVED_OPTION = {"label": "A", "rank": 1, "title": "Relocate to sister property"}

# Bounded, DynamoDB-key-safe identifier alphabet.
_IDENT = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=90),
    min_size=1,
    max_size=20,
)


def _seed_walk_alert(
    env: DynamoEnv,
    property_id: str,
    arrival_date: str,
    *,
    status: AlertStatus = AlertStatus.UNACKNOWLEDGED,
    resolved_by: str | None = None,
) -> dict[str, Any]:
    """Seed a WALK_RISK alert and return the persisted item.

    Args:
        env: The moto DynamoDB environment.
        property_id: The owning property.
        arrival_date: The arrival date (the walk aggregate's entity key).
        status: The initial alert status.
        resolved_by: The resolving user to stamp when pre-resolving.

    Returns:
        The seeded ``pulse-alerts`` item.
    """
    dedupe_key = walk_risk_dedupe_key(property_id, arrival_date)
    item: dict[str, Any] = {
        "alertId": derive_alert_id(dedupe_key),
        "propertyId": property_id,
        "tier": AlertTier.CRITICAL.value,
        "type": AlertType.WALK_RISK.value,
        "title": "Walk Risk",
        "detail": "confirmed exceeds available",
        "status": status.value,
        "dedupeKey": dedupe_key,
        "sourceEntityRef": {
            "table": "stayos-reservations",
            "propertyId": property_id,
            "entityKey": arrival_date,
            "ruleType": AlertType.WALK_RISK.value,
        },
        "triageBrief": {
            "summary": "s",
            "confidence": 90,
            "options": [_APPROVED_OPTION],
        },
        "approval": {
            "state": "PENDING",
            "selectedOption": None,
            "decidedBy": None,
            "decidedAt": None,
        },
        "createdAt": "2026-08-17T10:00:00Z",
        "lastStatusChangeAt": "2026-08-17T10:00:00Z",
    }
    if resolved_by is not None:
        item["resolvedBy"] = resolved_by
        item["resolvedAt"] = "2026-08-17T09:00:00Z"
    env.alerts.put_item(Item=item)
    return item


def _seed_walk_reservation(
    env: DynamoEnv, property_id: str, arrival_date: str, confirmed: int, available: int
) -> None:
    """Seed the walk-risk arrival aggregate the executor writes back to."""
    env.reservations.put_item(
        Item={
            "propertyId": property_id,
            RESERVATIONS_SK: f"WALK#{arrival_date}",
            "arrivalDate": arrival_date,
            "confirmedReservations": confirmed,
            "availableRooms": available,
        }
    )


# ---------------------------------------------------------------------------
# Property 26: approved action clears the condition and resolves exactly once.
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 26: An approved resolving action
# clears the triggering condition and resolves the originating alert exactly once
@FIXTURE_PROPERTY_SETTINGS
@given(
    property_id=_IDENT,
    arrival_date=st.dates().map(lambda d: d.isoformat()),
    available=st.integers(min_value=0, max_value=500),
    shortfall=st.integers(min_value=1, max_value=50),
    user_id=_IDENT,
)
def test_property_26_clears_condition_and_resolves_once(
    dynamo_env: DynamoEnv,
    property_id: str,
    arrival_date: str,
    available: int,
    shortfall: int,
    user_id: str,
) -> None:
    """An approved action atomically clears the condition and resolves once.

    Validates: Requirements 12.3, 12.6
    """
    confirmed = available + shortfall  # condition holds initially
    alert_item = _seed_walk_alert(dynamo_env, property_id, arrival_date)
    _seed_walk_reservation(dynamo_env, property_id, arrival_date, confirmed, available)
    publisher = SpyPublisher()

    result = execute_approved_action(
        alert_item,
        _APPROVED_OPTION,
        user_id,
        alerts_table_name=ALERTS_TABLE_NAME,
        realtime_publisher=publisher,
    )

    # The transaction committed: the alert is RESOLVED with the resolving user.
    assert result.executed is True
    assert result.new_status is AlertStatus.RESOLVED
    stored = dynamo_env.alerts.get_item(Key={"alertId": alert_item["alertId"]})["Item"]
    assert stored["status"] == AlertStatus.RESOLVED.value
    assert stored["resolvedBy"] == user_id
    assert stored["resolvedAt"] == result.resolved_at
    assert stored["approval"]["state"] == "APPROVED"

    # The operational condition cleared: confirmed no longer exceeds available.
    reservation = dynamo_env.reservations.get_item(
        Key={"propertyId": property_id, RESERVATIONS_SK: f"WALK#{arrival_date}"}
    )["Item"]
    assert int(reservation["confirmedReservations"]) <= int(
        reservation["availableRooms"]
    )

    # Post-commit realtime publish happened exactly once (best-effort).
    assert len(publisher.calls) == 1

    # Exactly once: a second execution is a no-op (guard status <> RESOLVED).
    with pytest.raises(WriteBackError):
        execute_approved_action(
            alert_item,
            _APPROVED_OPTION,
            user_id,
            alerts_table_name=ALERTS_TABLE_NAME,
            realtime_publisher=publisher,
        )
    reresolved = dynamo_env.alerts.get_item(
        Key={"alertId": alert_item["alertId"]}
    )["Item"]
    # Unchanged by the second attempt: same resolving user and timestamp.
    assert reresolved["resolvedBy"] == user_id
    assert reresolved["resolvedAt"] == result.resolved_at
    # No additional realtime publish occurred on the failed second attempt.
    assert len(publisher.calls) == 1


# ---------------------------------------------------------------------------
# Property 27: write-back re-evaluation never duplicates; resolution idempotent.
# ---------------------------------------------------------------------------


# Feature: initial-pulse-project, Property 27: Write-back re-evaluation never
# creates a duplicate alert and resolution is idempotent
@FIXTURE_PROPERTY_SETTINGS
@given(
    property_id=_IDENT,
    arrival_date=st.dates().map(lambda d: d.isoformat()),
    available=st.integers(min_value=0, max_value=500),
    shortfall=st.integers(min_value=1, max_value=50),
    user_id=_IDENT,
)
def test_property_27_no_duplicate_and_idempotent_resolution(
    dynamo_env: DynamoEnv,
    property_id: str,
    arrival_date: str,
    available: int,
    shortfall: int,
    user_id: str,
) -> None:
    """Re-evaluation creates no duplicate and resolution is idempotent.

    Validates: Requirements 8.4, 9.4, 12.3
    """
    confirmed = available + shortfall
    alert_item = _seed_walk_alert(dynamo_env, property_id, arrival_date)
    _seed_walk_reservation(dynamo_env, property_id, arrival_date, confirmed, available)
    publisher = SpyPublisher()

    # Approve and execute -> the originating alert resolves.
    execute_approved_action(
        alert_item,
        _APPROVED_OPTION,
        user_id,
        alerts_table_name=ALERTS_TABLE_NAME,
        realtime_publisher=publisher,
    )
    alert_id = alert_item["alertId"]

    # Re-evaluate the (now-cleared) write-back via the safety-net loop-guard:
    # the correlated alert is already terminal, so this is a no-op.
    cleared_change = make_change(
        "stayos-reservations",
        {
            "propertyId": property_id,
            RESERVATIONS_SK: f"WALK#{arrival_date}",
            "arrivalDate": arrival_date,
            "confirmedReservations": available,
            "availableRooms": available,
        },
    )
    walk_rule = make_rule(
        AlertType.WALK_RISK, AlertTier.CRITICAL, property_id=property_id
    )
    loop_publisher = SpyPublisher()
    resolved_ids = resolve_cleared_correlations(
        cleared_change,
        [walk_rule],
        dynamo_env.alerts,
        realtime_publisher=loop_publisher,
    )
    # No open correlated alert to resolve -> nothing resolved, nothing published.
    assert resolved_ids == []
    assert loop_publisher.calls == []

    # A repeated execute is a no-op (transaction guard) -> raises, unchanged.
    with pytest.raises(WriteBackError):
        execute_approved_action(
            alert_item,
            _APPROVED_OPTION,
            user_id,
            alerts_table_name=ALERTS_TABLE_NAME,
            realtime_publisher=publisher,
        )

    # Exactly one alert item exists for this alertId and it stays RESOLVED.
    scanned = dynamo_env.alerts.scan()["Items"]
    assert sum(1 for it in scanned if it["alertId"] == alert_id) == 1
    stored = dynamo_env.alerts.get_item(Key={"alertId": alert_id})["Item"]
    assert stored["status"] == AlertStatus.RESOLVED.value
    assert stored["resolvedBy"] == user_id


# ---------------------------------------------------------------------------
# Unit test: Requirement 12.6 - a cancelled transaction leaves the alert as-is.
# ---------------------------------------------------------------------------


def test_requirement_12_6_cancelled_transaction_leaves_alert_unchanged(
    dynamo_env: DynamoEnv,
) -> None:
    """A cancelled write-back raises WriteBackError; alert unchanged, no publish.

    The alert is already RESOLVED, so the ``status <> RESOLVED`` transaction
    guard fails and the whole ``TransactWriteItems`` is cancelled: the
    operational write does not commit, the alert is untouched, and no realtime
    event is published.

    Validates: Requirement 12.6
    """
    property_id = "ALOHA-CHI-001"
    arrival_date = "2026-08-17"
    original_resolver = "prior-gm"
    alert_item = _seed_walk_alert(
        dynamo_env,
        property_id,
        arrival_date,
        status=AlertStatus.RESOLVED,
        resolved_by=original_resolver,
    )
    _seed_walk_reservation(dynamo_env, property_id, arrival_date, 374, 368)
    publisher = SpyPublisher()

    with pytest.raises(WriteBackError):
        execute_approved_action(
            alert_item,
            _APPROVED_OPTION,
            "new-gm",
            alerts_table_name=ALERTS_TABLE_NAME,
            realtime_publisher=publisher,
        )

    # The alert is unchanged: still resolved by the original resolver.
    stored = dynamo_env.alerts.get_item(
        Key={"alertId": alert_item["alertId"]}
    )["Item"]
    assert stored["status"] == AlertStatus.RESOLVED.value
    assert stored["resolvedBy"] == original_resolver
    assert stored["approval"]["state"] == "PENDING"

    # The operational write did not commit (still oversold, condition unchanged).
    reservation = dynamo_env.reservations.get_item(
        Key={"propertyId": property_id, RESERVATIONS_SK: f"WALK#{arrival_date}"}
    )["Item"]
    assert int(reservation["confirmedReservations"]) == 374
    assert "walkRelocatedBy" not in reservation

    # No realtime publish on a failed transaction.
    assert publisher.calls == []


def test_requirement_12_6_make_action_executor_surfaces_failure(
    dynamo_env: DynamoEnv,
) -> None:
    """The executor adapter reports a failed write-back without changing state.

    The :func:`make_action_executor` adapter catches :class:`WriteBackError`
    and returns ``{"executed": False, "reason": "write-back-failed"}`` so the
    recorded approval stands while the alert is left unchanged (Requirement
    12.6).
    """
    property_id = "ALOHA-CHI-001"
    arrival_date = "2026-08-17"
    alert_item = _seed_walk_alert(
        dynamo_env,
        property_id,
        arrival_date,
        status=AlertStatus.RESOLVED,
        resolved_by="prior-gm",
    )
    _seed_walk_reservation(dynamo_env, property_id, arrival_date, 374, 368)
    publisher = SpyPublisher()

    executor = make_action_executor(
        ALERTS_TABLE_NAME, realtime_publisher=publisher
    )
    summary = executor(alert_item, "A", "new-gm")

    assert summary["executed"] is False
    assert summary["reason"] == "write-back-failed"
    # Unchanged and no publish.
    stored = dynamo_env.alerts.get_item(
        Key={"alertId": alert_item["alertId"]}
    )["Item"]
    assert stored["resolvedBy"] == "prior-gm"
    assert publisher.calls == []
