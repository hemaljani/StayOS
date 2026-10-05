"""Unit tests for LUMI settings handler.

Tests GET /v1/settings/{gmAlias} and PUT /v1/settings/{gmAlias}
including success cases, 404, validation errors, and ownership enforcement.
"""

import json
from typing import Any, Dict
from unittest.mock import patch

import boto3
import moto
import pytest

from exceptions import ForbiddenError, SettingsNotFoundException, ValidationError
from handlers.settings_handler import get_settings, put_settings


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_settings_item() -> Dict[str, Any]:
    """Sample DynamoDB settings item for jsmith."""
    return {
        "gmAlias": "jsmith",
        "propertyId": "ALOHA-CHI-001",
        "briefDeliveryTime": "06:30",
        "alertToggles": {
            "overbookingRisk": True,
            "roomsOutOfOrder": True,
            "vipArrivalAlert": True,
            "upsellOpportunity": True,
            "staffingConfirmed": True,
        },
        "kpiThresholds": {
            "occupancyAlertBelow": 70,
            "adrAlertBelow": 200,
        },
        "audioPreferences": {
            "language": "en-US",
            "briefLength": "standard",
        },
        "createdAt": "2026-08-01T00:00:00+00:00",
        "updatedAt": "2026-08-01T00:00:00+00:00",
    }


def _make_event(
    gm_alias: str = "jsmith",
    user_gm_alias: str = "jsmith",
    body: Any = None,
    method: str = "GET",
) -> Dict[str, Any]:
    """Build a minimal API Gateway event for the settings handler.

    Args:
        gm_alias: GM alias in the path.
        user_gm_alias: GM alias in Cognito claims.
        body: Optional request body (for PUT).
        method: HTTP method.

    Returns:
        API Gateway proxy event dictionary.
    """
    event: Dict[str, Any] = {
        "httpMethod": method,
        "path": f"/v1/settings/{gm_alias}",
        "requestContext": {
            "authorizer": {
                "claims": {
                    "custom:gmAlias": user_gm_alias,
                    "custom:propertyId": "ALOHA-CHI-001",
                }
            }
        },
    }
    if body is not None:
        event["body"] = json.dumps(body)
    else:
        event["body"] = None
    return event


# ---------------------------------------------------------------------------
# Tests: GET /v1/settings/{gmAlias}
# ---------------------------------------------------------------------------


