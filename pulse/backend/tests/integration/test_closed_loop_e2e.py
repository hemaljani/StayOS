"""End-to-end closed-loop integration test (Task 23.2).

Drives the full PULSE loop in-process against moto DynamoDB with Bedrock /
AppSync Events / Gateway / Web Push mocked:

    demo ``walk-risk`` scenario writes operational data
        -> the Rule Engine evaluates the resulting change and creates a CRITICAL
           Walk Risk alert
        -> the alert is delivered (ALERT_CREATED published via the mocked
           realtime publisher)
        -> a GM approval is recorded
        -> the Action Executor performs the condition-clearing write-back and
           the transactional RESOLVED update
        -> the alert reaches RESOLVED, the operational condition is cleared, and
           ALERT_RESOLVED is published
        -> re-running the Rule Engine on the write-back change creates NO
           duplicate alert and the resolution is idempotent (Decision 6;
           Properties 26, 27).

The real-DynamoDB-Streams + real-Cognito-WebSocket variant needs a live deploy
and is marked skip-with-reason; the in-process equivalent below runs and passes.
"""

from __future__ import annotations

import pytest

from pulse.action_executor.executor import make_action_executor
from pulse.api.alert_lifecycle import decide_approval
from pulse.common.operational_schema import RESERVATIONS_SK
from pulse.delivery import push_service
from pulse.delivery import realtime_publish as rt
from pulse.demo_simulator import simulator
from pulse.rule_engine import handler as rule_handler
from pulse.rule_engine.rule_validation import (
    default_rule_templates,
    default_template_item,
)
from tests.integration.conftest import (
    ALERTS_TABLE_NAME,
    IntegrationEnv,
    SpyPublisher,
)

DEMO_PROPERTY_ID = simulator.DEMO_PROPERTY_ID
ARRIVAL_DATE = "2026-08-17"


def _seed_default_rules(env: IntegrationEnv) -> None:
    """Seed the default enabled rule templates for the demo property."""
    for rule in default_rule_templates(DEMO_PROPERTY_ID):
        env.rules.put_item(Item=default_template_item(rule))


def _walk_stream_event(confirmed: int, available: int) -> dict:
    """Build a raw DynamoDB Streams event for the walk-risk aggregate."""
    return {
        "Records": [
            {
                "eventID": "1",
                "eventName": "MODIFY",
                "eventSourceARN": (
                    "arn:aws:dynamodb:us-east-1:123456789012:table/"
                    "stayos-reservations/stream/2026-08-17T00:00:00.000"
                ),
                "dynamodb": {
                    "NewImage": {
                        "propertyId": {"S": DEMO_PROPERTY_ID},
                        RESERVATIONS_SK: {"S": f"WALK#{ARRIVAL_DATE}"},
                        "arrivalDate": {"S": ARRIVAL_DATE},
                        "confirmedReservations": {"N": str(confirmed)},
                        "availableRooms": {"N": str(available)},
                    }
                },
            }
        ]
    }


