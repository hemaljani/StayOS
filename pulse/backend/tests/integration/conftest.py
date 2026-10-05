"""Shared fixtures for PULSE integration tests (Task 23).

Provides dummy AWS credentials plus a comprehensive moto-backed DynamoDB
environment holding every table the closed loop touches:

    * ``pulse-alerts`` (with the four production GSIs and a NEW_AND_OLD_IMAGES
      stream),
    * ``pulse-rules``,
    * ``pulse-alert-history`` (composite key + shift-handover GSI),
    * ``pulse-push-subscriptions``,
    * the three LUMI operational tables the simulator/executor read and write
      (``stayos-reservations``, ``stayos-rooms``, ``stayos-guests``).

The fixture sets the environment variables the shared config
(:mod:`pulse.common.config`) and operational-schema resolver
(:mod:`pulse.common.operational_schema`) read, clears the cached boto3
resource/table factories so all default seams target moto (not a live account),
and resets the module-level rules-repository singleton so no cached rules leak
between tests. ``TRIAGE_INVOKER_FUNCTION_NAME`` (and ``TRIAGE_RUNTIME_ARN``) are
explicitly unset so the best-effort async triage dispatch is a no-op (no
network) during the closed loop.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import boto3
import pytest
from moto import mock_aws

from pulse.common import aws as aws_factory
from pulse.common import dynamo as dynamo_module
from pulse.common.operational_schema import (
    GUESTS_SK,
    RESERVATIONS_SK,
    ROOMS_SK,
)
from pulse.rule_engine import rules_repository as rules_repo_module

# Physical table names for the moto environment (match NAMING conventions).
ALERTS_TABLE_NAME = "pulse-alerts"
RULES_TABLE_NAME = "pulse-rules"
ALERT_HISTORY_TABLE_NAME = "pulse-alert-history"
PUSH_SUBSCRIPTIONS_TABLE_NAME = "pulse-push-subscriptions"
RESERVATIONS_TABLE_NAME = "stayos-reservations"
ROOMS_TABLE_NAME = "stayos-rooms"
GUESTS_TABLE_NAME = "stayos-guests"

# The Bedrock model id the config loader requires (never invoked in tests).
TRIAGE_MODEL_ID = "us.anthropic.claude-sonnet-4-6"


@dataclass(frozen=True)
class IntegrationEnv:
    """Handles to the moto-backed tables used by the integration tests.

    Attributes:
        resource: The moto DynamoDB resource.
        alerts: The ``pulse-alerts`` table resource.
        rules: The ``pulse-rules`` table resource.
        history: The ``pulse-alert-history`` table resource.
        subscriptions: The ``pulse-push-subscriptions`` table resource.
        reservations: The ``stayos-reservations`` table resource.
        rooms: The ``stayos-rooms`` table resource.
        guests: The ``stayos-guests`` table resource.
    """

    resource: Any
    alerts: Any
    rules: Any
    history: Any
    subscriptions: Any
    reservations: Any
    rooms: Any
    guests: Any


@pytest.fixture(autouse=True)
def _aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set dummy AWS credentials and region for moto-backed tests."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


def _create_alerts_table(resource: Any) -> Any:
    """Create ``pulse-alerts`` with a NEW_AND_OLD_IMAGES stream.

    Only the two GSIs whose key attributes are always populated on a freshly
    created alert item (``propertyId``/``createdAt`` and ``propertyId``/
    ``status``) are created. The ``gmAlias-status-index`` is intentionally
    omitted: a rule-engine alert is created with ``gmAlias = None`` (no owning
    GM yet), and DynamoDB (and moto) reject writing an item whose *present* GSI
    key attribute is NULL. The full four-GSI schema is asserted against the CFN
    template by the smoke tests instead. The stream is enabled to mirror the
    real table (history/escalation fan-out source).
    """
    resource.create_table(
        TableName=ALERTS_TABLE_NAME,
        KeySchema=[{"AttributeName": "alertId", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "alertId", "AttributeType": "S"},
            {"AttributeName": "propertyId", "AttributeType": "S"},
            {"AttributeName": "createdAt", "AttributeType": "S"},
            {"AttributeName": "status", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "propertyId-createdAt-index",
                "KeySchema": [
                    {"AttributeName": "propertyId", "KeyType": "HASH"},
                    {"AttributeName": "createdAt", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "propertyId-status-index",
                "KeySchema": [
                    {"AttributeName": "propertyId", "KeyType": "HASH"},
                    {"AttributeName": "status", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        StreamSpecification={
            "StreamEnabled": True,
            "StreamViewType": "NEW_AND_OLD_IMAGES",
        },
        BillingMode="PAY_PER_REQUEST",
    )
    return resource.Table(ALERTS_TABLE_NAME)


def _create_rules_table(resource: Any) -> Any:
    """Create ``pulse-rules`` (propertyId HASH + ruleType RANGE)."""
    resource.create_table(
        TableName=RULES_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "propertyId", "KeyType": "HASH"},
            {"AttributeName": "ruleType", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "propertyId", "AttributeType": "S"},
            {"AttributeName": "ruleType", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return resource.Table(RULES_TABLE_NAME)


def _create_history_table(resource: Any) -> Any:
    """Create ``pulse-alert-history`` (alertId HASH + version RANGE) + GSI."""
    resource.create_table(
        TableName=ALERT_HISTORY_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "alertId", "KeyType": "HASH"},
            {"AttributeName": "version", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "alertId", "AttributeType": "S"},
            {"AttributeName": "version", "AttributeType": "N"},
            {"AttributeName": "propertyId", "AttributeType": "S"},
            {"AttributeName": "createdAt", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "propertyId-createdAt-index",
                "KeySchema": [
                    {"AttributeName": "propertyId", "KeyType": "HASH"},
                    {"AttributeName": "createdAt", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return resource.Table(ALERT_HISTORY_TABLE_NAME)


def _create_subscriptions_table(resource: Any) -> Any:
    """Create ``pulse-push-subscriptions`` (gmAlias HASH + endpointHash RANGE)."""
    resource.create_table(
        TableName=PUSH_SUBSCRIPTIONS_TABLE_NAME,
        KeySchema=[
            {"AttributeName": "gmAlias", "KeyType": "HASH"},
            {"AttributeName": "endpointHash", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "gmAlias", "AttributeType": "S"},
            {"AttributeName": "endpointHash", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return resource.Table(PUSH_SUBSCRIPTIONS_TABLE_NAME)


def _create_operational_table(resource: Any, name: str, sort_key: str) -> Any:
    """Create an operational table with a ``propertyId`` + sort-key schema."""
    resource.create_table(
        TableName=name,
        KeySchema=[
            {"AttributeName": "propertyId", "KeyType": "HASH"},
            {"AttributeName": sort_key, "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "propertyId", "AttributeType": "S"},
            {"AttributeName": sort_key, "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return resource.Table(name)


@pytest.fixture
def integration_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[IntegrationEnv]:
    """Yield a fully-wired moto DynamoDB environment for the closed loop.

    Sets every table-name / model-id environment variable the config and
    operational-schema modules read, clears the cached boto3 factories so all
    default seams talk to moto, unsets the triage runtime ARN so async triage is
    a no-op, and resets the rules-repository singleton so no cached rules leak
    across tests.
    """
    # Config-required resource identifiers (PulseConfig.load_config).
    monkeypatch.setenv("ALERTS_TABLE_NAME", ALERTS_TABLE_NAME)
    monkeypatch.setenv("RULES_TABLE_NAME", RULES_TABLE_NAME)
    monkeypatch.setenv("ALERT_HISTORY_TABLE_NAME", ALERT_HISTORY_TABLE_NAME)
    monkeypatch.setenv(
        "PUSH_SUBSCRIPTIONS_TABLE_NAME", PUSH_SUBSCRIPTIONS_TABLE_NAME
    )
    monkeypatch.setenv("KITCHEN_TABLE_NAME", "pulse-kitchen")
    monkeypatch.setenv("TRIAGE_MODEL_ID", TRIAGE_MODEL_ID)
    # Operational-table names for the simulator + action executor.
    monkeypatch.setenv("RESERVATIONS_TABLE_NAME", RESERVATIONS_TABLE_NAME)
    monkeypatch.setenv("ROOMS_TABLE_NAME", ROOMS_TABLE_NAME)
    monkeypatch.setenv("GUESTS_TABLE_NAME", GUESTS_TABLE_NAME)
    # Async triage must be a no-op (no network) during the loop: unset both the
    # evaluator's dispatch target and the invoker's runtime ARN.
    monkeypatch.delenv("TRIAGE_INVOKER_FUNCTION_NAME", raising=False)
    monkeypatch.delenv("TRIAGE_RUNTIME_ARN", raising=False)

    aws_factory.get_resource.cache_clear()
    dynamo_module.get_table.cache_clear()
    rules_repo_module._default_repository = None

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        env = IntegrationEnv(
            resource=resource,
            alerts=_create_alerts_table(resource),
            rules=_create_rules_table(resource),
            history=_create_history_table(resource),
            subscriptions=_create_subscriptions_table(resource),
            reservations=_create_operational_table(
                resource, RESERVATIONS_TABLE_NAME, RESERVATIONS_SK
            ),
            rooms=_create_operational_table(resource, ROOMS_TABLE_NAME, ROOMS_SK),
            guests=_create_operational_table(
                resource, GUESTS_TABLE_NAME, GUESTS_SK
            ),
        )
        yield env

    aws_factory.get_resource.cache_clear()
    dynamo_module.get_table.cache_clear()
    rules_repo_module._default_repository = None


class SpyPublisher:
    """A realtime publisher seam double that records publish calls in order.

    Attributes:
        calls: Recorded ``(channel, events)`` tuples for each publish batch, in
            call order.
    """

    def __init__(self) -> None:
        """Initialize with no recorded calls."""
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    def __call__(self, channel: str, events: Any) -> None:
        """Record a publish call.

        Args:
            channel: The channel path published to.
            events: The batch of events published.
        """
        self.calls.append((channel, list(events)))

    def event_types(self) -> list[str]:
        """Return the flattened list of published event types in order.

        Returns:
            The ``eventType`` of every event across all recorded batches.
        """
        return [
            str(event.get("eventType"))
            for _channel, events in self.calls
            for event in events
        ]
