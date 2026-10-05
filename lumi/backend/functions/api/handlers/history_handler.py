"""LUMI API handler for brief history retrieval.

Handles GET /briefs/history - returns historical brief summaries for the
authenticated GM's property over a configurable lookback window (1-30 days).
The property ID is extracted from Cognito JWT claims, not from the URL path.
"""

import os
import re
from datetime import date, timedelta, timezone
from typing import Any, Dict, List

import boto3
from aws_lambda_powertools import Logger, Tracer
from botocore.exceptions import ClientError

from exceptions import ForbiddenError, InternalError, ValidationError
from router import route

# Module-level clients for connection reuse across Lambda invocations (PYQUALITY-06)
_dynamodb_resource = boto3.resource("dynamodb")

logger = Logger(service="stayos-api")
tracer = Tracer(service="stayos-api")

# Read table name from environment variable - never hardcode resource identifiers
BRIEFS_TABLE_NAME = os.environ.get("BRIEFS_TABLE_NAME", "")


def _compute_cutoff_date(days: int) -> str:
    """Compute the earliest date (inclusive) for the history lookback window.

    Calculates today minus (days - 1) to include today in the count.
    For example, days=7 returns 6 days ago (today counts as day 1).

    Args:
        days: Number of days to look back (including today).

    Returns:
        ISO 8601 date string (YYYY-MM-DD) representing the cutoff date.
    """
    cutoff = date.today() - timedelta(days=days - 1)
    return cutoff.isoformat()


def _build_history_summary(item: Dict[str, Any]) -> Dict[str, Any]:
    """Extract a lightweight summary from a full DynamoDB brief item.

    Projects only the fields needed for the trend chart and past briefs
    list: briefDate, status, three KPI current values, and a truncated
    narrative preview.

    Args:
        item: Full DynamoDB brief item containing all brief fields.

    Returns:
        Summary dict with briefDate, status, dailyKPIs (occupancy.current,
        adr.current, revPAR.current), and narrativePreview.
    """
    daily_kpis = item.get("dailyKPIs", {})

    # Extract only the current values for the three revenue metrics
    occupancy_current = daily_kpis.get("occupancy", {}).get("current", 0)
    adr_current = daily_kpis.get("adr", {}).get("current", 0)
    revpar_current = daily_kpis.get("revPAR", {}).get("current", 0)

    # Truncate narrative to 100 chars for preview display
    narrative = item.get("narrative", "")
    if narrative and len(narrative) > 100:
        narrative_preview = narrative[:100] + "..."
    else:
        narrative_preview = narrative if narrative else ""

    return {
        "briefDate": item.get("briefDate", ""),
        "status": item.get("status", ""),
        "dailyKPIs": {
            "occupancy": {"current": occupancy_current},
            "adr": {"current": adr_current},
            "revPAR": {"current": revpar_current},
        },
        "narrativePreview": narrative_preview,
    }


@tracer.capture_method
@route("GET", "/briefs/history")
def get_brief_history(event: Dict[str, Any], params: Dict[str, str]) -> List[Dict[str, Any]]:
    """Retrieve historical brief summaries for the authenticated GM's property.

    Extracts the property ID from Cognito JWT claims and queries DynamoDB
    for briefs within the specified lookback window. Returns summaries
    ordered oldest-first for chronological chart rendering.

    Args:
        event: API Gateway Lambda proxy event with authorizer claims.
        params: Path parameters dict (empty for this route).

    Returns:
        List of brief summary dicts ordered oldest-first by briefDate.

    Raises:
        ForbiddenError: When no property is assigned to the user's account
            or the property ID format is invalid.
        ValidationError: When the days query parameter is not a valid integer
            between 1 and 30.
        InternalError: When DynamoDB query fails unexpectedly.
    """
    # Extract propertyId from Cognito JWT claims (not from URL path)
    claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
    property_id = claims.get("custom:propertyId", "")

    if not property_id:
        raise ForbiddenError("No property assigned to your account")

    tracer.put_annotation("propertyId", property_id)

    # Validate property ID format matches expected pattern
    if not re.match(r"^ALOHA-[A-Z]{3}-\d{3}$", property_id):
        raise ForbiddenError("No property assigned to your account")

    # Parse and validate the days query parameter (default 7)
    query_params = event.get("queryStringParameters") or {}
    days_str = query_params.get("days", "7")

    try:
        days = int(days_str)
    except (ValueError, TypeError):
        raise ValidationError(
            message="days must be an integer between 1 and 30",
            field="days",
        )

    if days < 1 or days > 30:
        raise ValidationError(
            message="days must be an integer between 1 and 30",
            field="days",
        )

    cutoff_date = _compute_cutoff_date(days)

    logger.info(
        "Querying brief history",
        extra={
            "property_id": property_id,
            "days": days,
            "cutoff_date": cutoff_date,
        },
    )

    try:
        # Query DynamoDB using composite key: propertyId (PK) + briefDate (SK)
        # ScanIndexForward=True returns items in ascending date order (oldest first)
        table = _dynamodb_resource.Table(BRIEFS_TABLE_NAME)
        response = table.query(
            KeyConditionExpression="propertyId = :pid AND briefDate >= :cutoff",
            ExpressionAttributeValues={
                ":pid": property_id,
                ":cutoff": cutoff_date,
            },
            ScanIndexForward=True,
        )
    except ClientError as exc:
        logger.error(
            "DynamoDB query failed for brief history",
            extra={
                "property_id": property_id,
                "error": str(exc),
            },
        )
        raise InternalError("Failed to retrieve brief history")

    items = response.get("Items", [])

    # Map full DynamoDB items to lightweight summary projections
    summaries = [_build_history_summary(item) for item in items]

    logger.info(
        "Brief history retrieved",
        extra={
            "property_id": property_id,
            "items_returned": len(summaries),
        },
    )

    return summaries
