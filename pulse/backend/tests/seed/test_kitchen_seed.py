"""Unit tests for the kitchen seed CloudFormation custom-resource handler.

Mirrors the behavior the handler copies from LUMI's ``Custom::SeedData`` and
PULSE's ``rules_seed`` (multi-property):

    * Create seeds a ``pulse-kitchen`` snapshot for EVERY configured property
      and reports SUCCESS,
    * Update skips a property whose snapshot already exists (per-property
      idempotency) but still fills in any missing property, SUCCESS,
    * Delete is a no-op that preserves the snapshots and reports SUCCESS,
    * a direct invoke with no ResponseURL performs the puts but skips the CFN
      PUT (the reseed path),
    * ``Force`` reseeds even already-seeded properties,
    * a single ``PropertyId`` override seeds just that property,
    * an unknown RequestType reports FAILED.

The ``pulse-kitchen`` table is created in moto; the module-level DynamoDB
resource is rebound inside each mock so calls route through moto. The CFN
ResponseURL PUT is captured by monkeypatching ``urllib.request.urlopen``.

# Feature: initial-pulse-project - kitchen seed custom resource
"""

from __future__ import annotations

import json
from typing import Any, Optional
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.config import Config
from moto import mock_aws

from pulse.seed import kitchen_seed
from pulse.seed.kitchen_snapshot import DEMO_PROPERTY_ID, build_kitchen_snapshot

KITCHEN_TABLE_NAME = "pulse-kitchen"

# The five pilot properties the handler seeds by default (ESTATE_PROPERTY_IDS
# unset -> DEFAULT_PROPERTY_IDS fallback).
ALL_PROPERTY_IDS = sorted(kitchen_seed.DEFAULT_PROPERTY_IDS)


def _lambda_context() -> Any:
    """Return a fake Lambda context for @logger.inject_lambda_context.

    The handler mirrors LUMI's seed handler, which is also decorated with
    Powertools ``inject_lambda_context``; that decorator reads context.function_name
    (and other runtime fields), so a real Lambda always supplies a context. Mirror
    LUMI's seed tests by passing a MagicMock with function_name set rather than None.
    """
    context = MagicMock()
    context.function_name = "pulse-kitchen-seed"
    return context


@pytest.fixture(autouse=True)
def _aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set dummy AWS credentials, region, and the table env var for moto tests."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv(kitchen_seed.ENV_KITCHEN_TABLE, KITCHEN_TABLE_NAME)
    # Ensure a deterministic estate: unset any inherited ESTATE_PROPERTY_IDS so
    # the handler uses DEFAULT_PROPERTY_IDS (the five pilot properties).
    monkeypatch.delenv(kitchen_seed.ENV_ESTATE_PROPERTY_IDS, raising=False)


class _ResponseRecorder:
    """Capture urlopen calls so the CFN response PUT can be asserted."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def __call__(self, request: Any) -> Any:
        self.calls.append(request)

        class _Ctx:
            status = 200

            def __enter__(self_inner) -> Any:
                return self_inner

            def __exit__(self_inner, *exc: Any) -> bool:
                return False

        return _Ctx()


def _create_kitchen_table() -> Any:
    """Create the ``pulse-kitchen`` table in moto and rebind the module resource.

    Returns:
        The created ``Table`` resource.
    """
    resource = boto3.resource(
        "dynamodb", config=Config(retries={"mode": "standard"})
    )
    resource.create_table(
        TableName=KITCHEN_TABLE_NAME,
        KeySchema=[{"AttributeName": "propertyId", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "propertyId", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    # Rebind the handler's module-level resource to one created inside this mock.
    kitchen_seed._dynamodb = resource
    return resource.Table(KITCHEN_TABLE_NAME)


def _event(request_type: str, *, with_url: bool = True, **extra: Any) -> dict[str, Any]:
    """Build a CloudFormation custom-resource event.

    Args:
        request_type: Create / Update / Delete / an unknown value.
        with_url: When True, include a ResponseURL (CFN-driven invoke).
        **extra: Additional event keys (e.g. PropertyId, Force).

    Returns:
        A CloudFormation custom-resource event dict.
    """
    event: dict[str, Any] = {
        "RequestType": request_type,
        "StackId": "stack-1",
        "RequestId": "req-1",
        "LogicalResourceId": "KitchenSeedCustomResource",
        "ResourceProperties": {},
        **extra,
    }
    if with_url:
        event["ResponseURL"] = "https://cfn.example.com/presigned"
    return event


def _sent_status(recorder: _ResponseRecorder) -> Optional[str]:
    """Return the Status field of the last CFN response PUT, or None."""
    if not recorder.calls:
        return None
    body = recorder.calls[-1].data.decode("utf-8")
    return json.loads(body)["Status"]


def _all_property_ids(table: Any) -> list[str]:
    """Return the sorted propertyIds present in the kitchen table."""
    items = table.scan().get("Items", [])
    return sorted(item["propertyId"] for item in items)


def test_create_seeds_all_pilot_properties(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Create request seeds a snapshot for every pilot property, SUCCESS."""
    recorder = _ResponseRecorder()
    monkeypatch.setattr(kitchen_seed.urllib.request, "urlopen", recorder)

    with mock_aws():
        table = _create_kitchen_table()
        kitchen_seed.lambda_handler(_event("Create"), _lambda_context())

        seeded_ids = _all_property_ids(table)
        chi = table.get_item(Key={"propertyId": DEMO_PROPERTY_ID}).get("Item")

    # Every pilot property now has a snapshot (was the single-property bug).
    assert seeded_ids == ALL_PROPERTY_IDS
    # The canonical demo property still gets the exact curated snapshot.
    assert chi == build_kitchen_snapshot(DEMO_PROPERTY_ID)
    assert _sent_status(recorder) == kitchen_seed.SUCCESS


