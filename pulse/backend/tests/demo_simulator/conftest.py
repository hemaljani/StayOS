"""Shared fixtures for Demo Scenario Simulator tests.

Provides dummy AWS credentials and a moto-backed DynamoDB environment holding
the three operational tables the simulator writes to (``stayos-reservations``,
``stayos-rooms``, ``stayos-guests``), plus the operational-table name environment
variables the shared :mod:`pulse.common.operational_schema` resolves.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from pulse.common.operational_schema import (
    GUESTS_SK,
    RESERVATIONS_SK,
    ROOMS_SK,
)

RESERVATIONS_TABLE_NAME = "stayos-reservations"
ROOMS_TABLE_NAME = "stayos-rooms"
GUESTS_TABLE_NAME = "stayos-guests"


@pytest.fixture(autouse=True)
def _aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set dummy AWS credentials and region for moto-backed tests."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


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
def operational_tables(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Yield a moto DynamoDB resource with the three operational tables.

    Sets the operational-table name environment variables so
    :mod:`pulse.common.operational_schema` resolves them and the simulator can
    look up each scenario's target table by name.
    """
    monkeypatch.setenv("RESERVATIONS_TABLE_NAME", RESERVATIONS_TABLE_NAME)
    monkeypatch.setenv("ROOMS_TABLE_NAME", ROOMS_TABLE_NAME)
    monkeypatch.setenv("GUESTS_TABLE_NAME", GUESTS_TABLE_NAME)
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        # Use the real LUMI operational-table sort-key names (resolved from the
        # shared schema module) so the mock matches production and cannot drift.
        _create_operational_table(resource, RESERVATIONS_TABLE_NAME, RESERVATIONS_SK)
        _create_operational_table(resource, ROOMS_TABLE_NAME, ROOMS_SK)
        _create_operational_table(resource, GUESTS_TABLE_NAME, GUESTS_SK)
        yield resource
