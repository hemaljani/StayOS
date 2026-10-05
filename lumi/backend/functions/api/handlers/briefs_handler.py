"""LUMI API handler for brief retrieval.

Handles GET /briefs/{propertyId} - returns the most recent daily brief
for a property from DynamoDB. Handles GET /briefs/{propertyId}/{briefDate} -
returns a specific dated brief. Validates property ownership against the
requesting GM's Cognito claims.
"""

import os
import re
from typing import Any, Dict

import boto3
from aws_lambda_powertools import Logger, Tracer

from exceptions import BriefNotFoundException, ForbiddenError, ValidationError
from router import route

# Module-level clients for connection reuse per PYQUALITY-06
_dynamodb_resource = boto3.resource("dynamodb")

logger = Logger(service="stayos-api")

# Module-level tracer for X-Ray distributed tracing (REQ-TEL-3)
tracer = Tracer(service="stayos-api")

# Read table name from environment variable per PYQUALITY-06
BRIEFS_TABLE_NAME = os.environ.get("BRIEFS_TABLE_NAME", "")
AUDIO_CLOUDFRONT_DOMAIN = os.environ.get("AUDIO_CLOUDFRONT_DOMAIN", "")


def _validate_property_ownership(event: Dict[str, Any], property_id: str) -> None:
    """Validate that the requesting user owns the specified property.

    Args:
        event: API Gateway event with authorizer claims.
        property_id: The property ID from the request path.

    Raises:
        ValidationError: When propertyId format is invalid.
        ForbiddenError: When the requesting user does not own the property.
    """
    # Validate propertyId format (REQ-CR-10)
    if not re.match(r'^ALOHA-[A-Z]{3}-\d{3}$', property_id):
        raise ValidationError(
            message="Invalid property ID format. Expected: ALOHA-XXX-NNN",
            field="propertyId",
        )

    claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
    user_property_id = claims.get("custom:propertyId", "")

    if not user_property_id:
        logger.warning("Missing propertyId claim in token", extra={"requested_property": property_id})
        raise ForbiddenError("No propertyId claim found in token")

    if user_property_id != property_id:
        logger.warning(
            "Property ownership mismatch",
            extra={
                "requested_property": property_id,
                "user_property": user_property_id,
            },
        )
        raise ForbiddenError("You do not have access to this property's brief")


def _attach_audio_url(brief: Dict[str, Any]) -> None:
    """Construct and attach the CloudFront audio URL to a brief record.

    Mutates the brief dict in place, adding an audioUrl field to the
    audioBrief object if an s3Key and CloudFront domain are available.

    Args:
        brief: Full brief record from DynamoDB.
    """
    audio_brief = brief.get("audioBrief", {})
    if audio_brief.get("s3Key") and AUDIO_CLOUDFRONT_DOMAIN:
        audio_brief["audioUrl"] = (
            f"https://{AUDIO_CLOUDFRONT_DOMAIN}/{audio_brief['s3Key']}"
        )


@tracer.capture_method
@route("GET", "/briefs/{propertyId}/{briefDate}")
def get_brief_by_date(event: Dict[str, Any], params: Dict[str, str]) -> Dict[str, Any]:
    """Retrieve a specific daily brief for a property and date.

    Fetches the brief using the composite key (propertyId + briefDate).
    Used by the frontend to display full past brief detail views.

    Args:
        event: API Gateway Lambda proxy event with authorizer claims.
        params: Path parameters dict containing propertyId and briefDate.

    Returns:
        Full brief response for the specified date.

    Raises:
        BriefNotFoundException: When no brief exists for the property/date.
        ForbiddenError: When the requesting GM does not own the property.
        ValidationError: When propertyId or briefDate format is invalid.
    """
    property_id = params["propertyId"]
    brief_date = params["briefDate"]

    _validate_property_ownership(event, property_id)

    # Validate briefDate format (YYYY-MM-DD)
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', brief_date):
        raise ValidationError(
            message="Invalid date format. Expected: YYYY-MM-DD",
            field="briefDate",
        )

    # Add searchable annotations for X-Ray trace filtering
    tracer.put_annotation("propertyId", property_id)
    tracer.put_annotation("briefDate", brief_date)

    # Fetch the specific brief by composite key (PK + SK)
    table = _dynamodb_resource.Table(BRIEFS_TABLE_NAME)
    response = table.get_item(
        Key={
            "propertyId": property_id,
            "briefDate": brief_date,
        }
    )

    item = response.get("Item")
    if not item:
        raise BriefNotFoundException(property_id)

    _attach_audio_url(item)

    logger.info(
        "Dated brief retrieved successfully",
        extra={
            "property_id": property_id,
            "brief_date": brief_date,
        },
    )

    return item


@tracer.capture_method
@route("GET", "/briefs/{propertyId}")
def get_brief(event: Dict[str, Any], params: Dict[str, str]) -> Dict[str, Any]:
    """Retrieve the most recent daily brief for a property.

    Queries DynamoDB with propertyId as partition key, returning the latest
    brief by sorting on briefDate descending. Constructs the audio URL
    using the CloudFront domain from environment variables.

    Args:
        event: API Gateway Lambda proxy event with authorizer claims.
        params: Path parameters dict containing propertyId.

    Returns:
        Full brief response matching the API schema (property, dailyKPIs,
        actionItems, vipArrivals, audioBrief).

    Raises:
        BriefNotFoundException: When no brief exists for the property.
        ForbiddenError: When the requesting GM does not own the property.
    """
    property_id = params["propertyId"]

    _validate_property_ownership(event, property_id)

    # Add searchable annotation for X-Ray trace filtering
    tracer.put_annotation("propertyId", property_id)

    # Query DynamoDB for the latest brief (sort key descending, limit 1)
    table = _dynamodb_resource.Table(BRIEFS_TABLE_NAME)
    response = table.query(
        KeyConditionExpression="propertyId = :pid",
        ExpressionAttributeValues={":pid": property_id},
        ScanIndexForward=False,
        Limit=2,
    )

    items = response.get("Items", [])
    if not items:
        raise BriefNotFoundException(property_id)

    brief = items[0]
    _attach_audio_url(brief)

    logger.info(
        "Brief retrieved successfully",
        extra={
            "property_id": property_id,
            "brief_date": brief.get("briefDate"),
        },
    )

    return brief
