"""Shared fixtures and builders for PULSE REST API tests.

Provides dummy AWS credentials so moto-backed tests can create clients, plus
small helpers to create the ``pulse-alerts`` table (with the feed GSI) and to
build alert/claims fixtures without repeating boilerplate.
"""

from __future__ import annotations

from typing import Any, Optional

import boto3
import pytest

from pulse.api.identity import CallerIdentity

ALERTS_TABLE_NAME = "pulse-alerts"
RULES_TABLE_NAME = "pulse-rules"
SUBSCRIPTIONS_TABLE_NAME = "pulse-push-subscriptions"
KITCHEN_TABLE_NAME = "pulse-kitchen"

PROPERTY_CREATED_INDEX = "propertyId-createdAt-index"


@pytest.fixture(autouse=True)
def _aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set dummy AWS credentials and region for moto-backed tests."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


def table_getter(name: str) -> Any:
    """Return a fresh DynamoDB ``Table`` bound inside the active moto mock.

    A non-cached getter is used in tests so each mock_aws context gets its own
    resource rather than a process-cached one.

    Args:
        name: The table name.

    Returns:
        A boto3 DynamoDB ``Table`` resource.
    """
    return boto3.resource("dynamodb").Table(name)


def create_alerts_table() -> Any:
    """Create the ``pulse-alerts`` table with the feed GSI in moto.

    Returns:
        The created ``Table`` resource.
    """
    resource = boto3.resource("dynamodb")
    resource.create_table(
        TableName=ALERTS_TABLE_NAME,
        KeySchema=[{"AttributeName": "alertId", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "alertId", "AttributeType": "S"},
            {"AttributeName": "propertyId", "AttributeType": "S"},
            {"AttributeName": "createdAt", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": PROPERTY_CREATED_INDEX,
                "KeySchema": [
                    {"AttributeName": "propertyId", "KeyType": "HASH"},
                    {"AttributeName": "createdAt", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return resource.Table(ALERTS_TABLE_NAME)


def create_kitchen_table() -> Any:
    """Create the ``pulse-kitchen`` table (propertyId-only key) in moto.

    Returns:
        The created ``Table`` resource.
    """
    resource = boto3.resource("dynamodb")
    resource.create_table(
        TableName=KITCHEN_TABLE_NAME,
        KeySchema=[{"AttributeName": "propertyId", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "propertyId", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return resource.Table(KITCHEN_TABLE_NAME)


def make_kitchen_item(property_id: str) -> dict[str, Any]:
    """Build a minimal ``pulse-kitchen`` snapshot item for tests.

    Args:
        property_id: The owning property (partition key).

    Returns:
        A ``pulse-kitchen`` item dict with the nested snapshot attributes.
    """
    return {
        "propertyId": property_id,
        "banquetCountdown": {"title": "Banquet", "minutesRemaining": 18},
        "fbStats": [{"label": "Orders", "value": "47"}],
        "deliverySla": {"label": "Room Service SLA", "pct": 90},
        "kitchenOrders": [{"id": "ko-1", "title": "Room 1802"}],
        "channelMix": [{"label": "Room Svc", "pct": 62}],
        "channelMixNote": "note",
    }


def create_simple_table(name: str, partition_key: str, sort_key: str) -> Any:
    """Create a simple two-key table in moto (rules / subscriptions).

    Args:
        name: The table name.
        partition_key: The partition (HASH) key attribute name.
        sort_key: The sort (RANGE) key attribute name.

    Returns:
        The created ``Table`` resource.
    """
    resource = boto3.resource("dynamodb")
    resource.create_table(
        TableName=name,
        KeySchema=[
            {"AttributeName": partition_key, "KeyType": "HASH"},
            {"AttributeName": sort_key, "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": partition_key, "AttributeType": "S"},
            {"AttributeName": sort_key, "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return resource.Table(name)


def make_alert_item(
    alert_id: str,
    property_id: str,
    *,
    status: str = "UNACKNOWLEDGED",
    tier: str = "CRITICAL",
    created_at: str = "2026-08-17T14:30:00Z",
    approval_state: str = "PENDING",
    options: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Build a minimal ``pulse-alerts`` item for tests.

    Args:
        alert_id: The alert id.
        property_id: The owning property.
        status: The alert status.
        tier: The alert tier.
        created_at: The ISO 8601 creation timestamp.
        approval_state: The approval-record state.
        options: Optional triage-brief ranked options.

    Returns:
        A ``pulse-alerts`` item dict.
    """
    item: dict[str, Any] = {
        "alertId": alert_id,
        "propertyId": property_id,
        "tier": tier,
        "type": "WALK_RISK",
        "title": "Walk Risk",
        "detail": "374 confirmed vs 368 available",
        "status": status,
        "createdAt": created_at,
        "lastStatusChangeAt": created_at,
        "approval": {
            "state": approval_state,
            "selectedOption": None,
            "decidedBy": None,
            "decidedAt": None,
        },
    }
    if options is not None:
        item["triageBrief"] = {"summary": "s", "confidence": 90, "options": options}
    return item


def identity(gm_alias: str, properties: set[str]) -> CallerIdentity:
    """Build a :class:`CallerIdentity` for tests.

    Args:
        gm_alias: The caller identity.
        properties: The associated property set.

    Returns:
        A populated :class:`CallerIdentity`.
    """
    return CallerIdentity(gm_alias=gm_alias, properties=frozenset(properties))
