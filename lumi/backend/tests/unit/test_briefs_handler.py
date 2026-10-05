"""Unit tests for LUMI briefs handler.

Tests GET /v1/briefs/{propertyId} including successful retrieval,
404 when no brief exists, response shape, and ownership validation.
"""

import os
from decimal import Decimal
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import boto3
import moto
import pytest

from exceptions import BriefNotFoundException, ForbiddenError, ValidationError
from handlers.briefs_handler import get_brief, get_brief_by_date


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_brief_item() -> Dict[str, Any]:
    """Sample DynamoDB brief item matching the API response schema.

    Uses Decimal for numeric types as required by DynamoDB/boto3.
    """
    return {
        "propertyId": "ALOHA-CHI-001",
        "briefDate": "2026-08-03",
        "generatedAt": "2026-08-03T06:30:00-05:00",
        "property": {
            "propertyId": "ALOHA-CHI-001",
            "propertyName": "Aloha Grand Chicago",
            "brand": "Aloha Hotels & Resorts",
            "timezone": "America/Chicago",
            "totalRooms": Decimal("368"),
        },
        "dailyKPIs": {
            "date": "2026-08-03",
            "asOf": "2026-08-03T09:07:00-05:00",
            "occupancy": {"current": Decimal("87"), "unit": "percent", "vsLastWeek": Decimal("4.2")},
            "adr": {"current": Decimal("248"), "currency": "USD", "vsLastWeek": Decimal("12")},
            "revPAR": {"current": Decimal("216"), "currency": "USD", "vsYOY": Decimal("7.1")},
            "arrivals": {"total": Decimal("142"), "vipCount": Decimal("7")},
            "departures": {"total": Decimal("118")},
        },
        "actionItems": [
            {
                "id": "action-001",
                "type": "OVERBOOKING_RISK",
                "severity": "URGENT",
                "title": "Overbooking Risk - +6 Rooms",
                "detail": "374 confirmed vs 368 available.",
            }
        ],
        "vipArrivals": [
            {
                "guestId": "ALH-MBR-00238471",
                "guestName": "David Chen",
                "loyaltyTier": "AMBASSADOR",
                "roomNumber": "2401",
            }
        ],
        "audioBrief": {
            "briefId": "brief-2026-08-03-ALOHA-CHI-001",
            "durationSeconds": Decimal("74"),
            "status": "READY",
            "s3Key": "briefs/2026/08/03/ALOHA-CHI-001/morning-brief.mp3",
            "transcriptSnippet": "Good morning, Jennifer...",
        },
    }


