"""Unit tests for rule-admin, push-subscription, identity, and router wiring.

These cover the remaining Task 15 surface not exercised by the property tests:
rule-update validation with prior retention (Requirement 2.5), Web Push
subscription registration (Requirement 13), JWT-claim identity extraction, and
the thin router's dispatch and unauthenticated handling (Requirement 16.2).
"""

from __future__ import annotations

import json
from typing import Any

from moto import mock_aws

from pulse.api import handler, rules_admin, subscriptions
from pulse.api.identity import extract_identity
from tests.api.conftest import (
    RULES_TABLE_NAME,
    SUBSCRIPTIONS_TABLE_NAME,
    create_simple_table,
    identity,
    table_getter,
)

_VALID_RULE_BODY: dict[str, Any] = {
    "tier": "CRITICAL",
    "triggerCondition": {"operator": "gt", "left": "a", "right": "b"},
    "agentTriageEnabled": True,
    "escalationTimeoutSec": 300,
    "enabled": True,
}


# ---------------------------------------------------------------------------
# Identity extraction
# ---------------------------------------------------------------------------


def test_extract_identity_http_api_v2_shape() -> None:
    """Claims under authorizer.jwt.claims yield alias + associated properties."""
    event = {
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": {
                        "cognito:username": "jsmith",
                        "custom:properties": '["P-A", "P-B"]',
                    }
                }
            }
        }
    }
    caller = extract_identity(event)
    assert caller.gm_alias == "jsmith"
    assert caller.properties == frozenset({"P-A", "P-B"})


def test_extract_identity_rest_shape_and_csv_properties() -> None:
    """Claims under authorizer.claims with a CSV property list are parsed."""
    event = {
        "requestContext": {
            "authorizer": {"claims": {"username": "rmoore", "properties": "P-A, P-C"}}
        }
    }
    caller = extract_identity(event)
    assert caller.gm_alias == "rmoore"
    assert caller.properties == frozenset({"P-A", "P-C"})


def test_extract_identity_singular_custom_property_id() -> None:
    """Singular ``custom:propertyId`` claim yields a one-element property set.

    LUMI provisions the singular attribute, not the plural ``custom:properties``
    (BUG-013), so a GM token carrying only ``custom:propertyId`` must still
    resolve to that one property.
    """
    event = {
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": {
                        "cognito:username": "jsmith",
                        "custom:propertyId": "ALOHA-CHI-001",
                    }
                }
            }
        }
    }
    caller = extract_identity(event)
    assert caller.gm_alias == "jsmith"
    assert caller.properties == frozenset({"ALOHA-CHI-001"})
    assert caller.is_associated_with("ALOHA-CHI-001")


# ---------------------------------------------------------------------------
# Rule admin (Requirement 2.5)
# ---------------------------------------------------------------------------


def test_update_rule_accepts_valid_definition() -> None:
    """A valid update is validated and persisted to pulse-rules."""
    with mock_aws():
        create_simple_table(RULES_TABLE_NAME, "propertyId", "ruleType")
        caller = identity("admin", {"P-A"})
        result = rules_admin.update_rule(
            "P-A#WALK_RISK",
            _VALID_RULE_BODY,
            caller,
            rules_table_name=RULES_TABLE_NAME,
            table_getter=table_getter,
        )
        stored = (
            table_getter(RULES_TABLE_NAME)
            .get_item(Key={"propertyId": "P-A", "ruleType": "WALK_RISK"})
            .get("Item")
        )

    assert result["accepted"] is True
    assert stored is not None
    assert stored["escalationTimeoutSec"] == 300


def test_update_rule_rejects_out_of_range_and_retains_prior() -> None:
    """An out-of-range timeout is rejected and nothing is written."""
    body = dict(_VALID_RULE_BODY, escalationTimeoutSec=5)  # below the 60s minimum
    with mock_aws():
        create_simple_table(RULES_TABLE_NAME, "propertyId", "ruleType")
        caller = identity("admin", {"P-A"})
        result = rules_admin.update_rule(
            "P-A#WALK_RISK",
            body,
            caller,
            rules_table_name=RULES_TABLE_NAME,
            table_getter=table_getter,
        )
        stored = (
            table_getter(RULES_TABLE_NAME)
            .get_item(Key={"propertyId": "P-A", "ruleType": "WALK_RISK"})
            .get("Item")
        )

    assert result["accepted"] is False
    assert result["invalidAttribute"] == "escalationTimeoutSec"
    # Rejected update writes nothing (prior definition retained).
    assert stored is None