def test_walk_risk_closed_loop_resolves_and_never_duplicates(
    integration_env: IntegrationEnv,
) -> None:
    """The full walk-risk loop resolves the alert exactly once with no duplicate.

    Validates: Requirement 12.3; Decision 6; Properties 26, 27 (in-process)
    """
    _seed_default_rules(integration_env)
    spy = SpyPublisher()

    # 1. The demo simulator writes the operational data that raises the trigger
    #    (confirmed 374 > available 368) using the real operational schema.
    scenario = simulator.get_scenario("walk-risk")
    simulator.apply_scenario(scenario, simulator.ACTION_RUN)
    reservation = integration_env.reservations.get_item(
        Key={"propertyId": DEMO_PROPERTY_ID, RESERVATIONS_SK: f"WALK#{ARRIVAL_DATE}"}
    )["Item"]
    assert int(reservation["confirmedReservations"]) == 374
    assert int(reservation["availableRooms"]) == 368

    # 2. The Rule Engine evaluates the resulting change -> one CRITICAL alert.
    created = rule_handler.lambda_handler(_walk_stream_event(374, 368), None)
    assert created["alertsCreated"] == 1
    alerts = integration_env.alerts.scan()["Items"]
    assert len(alerts) == 1
    alert_item = alerts[0]
    alert_id = alert_item["alertId"]
    assert alert_item["type"] == "WALK_RISK"
    assert alert_item["tier"] == "CRITICAL"
    assert alert_item["status"] == "UNACKNOWLEDGED"

    # 3. Delivery publishes ALERT_CREATED (mocked realtime publisher, no Web
    #    Push subscriptions so the mocked sender is never called).
    push_service.deliver_alert(
        alert_item,
        rt.EVENT_ALERT_CREATED,
        subscription_loader=lambda _alias: [],
        web_push_sender=lambda _sub, _payload: None,
        realtime_publisher=spy,
        sleep=lambda _s: None,
    )

    # 4. A GM records an approval; the approval gate invokes the Action Executor.
    executor = make_action_executor(ALERTS_TABLE_NAME, realtime_publisher=spy)
    decision = decide_approval(
        alert_id,
        "jsmith",
        "approve",
        "A",
        item=alert_item,
        alerts_table_name=ALERTS_TABLE_NAME,
        action_executor=executor,
    )
    assert decision["accepted"] is True
    assert decision["executed"] is True

    # 5. The alert reached RESOLVED and the operational condition is cleared.
    resolved = integration_env.alerts.get_item(Key={"alertId": alert_id})["Item"]
    assert resolved["status"] == "RESOLVED"
    assert resolved["resolvedBy"] == "jsmith"
    assert resolved["approval"]["state"] == "APPROVED"
    cleared_reservation = integration_env.reservations.get_item(
        Key={"propertyId": DEMO_PROPERTY_ID, RESERVATIONS_SK: f"WALK#{ARRIVAL_DATE}"}
    )["Item"]
    assert int(cleared_reservation["confirmedReservations"]) <= int(
        cleared_reservation["availableRooms"]
    )
    assert cleared_reservation["walkRelocatedBy"] == "jsmith"

    # 6. Re-running the Rule Engine on the (now-cleared) write-back change
    #    creates no duplicate alert; the safety-net resolution is a no-op
    #    because the correlated alert is already terminal (Properties 26, 27).
    reeval = rule_handler.lambda_handler(_walk_stream_event(368, 368), None)
    assert reeval["alertsCreated"] == 0
    after = integration_env.alerts.scan()["Items"]
    assert len(after) == 1
    assert after[0]["alertId"] == alert_id
    assert after[0]["status"] == "RESOLVED"

    # 7. The realtime channel saw ALERT_CREATED then ALERT_RESOLVED, both on the
    #    property broadcast channel.
    assert spy.event_types() == [
        rt.EVENT_ALERT_CREATED,
        rt.EVENT_ALERT_RESOLVED,
    ]
    channels = [channel for channel, _events in spy.calls]
    assert channels == [
        rt.broadcast_channel(DEMO_PROPERTY_ID),
        rt.broadcast_channel(DEMO_PROPERTY_ID),
    ]


@pytest.mark.skip(
    reason=(
        "The real-DynamoDB-Streams + real-Cognito-WebSocket closed-loop variant "
        "requires a live deploy: it needs genuine Streams propagation to trigger "
        "the rule evaluator Lambda and a Cognito-authorized AppSync Events "
        "WebSocket client to observe ALERT_CREATED/ALERT_RESOLVED. The "
        "in-process equivalent is covered by "
        "test_walk_risk_closed_loop_resolves_and_never_duplicates."
    )
)
def test_walk_risk_closed_loop_live_streams_and_websocket() -> None:  # pragma: no cover
    """Placeholder for the live-only Streams + Cognito WebSocket closed loop."""