@moto.mock_aws
def test_get_settings_success(sample_settings_item: Dict[str, Any]) -> None:
    """Verify successful settings retrieval returns expected fields."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="stayos-settings-test",
        KeySchema=[{"AttributeName": "gmAlias", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "gmAlias", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.put_item(Item=sample_settings_item)

    event = _make_event(gm_alias="jsmith")
    params = {"gmAlias": "jsmith"}

    with patch("handlers.settings_handler._dynamodb_resource", dynamodb), \
         patch("handlers.settings_handler.SETTINGS_TABLE_NAME", "stayos-settings-test"):
        result = get_settings(event, params)

    assert result["gmAlias"] == "jsmith"
    assert result["propertyId"] == "ALOHA-CHI-001"
    assert result["briefDeliveryTime"] == "06:30"
    assert result["alertToggles"]["overbookingRisk"] is True
    assert result["kpiThresholds"]["occupancyAlertBelow"] == 70
    assert result["audioPreferences"]["language"] == "en-US"


@moto.mock_aws
def test_get_settings_404_when_not_found() -> None:
    """Verify SettingsNotFoundException raised when GM does not exist."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName="stayos-settings-test",
        KeySchema=[{"AttributeName": "gmAlias", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "gmAlias", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    event = _make_event(gm_alias="nonexistent", user_gm_alias="nonexistent")
    params = {"gmAlias": "nonexistent"}

    with patch("handlers.settings_handler._dynamodb_resource", dynamodb), \
         patch("handlers.settings_handler.SETTINGS_TABLE_NAME", "stayos-settings-test"):
        with pytest.raises(SettingsNotFoundException) as exc_info:
            get_settings(event, params)

    assert exc_info.value.status_code == 404
    assert "nonexistent" in exc_info.value.message


@moto.mock_aws
def test_get_settings_ownership_enforcement() -> None:
    """Verify ForbiddenError when user tries to access another GM's settings."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName="stayos-settings-test",
        KeySchema=[{"AttributeName": "gmAlias", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "gmAlias", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    # User is jsmith but trying to access another GM's settings
    event = _make_event(gm_alias="otheruser", user_gm_alias="jsmith")
    params = {"gmAlias": "otheruser"}

    with patch("handlers.settings_handler._dynamodb_resource", dynamodb), \
         patch("handlers.settings_handler.SETTINGS_TABLE_NAME", "stayos-settings-test"):
        with pytest.raises(ForbiddenError) as exc_info:
            get_settings(event, params)

    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# Tests: PUT /v1/settings/{gmAlias}
# ---------------------------------------------------------------------------


@moto.mock_aws
def test_put_settings_success(sample_settings_item: Dict[str, Any]) -> None:
    """Verify successful settings update with valid body."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="stayos-settings-test",
        KeySchema=[{"AttributeName": "gmAlias", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "gmAlias", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.put_item(Item=sample_settings_item)

    update_body = {"briefDeliveryTime": "07:00"}
    event = _make_event(gm_alias="jsmith", body=update_body, method="PUT")
    params = {"gmAlias": "jsmith"}

    with patch("handlers.settings_handler._dynamodb_resource", dynamodb), \
         patch("handlers.settings_handler.SETTINGS_TABLE_NAME", "stayos-settings-test"):
        result = put_settings(event, params)

    assert result["briefDeliveryTime"] == "07:00"
    assert "updatedAt" in result
    # Other fields should remain unchanged
    assert result["alertToggles"]["overbookingRisk"] is True


@moto.mock_aws
def test_put_settings_400_with_invalid_body(sample_settings_item: Dict[str, Any]) -> None:
    """Verify ValidationError raised for invalid input."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="stayos-settings-test",
        KeySchema=[{"AttributeName": "gmAlias", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "gmAlias", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.put_item(Item=sample_settings_item)

    # Invalid time format
    update_body = {"briefDeliveryTime": "25:00"}
    event = _make_event(gm_alias="jsmith", body=update_body, method="PUT")
    params = {"gmAlias": "jsmith"}

    with patch("handlers.settings_handler._dynamodb_resource", dynamodb), \
         patch("handlers.settings_handler.SETTINGS_TABLE_NAME", "stayos-settings-test"):
        with pytest.raises(ValidationError) as exc_info:
            put_settings(event, params)

    assert exc_info.value.status_code == 400
    assert exc_info.value.field == "briefDeliveryTime"


@moto.mock_aws
def test_put_settings_404_when_not_found() -> None:
    """Verify SettingsNotFoundException when updating non-existent GM."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName="stayos-settings-test",
        KeySchema=[{"AttributeName": "gmAlias", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "gmAlias", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    update_body = {"briefDeliveryTime": "07:00"}
    event = _make_event(gm_alias="jsmith", body=update_body, method="PUT")
    params = {"gmAlias": "jsmith"}

    with patch("handlers.settings_handler._dynamodb_resource", dynamodb), \
         patch("handlers.settings_handler.SETTINGS_TABLE_NAME", "stayos-settings-test"):
        with pytest.raises(SettingsNotFoundException):
            put_settings(event, params)


@moto.mock_aws
def test_put_settings_ownership_enforcement(sample_settings_item: Dict[str, Any]) -> None:
    """Verify ForbiddenError when user tries to update another GM's settings."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="stayos-settings-test",
        KeySchema=[{"AttributeName": "gmAlias", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "gmAlias", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.put_item(Item=sample_settings_item)

    update_body = {"briefDeliveryTime": "07:00"}
    # User is "attacker" but trying to update jsmith's settings
    event = _make_event(gm_alias="jsmith", user_gm_alias="attacker", body=update_body, method="PUT")
    params = {"gmAlias": "jsmith"}

    with patch("handlers.settings_handler._dynamodb_resource", dynamodb), \
         patch("handlers.settings_handler.SETTINGS_TABLE_NAME", "stayos-settings-test"):
        with pytest.raises(ForbiddenError):
            put_settings(event, params)


@moto.mock_aws
def test_put_settings_merges_partial_update(sample_settings_item: Dict[str, Any]) -> None:
    """Verify partial update only changes provided fields."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="stayos-settings-test",
        KeySchema=[{"AttributeName": "gmAlias", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "gmAlias", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    table.put_item(Item=sample_settings_item)

    # Only update audioPreferences
    update_body = {"audioPreferences": {"language": "ja-JP", "briefLength": "brief"}}
    event = _make_event(gm_alias="jsmith", body=update_body, method="PUT")
    params = {"gmAlias": "jsmith"}

    with patch("handlers.settings_handler._dynamodb_resource", dynamodb), \
         patch("handlers.settings_handler.SETTINGS_TABLE_NAME", "stayos-settings-test"):
        result = put_settings(event, params)

    # Updated field should change
    assert result["audioPreferences"]["language"] == "ja-JP"
    assert result["audioPreferences"]["briefLength"] == "brief"
    # Other fields should remain unchanged
    assert result["briefDeliveryTime"] == "06:30"
    assert result["alertToggles"]["overbookingRisk"] is True
    assert result["kpiThresholds"]["occupancyAlertBelow"] == 70