def test_update_rule_denied_for_unassociated_property() -> None:
    """A rule update for a non-associated property is denied."""
    with mock_aws():
        create_simple_table(RULES_TABLE_NAME, "propertyId", "ruleType")
        caller = identity("admin", {"P-A"})
        result = rules_admin.update_rule(
            "P-B#WALK_RISK",
            _VALID_RULE_BODY,
            caller,
            rules_table_name=RULES_TABLE_NAME,
            table_getter=table_getter,
        )
    assert result.get("denied") is True


# ---------------------------------------------------------------------------
# Push subscriptions (Requirement 13)
# ---------------------------------------------------------------------------


def test_register_subscription_persists_under_caller() -> None:
    """A valid subscription is stored keyed by the caller's alias."""
    with mock_aws():
        create_simple_table(SUBSCRIPTIONS_TABLE_NAME, "gmAlias", "endpointHash")
        caller = identity("jsmith", {"P-A"})
        result = subscriptions.register_subscription(
            {"endpoint": "https://push/ep", "p256dh": "k", "auth": "a"},
            caller,
            subscriptions_table_name=SUBSCRIPTIONS_TABLE_NAME,
            table_getter=table_getter,
        )
        stored = (
            table_getter(SUBSCRIPTIONS_TABLE_NAME)
            .get_item(
                Key={
                    "gmAlias": "jsmith",
                    "endpointHash": result["endpointHash"],
                }
            )
            .get("Item")
        )

    assert result["registered"] is True
    assert stored["endpoint"] == "https://push/ep"
    assert stored["propertyIds"] == ["P-A"]


def test_register_subscription_rejects_missing_fields() -> None:
    """A subscription missing encryption keys is rejected."""
    caller = identity("jsmith", {"P-A"})
    try:
        subscriptions.build_subscription_item(
            {"endpoint": "https://push/ep"}, caller, now="2026-08-17T00:00:00Z"
        )
    except subscriptions.InvalidSubscriptionError as exc:
        assert exc.missing == "p256dh"
    else:  # pragma: no cover - explicit failure if no raise
        raise AssertionError("expected InvalidSubscriptionError")


# ---------------------------------------------------------------------------
# Router / lambda_handler
# ---------------------------------------------------------------------------


def _event(method: str, path: str, claims: dict[str, Any]) -> dict[str, Any]:
    """Build an HTTP API v2 proxy event with the given claims."""
    return {
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {"jwt": {"claims": claims}},
        },
        "rawPath": path,
        "queryStringParameters": {},
    }


def test_lambda_handler_rejects_unauthenticated() -> None:
    """A request without a resolvable identity is rejected 401."""
    event = {"rawPath": "/alerts", "requestContext": {"http": {"method": "GET"}}}
    response = handler.lambda_handler(event, None)
    assert response["statusCode"] == 401


def test_lambda_handler_lists_alerts_scoped(monkeypatch: Any) -> None:
    """GET /alerts routes to the feed and returns a JSON body."""
    from tests.api.conftest import ALERTS_TABLE_NAME, create_alerts_table

    with mock_aws():
        create_alerts_table()
        monkeypatch.setenv("ALERTS_TABLE_NAME", ALERTS_TABLE_NAME)
        # Route through the real handler; the feed is empty but well-formed.
        event = _event(
            "GET",
            "/alerts",
            {"cognito:username": "jsmith", "properties": "P-A"},
        )
        # Point the default table getter at the mock via monkeypatching get_table.
        monkeypatch.setattr(
            "pulse.api.alerts_repository.get_table", table_getter, raising=True
        )
        response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["alerts"] == []
    assert body["count"] == 0


def test_lambda_handler_realtime_config_route(monkeypatch: Any) -> None:
    """GET /config/realtime returns the endpoint + namespace."""
    monkeypatch.setenv("REALTIME_HTTP_ENDPOINT", "https://events.example.com")
    monkeypatch.setenv("REALTIME_WSS_ENDPOINT", "wss://events.example.com/realtime")
    event = _event(
        "GET", "/config/realtime", {"cognito:username": "jsmith", "properties": "P-A"}
    )
    response = handler.lambda_handler(event, None)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["namespace"] == "pulse"
    assert body["httpEndpoint"] == "https://events.example.com"


# ---------------------------------------------------------------------------
# Kitchen route (GET /kitchen)
# ---------------------------------------------------------------------------


def test_lambda_handler_kitchen_returns_snapshot(monkeypatch: Any) -> None:
    """GET /kitchen?propertyId= returns the property-scoped snapshot."""
    from tests.api.conftest import (
        KITCHEN_TABLE_NAME,
        create_kitchen_table,
        make_kitchen_item,
    )

    with mock_aws():
        table = create_kitchen_table()
        table.put_item(Item=make_kitchen_item("ALOHA-CHI-001"))
        monkeypatch.setenv("KITCHEN_TABLE_NAME", KITCHEN_TABLE_NAME)
        monkeypatch.setattr(
            "pulse.api.kitchen_repository.get_table", table_getter, raising=True
        )
        event = _event(
            "GET",
            "/kitchen",
            {"cognito:username": "jsmith", "properties": "ALOHA-CHI-001"},
        )
        event["queryStringParameters"] = {"propertyId": "ALOHA-CHI-001"}
        response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["propertyId"] == "ALOHA-CHI-001"
    assert body["channelMixNote"] == "note"
    assert isinstance(body["kitchenOrders"], list)