def test_update_fills_missing_and_skips_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Update seeds missing properties but does not overwrite an existing one."""
    recorder = _ResponseRecorder()
    monkeypatch.setattr(kitchen_seed.urllib.request, "urlopen", recorder)

    with mock_aws():
        table = _create_kitchen_table()
        # Pre-seed a sentinel snapshot for CHI to detect an unwanted overwrite.
        table.put_item(Item={"propertyId": DEMO_PROPERTY_ID, "sentinel": "keep"})

        kitchen_seed.lambda_handler(_event("Update"), _lambda_context())

        seeded_ids = _all_property_ids(table)
        chi = table.get_item(Key={"propertyId": DEMO_PROPERTY_ID}).get("Item")

    # All pilot properties present; CHI's sentinel is untouched (idempotent skip).
    assert seeded_ids == ALL_PROPERTY_IDS
    assert chi == {"propertyId": DEMO_PROPERTY_ID, "sentinel": "keep"}
    assert _sent_status(recorder) == kitchen_seed.SUCCESS


def test_force_reseeds_existing_property(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force=True overwrites an already-seeded property with a fresh snapshot."""
    recorder = _ResponseRecorder()
    monkeypatch.setattr(kitchen_seed.urllib.request, "urlopen", recorder)

    with mock_aws():
        table = _create_kitchen_table()
        table.put_item(Item={"propertyId": DEMO_PROPERTY_ID, "sentinel": "keep"})

        kitchen_seed.lambda_handler(_event("Update", Force=True), _lambda_context())

        chi = table.get_item(Key={"propertyId": DEMO_PROPERTY_ID}).get("Item")

    # Force overwrote the sentinel with the real curated snapshot.
    assert chi == build_kitchen_snapshot(DEMO_PROPERTY_ID)
    assert _sent_status(recorder) == kitchen_seed.SUCCESS


def test_single_property_override_seeds_only_that_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct invoke with PropertyId seeds just that one property."""
    recorder = _ResponseRecorder()
    monkeypatch.setattr(kitchen_seed.urllib.request, "urlopen", recorder)

    target = "ALOHA-MIA-001"
    with mock_aws():
        table = _create_kitchen_table()
        kitchen_seed.lambda_handler(
            _event("Create", with_url=False, PropertyId=target), _lambda_context()
        )

        seeded_ids = _all_property_ids(table)
        item = table.get_item(Key={"propertyId": target}).get("Item")

    assert seeded_ids == [target]
    assert item == build_kitchen_snapshot(target)
    # Direct invoke skipped the CFN PUT (no ResponseURL).
    assert recorder.calls == []


def test_delete_is_noop_preserving_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Delete request preserves the snapshots and reports SUCCESS."""
    recorder = _ResponseRecorder()
    monkeypatch.setattr(kitchen_seed.urllib.request, "urlopen", recorder)

    with mock_aws():
        table = _create_kitchen_table()
        table.put_item(Item=build_kitchen_snapshot(DEMO_PROPERTY_ID))

        kitchen_seed.lambda_handler(_event("Delete"), _lambda_context())

        item = table.get_item(Key={"propertyId": DEMO_PROPERTY_ID}).get("Item")

    assert item == build_kitchen_snapshot(DEMO_PROPERTY_ID)
    assert _sent_status(recorder) == kitchen_seed.SUCCESS


def test_direct_invoke_skips_cfn_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """A direct invoke (no ResponseURL) seeds but skips the CFN PUT (reseed path)."""
    recorder = _ResponseRecorder()
    monkeypatch.setattr(kitchen_seed.urllib.request, "urlopen", recorder)

    with mock_aws():
        table = _create_kitchen_table()
        kitchen_seed.lambda_handler(_event("Create", with_url=False), _lambda_context())

        seeded_ids = _all_property_ids(table)

    # The puts happened for the whole estate, but no CFN response was PUT.
    assert seeded_ids == ALL_PROPERTY_IDS
    assert recorder.calls == []


def test_unknown_request_type_reports_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown RequestType reports FAILED to CloudFormation."""
    recorder = _ResponseRecorder()
    monkeypatch.setattr(kitchen_seed.urllib.request, "urlopen", recorder)

    with mock_aws():
        _create_kitchen_table()
        kitchen_seed.lambda_handler(_event("Snapshot"), _lambda_context())

    assert _sent_status(recorder) == kitchen_seed.FAILED
