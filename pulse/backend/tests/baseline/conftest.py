"""Shared fixtures for the curated baseline builder tests.

Provides dummy AWS credentials and a moto-backed ``pulse-alerts`` table (single
``alertId`` hash key, matching the real key schema) so the reset-then-prime
builder can be exercised end-to-end without touching AWS.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

# Physical table name used by the moto environment (matches NAMING conventions
# and the other PULSE test suites).
ALERTS_TABLE_NAME = "pulse-alerts"


@pytest.fixture(autouse=True)
def _aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set dummy AWS credentials and region for moto-backed tests."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")


@pytest.fixture
def alerts_table() -> Iterator[Any]:
    """Yield a moto-backed ``pulse-alerts`` DynamoDB table resource.

    The table has the real single ``alertId`` hash-key schema so a seeded
    baseline item round-trips exactly as a fired alert would.
    """
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=ALERTS_TABLE_NAME,
            KeySchema=[{"AttributeName": "alertId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "alertId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        resource.Table(ALERTS_TABLE_NAME).meta.client.get_waiter("table_exists").wait(
            TableName=ALERTS_TABLE_NAME
        )
        yield resource.Table(ALERTS_TABLE_NAME)
