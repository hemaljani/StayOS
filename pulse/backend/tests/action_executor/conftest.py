"""Shared fixtures for Action Executor tests.

Provides dummy AWS credentials plus a moto-backed DynamoDB environment holding
the ``pulse-alerts`` table and the three operational tables the Action Executor
writes back to (``stayos-reservations``, ``stayos-rooms``, ``stayos-guests``). The
environment sets the operational-table name environment variables the shared
:mod:`pulse.common.operational_schema` resolves and clears the cached boto3
resource/table factories so the executor's default transaction writer talks to
moto rather than a real account.
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

# Physical table names used by the moto environment (match NAMING conventions).
ALERTS_TABLE_NAME = "pulse-alerts"
RESERVATIONS_TABLE_NAME = "stayos-reservations"
ROOMS_TABLE_NAME = "stayos-rooms"
GUESTS_TABLE_NAME = "stayos-guests"


@dataclass(frozen=True)
class DynamoEnv:
    """Handles to the moto-backed tables under test.

    Attributes:
        resource: The moto DynamoDB resource.
        alerts: The ``pulse-alerts`` table resource.
        reservations: The ``stayos-reservations`` table resource.
        rooms: The ``stayos-rooms`` table resource.
        guests: The ``stayos-guests`` table resource.
    """

    resource: Any
    alerts: Any
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
    """Create the ``pulse-alerts`` table (single ``alertId`` hash key)."""
    resource.create_table(
        TableName=ALERTS_TABLE_NAME,
        KeySchema=[{"AttributeName": "alertId", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "alertId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    return resource.Table(ALERTS_TABLE_NAME)


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
def dynamo_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[DynamoEnv]:
    """Yield a moto DynamoDB environment wired to the executor's factories.

    The operational-table name environment variables are set so
    :mod:`pulse.common.operational_schema` resolves them, and the cached boto3
    resource/table factories are cleared so the executor's default transaction
    writer (via ``get_dynamo_client``) targets moto.
    """
    monkeypatch.setenv("RESERVATIONS_TABLE_NAME", RESERVATIONS_TABLE_NAME)
    monkeypatch.setenv("ROOMS_TABLE_NAME", ROOMS_TABLE_NAME)
    monkeypatch.setenv("GUESTS_TABLE_NAME", GUESTS_TABLE_NAME)
    # The default transaction writer resolves a cached resource; clear the cache
    # so it is recreated inside the moto context (and again on teardown).
    aws_factory.get_resource.cache_clear()
    dynamo_module.get_table.cache_clear()
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        env = DynamoEnv(
            resource=resource,
            alerts=_create_alerts_table(resource),
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


class SpyPublisher:
    """A realtime publisher seam double that records publish calls.

    Attributes:
        calls: Recorded ``(channel, events)`` tuples for each publish.
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