def _make_event(
    property_id: str = "ALOHA-CHI-001",
    user_property_id: str = "ALOHA-CHI-001",
) -> Dict[str, Any]:
    """Build a minimal API Gateway event for the briefs handler.

    Args:
        property_id: Property ID in the path.
        user_property_id: Property ID in Cognito claims.

    Returns:
        API Gateway proxy event dictionary.
    """
    return {
        "httpMethod": "GET",
        "path": f"/v1/briefs/{property_id}",
        "requestContext": {
            "authorizer": {
                "claims": {
                    "custom:propertyId": user_property_id,
                    "custom:gmAlias": "jsmith",
                }
            }
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@moto.mock_aws
def test_get_brief_success(mock_brief_item: Dict[str, Any]) -> None:
    """Verify successful brief retrieval returns expected shape."""
    # Create mocked DynamoDB table
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="stayos-briefs-test",
        KeySchema=[
            {"AttributeName": "propertyId", "KeyType": "HASH"},
            {"AttributeName": "briefDate", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "propertyId", "AttributeType": "S"},
            {"AttributeName": "briefDate", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.put_item(Item=mock_brief_item)

    event = _make_event()
    params = {"propertyId": "ALOHA-CHI-001"}

    with patch("handlers.briefs_handler._dynamodb_resource", dynamodb), \
         patch("handlers.briefs_handler.BRIEFS_TABLE_NAME", "stayos-briefs-test"):
        result = get_brief(event, params)

    # Verify response contains expected top-level keys
    assert "property" in result
    assert "dailyKPIs" in result
    assert "actionItems" in result
    assert "vipArrivals" in result
    assert "audioBrief" in result


@moto.mock_aws
def test_get_brief_constructs_audio_url(mock_brief_item: Dict[str, Any]) -> None:
    """Verify audio URL is constructed from CloudFront domain and s3Key."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="stayos-briefs-test",
        KeySchema=[
            {"AttributeName": "propertyId", "KeyType": "HASH"},
            {"AttributeName": "briefDate", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "propertyId", "AttributeType": "S"},
            {"AttributeName": "briefDate", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table.put_item(Item=mock_brief_item)

    event = _make_event()
    params = {"propertyId": "ALOHA-CHI-001"}

    with patch("handlers.briefs_handler._dynamodb_resource", dynamodb), \
         patch("handlers.briefs_handler.BRIEFS_TABLE_NAME", "stayos-briefs-test"), \
         patch("handlers.briefs_handler.AUDIO_CLOUDFRONT_DOMAIN", "d1234567890.cloudfront.net"):
        result = get_brief(event, params)

    expected_url = "https://d1234567890.cloudfront.net/briefs/2026/08/03/ALOHA-CHI-001/morning-brief.mp3"
    assert result["audioBrief"]["audioUrl"] == expected_url


@moto.mock_aws
def test_get_brief_404_when_no_brief_exists() -> None:
    """Verify BriefNotFoundException raised when no items in table."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName="stayos-briefs-test",
        KeySchema=[
            {"AttributeName": "propertyId", "KeyType": "HASH"},
            {"AttributeName": "briefDate", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "propertyId", "AttributeType": "S"},
            {"AttributeName": "briefDate", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    # ALOHA-XYZ-999 is a validly-formatted (ALOHA-[A-Z]{3}-\d{3}) but
    # deliberately-unseeded property, so the request passes the propertyId
    # format check and reaches the 404 (no-brief) path under test. A code like
    # "NONE" (4 letters) would be rejected as a 400 before ever getting there.
    event = _make_event(property_id="ALOHA-XYZ-999", user_property_id="ALOHA-XYZ-999")
    params = {"propertyId": "ALOHA-XYZ-999"}

    with patch("handlers.briefs_handler._dynamodb_resource", dynamodb), \
         patch("handlers.briefs_handler.BRIEFS_TABLE_NAME", "stayos-briefs-test"):
        with pytest.raises(BriefNotFoundException) as exc_info:
            get_brief(event, params)

    assert exc_info.value.status_code == 404
    assert "ALOHA-XYZ-999" in exc_info.value.message


@moto.mock_aws
def test_get_brief_ownership_validation_rejects_mismatch() -> None:
    """Verify ForbiddenError raised when user property does not match request."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    dynamodb.create_table(
        TableName="stayos-briefs-test",
        KeySchema=[
            {"AttributeName": "propertyId", "KeyType": "HASH"},
            {"AttributeName": "briefDate", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "propertyId", "AttributeType": "S"},
            {"AttributeName": "briefDate", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    # User owns ALOHA-CHI-001 but requests a different property
    event = _make_event(property_id="ALOHA-NYC-001", user_property_id="ALOHA-CHI-001")
    params = {"propertyId": "ALOHA-NYC-001"}

    with patch("handlers.briefs_handler._dynamodb_resource", dynamodb), \
         patch("handlers.briefs_handler.BRIEFS_TABLE_NAME", "stayos-briefs-test"):
        with pytest.raises(ForbiddenError) as exc_info:
            get_brief(event, params)

    assert exc_info.value.status_code == 403


@moto.mock_aws
def test_get_brief_returns_latest_when_multiple_exist(
    mock_brief_item: Dict[str, Any],
) -> None:
    """Verify only the most recent brief is returned (descending sort)."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.create_table(
        TableName="stayos-briefs-test",
        KeySchema=[
            {"AttributeName": "propertyId", "KeyType": "HASH"},
            {"AttributeName": "briefDate", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "propertyId", "AttributeType": "S"},
            {"AttributeName": "briefDate", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )

    # Insert an older brief
    older_brief = mock_brief_item.copy()
    older_brief["briefDate"] = "2026-08-02"
    table.put_item(Item=older_brief)

    # Insert the latest brief
    table.put_item(Item=mock_brief_item)

    event = _make_event()
    params = {"propertyId": "ALOHA-CHI-001"}

    with patch("handlers.briefs_handler._dynamodb_resource", dynamodb), \
         patch("handlers.briefs_handler.BRIEFS_TABLE_NAME", "stayos-briefs-test"):
        result = get_brief(event, params)

    # Should return the latest brief (2026-08-03, not 2026-08-02)
    assert result["briefDate"] == "2026-08-03"



@moto.mock_aws
def test_get_brief_rejects_invalid_property_id_format() -> None:
    """A malformed propertyId is rejected with a ValidationError before lookup."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")

    # "NONE" is 4 letters, so it fails the ALOHA-[A-Z]{3}-\d{3} format check.
    event = _make_event(property_id="ALOHA-NONE-001", user_property_id="ALOHA-NONE-001")
    params = {"propertyId": "ALOHA-NONE-001"}

    with patch("handlers.briefs_handler._dynamodb_resource", dynamodb), \
         patch("handlers.briefs_handler.BRIEFS_TABLE_NAME", "stayos-briefs-test"):
        with pytest.raises(ValidationError) as exc_info:
            get_brief(event, params)

    assert exc_info.value.field == "propertyId"


@moto.mock_aws
def test_get_brief_forbidden_when_property_claim_missing() -> None:
    """A token with no custom:propertyId claim is rejected as forbidden."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")

    # Well-formed propertyId, but the claims carry no custom:propertyId.
    event = {
        "httpMethod": "GET",
        "path": "/v1/briefs/ALOHA-CHI-001",
        "requestContext": {"authorizer": {"claims": {"custom:gmAlias": "jsmith"}}},
    }
    params = {"propertyId": "ALOHA-CHI-001"}

    with patch("handlers.briefs_handler._dynamodb_resource", dynamodb), \
         patch("handlers.briefs_handler.BRIEFS_TABLE_NAME", "stayos-briefs-test"):
        with pytest.raises(ForbiddenError) as exc_info:
            get_brief(event, params)

    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# get_brief_by_date (GET /v1/briefs/{propertyId}/{briefDate})
# ---------------------------------------------------------------------------


def _create_briefs_table(dynamodb: Any) -> Any:
    """Create the moto-backed briefs table with the composite key schema."""
    return dynamodb.create_table(
        TableName="stayos-briefs-test",
        KeySchema=[
            {"AttributeName": "propertyId", "KeyType": "HASH"},
            {"AttributeName": "briefDate", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "propertyId", "AttributeType": "S"},
            {"AttributeName": "briefDate", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@moto.mock_aws
def test_get_brief_by_date_returns_matching_brief(mock_brief_item: Dict[str, Any]) -> None:
    """A brief for the exact propertyId + briefDate composite key is returned."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = _create_briefs_table(dynamodb)
    table.put_item(Item=mock_brief_item)

    event = _make_event()
    params = {"propertyId": "ALOHA-CHI-001", "briefDate": "2026-08-03"}

    with patch("handlers.briefs_handler._dynamodb_resource", dynamodb), \
         patch("handlers.briefs_handler.BRIEFS_TABLE_NAME", "stayos-briefs-test"):
        result = get_brief_by_date(event, params)

    assert result["propertyId"] == "ALOHA-CHI-001"
    assert result["briefDate"] == "2026-08-03"


@moto.mock_aws
def test_get_brief_by_date_attaches_audio_url(mock_brief_item: Dict[str, Any]) -> None:
    """A stored s3Key plus a configured CloudFront domain yields an audioUrl."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = _create_briefs_table(dynamodb)
    table.put_item(Item=mock_brief_item)

    event = _make_event()
    params = {"propertyId": "ALOHA-CHI-001", "briefDate": "2026-08-03"}

    with patch("handlers.briefs_handler._dynamodb_resource", dynamodb), \
         patch("handlers.briefs_handler.BRIEFS_TABLE_NAME", "stayos-briefs-test"), \
         patch("handlers.briefs_handler.AUDIO_CLOUDFRONT_DOMAIN", "d123.cloudfront.net"):
        result = get_brief_by_date(event, params)

    assert result["audioBrief"]["audioUrl"] == (
        "https://d123.cloudfront.net/"
        "briefs/2026/08/03/ALOHA-CHI-001/morning-brief.mp3"
    )


@moto.mock_aws
def test_get_brief_by_date_404_when_absent() -> None:
    """A valid property/date with no stored brief raises BriefNotFoundException."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    _create_briefs_table(dynamodb)

    event = _make_event(property_id="ALOHA-XYZ-999", user_property_id="ALOHA-XYZ-999")
    params = {"propertyId": "ALOHA-XYZ-999", "briefDate": "2026-08-03"}

    with patch("handlers.briefs_handler._dynamodb_resource", dynamodb), \
         patch("handlers.briefs_handler.BRIEFS_TABLE_NAME", "stayos-briefs-test"):
        with pytest.raises(BriefNotFoundException) as exc_info:
            get_brief_by_date(event, params)

    assert exc_info.value.status_code == 404


@moto.mock_aws
def test_get_brief_by_date_rejects_invalid_date_format() -> None:
    """A malformed briefDate is rejected with a ValidationError."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    _create_briefs_table(dynamodb)

    event = _make_event()
    params = {"propertyId": "ALOHA-CHI-001", "briefDate": "08-03-2026"}

    with patch("handlers.briefs_handler._dynamodb_resource", dynamodb), \
         patch("handlers.briefs_handler.BRIEFS_TABLE_NAME", "stayos-briefs-test"):
        with pytest.raises(ValidationError) as exc_info:
            get_brief_by_date(event, params)

    assert exc_info.value.field == "briefDate"


@moto.mock_aws
def test_get_brief_by_date_forbidden_on_ownership_mismatch() -> None:
    """Requesting another property's dated brief raises ForbiddenError."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    _create_briefs_table(dynamodb)

    event = _make_event(property_id="ALOHA-NYC-001", user_property_id="ALOHA-CHI-001")
    params = {"propertyId": "ALOHA-NYC-001", "briefDate": "2026-08-03"}

    with patch("handlers.briefs_handler._dynamodb_resource", dynamodb), \
         patch("handlers.briefs_handler.BRIEFS_TABLE_NAME", "stayos-briefs-test"):
        with pytest.raises(ForbiddenError) as exc_info:
            get_brief_by_date(event, params)

    assert exc_info.value.status_code == 403