def test_lambda_handler_kitchen_requires_property_id() -> None:
    """GET /kitchen without propertyId is a 400."""
    event = _event(
        "GET", "/kitchen", {"cognito:username": "jsmith", "properties": "ALOHA-CHI-001"}
    )
    response = handler.lambda_handler(event, None)
    assert response["statusCode"] == 400


def test_lambda_handler_kitchen_out_of_scope_is_403() -> None:
    """GET /kitchen for a non-associated property is a 403."""
    event = _event(
        "GET", "/kitchen", {"cognito:username": "jsmith", "properties": "ALOHA-CHI-001"}
    )
    event["queryStringParameters"] = {"propertyId": "ALOHA-MIA-001"}
    response = handler.lambda_handler(event, None)
    assert response["statusCode"] == 403


# ---------------------------------------------------------------------------
# Demo scenario routes (follow-up fix; gated on ENABLE_DEMO_SIMULATOR)
# ---------------------------------------------------------------------------


def test_demo_scenario_run_routes_to_simulator(monkeypatch: Any) -> None:
    """POST /demo/scenarios/walk-risk drives the simulator in-process (run)."""
    from pulse.demo_simulator import simulator

    monkeypatch.setenv("ENABLE_DEMO_SIMULATOR", "true")
    captured: dict[str, Any] = {}

    def _fake_apply(scenario: Any, action: str, **kwargs: Any) -> Any:
        captured["scenarioId"] = scenario.scenario_id
        captured["action"] = action
        captured["property_id"] = kwargs.get("property_id")
        return simulator.MutationPlan(
            scenario_id=scenario.scenario_id,
            action=action,
            table_kind=scenario.table_kind,
            operation="put",
            key={"propertyId": "P-A"},
        )

    monkeypatch.setattr(simulator, "apply_scenario", _fake_apply, raising=True)
    event = _event(
        "POST",
        "/demo/scenarios/walk-risk",
        {"cognito:username": "jsmith", "properties": "P-A"},
    )
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["scenarioId"] == "walk-risk"
    assert body["action"] == "run"
    # The demo targets the caller's OWN property (server-side from the claim),
    # not the hardcoded canonical demo property.
    assert captured["scenarioId"] == "walk-risk"
    assert captured["action"] == "run"
    assert captured["property_id"] == "P-A"


def test_demo_scenario_reset_routes_to_simulator(monkeypatch: Any) -> None:
    """POST /demo/scenarios/{id}/reset drives the simulator with action reset."""
    from pulse.demo_simulator import simulator

    monkeypatch.setenv("ENABLE_DEMO_SIMULATOR", "true")
    seen: dict[str, Any] = {}

    def _fake_apply(scenario: Any, action: str, **_kwargs: Any) -> Any:
        seen["action"] = action
        return simulator.MutationPlan(
            scenario_id=scenario.scenario_id,
            action=action,
            table_kind=scenario.table_kind,
            operation="put",
            key={},
        )

    monkeypatch.setattr(simulator, "apply_scenario", _fake_apply, raising=True)
    event = _event(
        "POST",
        "/demo/scenarios/walk-risk/reset",
        {"cognito:username": "jsmith", "properties": "P-A"},
    )
    response = handler.lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert seen["action"] == "reset"


def test_demo_scenario_unknown_id_returns_404(monkeypatch: Any) -> None:
    """An unknown scenario id yields 404 (no simulator mutation)."""
    monkeypatch.setenv("ENABLE_DEMO_SIMULATOR", "true")
    event = _event(
        "POST",
        "/demo/scenarios/does-not-exist",
        {"cognito:username": "jsmith", "properties": "P-A"},
    )
    response = handler.lambda_handler(event, None)
    assert response["statusCode"] == 404


def test_demo_routes_return_404_when_disabled(monkeypatch: Any) -> None:
    """With ENABLE_DEMO_SIMULATOR=false the /demo routes 404 as if absent."""
    monkeypatch.setenv("ENABLE_DEMO_SIMULATOR", "false")
    event = _event(
        "POST",
        "/demo/scenarios/walk-risk",
        {"cognito:username": "jsmith", "properties": "P-A"},
    )
    response = handler.lambda_handler(event, None)
    assert response["statusCode"] == 404
