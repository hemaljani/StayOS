"""LUMI API handler for GM settings management.

Handles GET /settings/{gmAlias} and PUT /settings/{gmAlias}.
Provides CRUD operations for GM preferences including delivery time,
alert toggles, KPI thresholds, and audio language configuration.
On delivery time change, syncs the per-GM EventBridge Scheduler schedule.
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict

import boto3
from aws_lambda_powertools import Logger, Tracer

from exceptions import ForbiddenError, SettingsNotFoundException, ValidationError
from router import route
from schedule_manager import upsert_gm_schedule
from validators.settings_validator import validate_settings

# Module-level clients for connection reuse per PYQUALITY-06
_dynamodb_resource = boto3.resource("dynamodb")

logger = Logger(service="stayos-api")

# Module-level tracer for X-Ray distributed tracing (REQ-TEL-3)
tracer = Tracer(service="stayos-api")

# Read table name from environment variable per PYQUALITY-06
SETTINGS_TABLE_NAME = os.environ.get("SETTINGS_TABLE_NAME", "")


def _validate_ownership(event: Dict[str, Any], gm_alias: str) -> None:
    """Validate that the requesting user owns the settings they are accessing.

    Compares the gmAlias from Cognito claims against the path parameter.
    GMs can only access their own settings.

    Args:
        event: API Gateway event with authorizer claims.
        gm_alias: The gmAlias from the request path.

    Raises:
        ValidationError: When gmAlias format is invalid.
        ForbiddenError: When the requesting user's gmAlias does not match.
    """
    # Validate gmAlias format (REQ-CR-10)
    if not re.match(r'^[a-z]{1,20}$', gm_alias):
        raise ValidationError(
            message="Invalid GM alias format. Expected: lowercase letters, 1-20 chars",
            field="gmAlias",
        )

    claims = event.get("requestContext", {}).get("authorizer", {}).get("claims", {})
    user_gm_alias = claims.get("custom:gmAlias", "")

    if not user_gm_alias:
        logger.warning("Missing gmAlias claim in token", extra={"requested_alias": gm_alias})
        raise ForbiddenError("No gmAlias claim found in token")

    if user_gm_alias != gm_alias:
        logger.warning(
            "Settings ownership mismatch",
            extra={
                "requested_alias": gm_alias,
                "user_alias": user_gm_alias,
            },
        )
        raise ForbiddenError("You can only access your own settings")


@tracer.capture_method
@route("GET", "/settings/{gmAlias}")
def get_settings(event: Dict[str, Any], params: Dict[str, str]) -> Dict[str, Any]:
    """Retrieve GM settings by alias.

    Reads the settings record from DynamoDB using gmAlias as the key.
    Validates that the requesting user owns the settings.

    Args:
        event: API Gateway Lambda proxy event with authorizer claims.
        params: Path parameters dict containing gmAlias.

    Returns:
        GM settings object with all preference fields.

    Raises:
        SettingsNotFoundException: When no settings exist for the alias.
        ForbiddenError: When the requesting user does not own the settings.
    """
    gm_alias = params["gmAlias"]
    _validate_ownership(event, gm_alias)

    # Add searchable annotation for X-Ray trace filtering
    tracer.put_annotation("gmAlias", gm_alias)

    table = _dynamodb_resource.Table(SETTINGS_TABLE_NAME)
    response = table.get_item(Key={"gmAlias": gm_alias})

    item = response.get("Item")
    if not item:
        raise SettingsNotFoundException(gm_alias)

    logger.info("Settings retrieved", extra={"gm_alias": gm_alias})
    return item


@tracer.capture_method
@route("PUT", "/settings/{gmAlias}")
def put_settings(event: Dict[str, Any], params: Dict[str, str]) -> Dict[str, Any]:
    """Update GM settings.

    Validates input, writes to DynamoDB with updated timestamp.
    Only updates provided fields - does not require full object replacement.
    If briefDeliveryTime changes, syncs the per-GM EventBridge Scheduler
    schedule to fire at the new time (best-effort - settings save is not
    blocked by schedule sync failures).

    Args:
        event: API Gateway Lambda proxy event with body and authorizer claims.
        params: Path parameters dict containing gmAlias.

    Returns:
        Updated GM settings object.

    Raises:
        ValidationError: When input fails validation rules.
        ForbiddenError: When the requesting user does not own the settings.
        SettingsNotFoundException: When no existing settings found to update.
    """
    gm_alias = params["gmAlias"]
    _validate_ownership(event, gm_alias)

    # Add searchable annotation for X-Ray trace filtering
    tracer.put_annotation("gmAlias", gm_alias)

    # Parse request body
    body_str = event.get("body", "{}")
    try:
        body = json.loads(body_str) if body_str else {}
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid JSON in request body: {exc}")

    # Validate input fields
    validation_errors = validate_settings(body)
    if validation_errors:
        # Return the first validation error
        first_error = validation_errors[0]
        raise ValidationError(
            message=first_error["message"],
            field=first_error.get("field"),
        )

    # Read existing settings to merge with updates
    table = _dynamodb_resource.Table(SETTINGS_TABLE_NAME)
    existing = table.get_item(Key={"gmAlias": gm_alias}).get("Item")

    if not existing:
        raise SettingsNotFoundException(gm_alias)

    # Merge updates into existing settings
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    if "briefDeliveryTime" in body:
        existing["briefDeliveryTime"] = body["briefDeliveryTime"]
    if "alertToggles" in body:
        existing["alertToggles"] = body["alertToggles"]
    if "kpiThresholds" in body:
        existing["kpiThresholds"] = body["kpiThresholds"]
    if "audioPreferences" in body:
        existing["audioPreferences"] = body["audioPreferences"]

    existing["updatedAt"] = now_iso

    # Write merged settings back to DynamoDB
    table.put_item(Item=existing)

    logger.info(
        "Settings updated",
        extra={"gm_alias": gm_alias, "updated_at": now_iso},
    )

    # Sync EventBridge Scheduler schedule if delivery time changed (REQ-SCHED-2)
    # Best-effort: settings save succeeds even if schedule sync fails
    if "briefDeliveryTime" in body:
        _sync_schedule(gm_alias, existing)

    return existing


def _sync_schedule(gm_alias: str, settings: Dict[str, Any]) -> None:
    """Sync the per-GM EventBridge Scheduler schedule after a delivery time change.

    Best-effort operation - failures are logged but do not block the
    settings save. A CloudWatch alarm on schedule sync errors alerts
    the team for manual remediation.

    Args:
        gm_alias: The GM's unique alias.
        settings: The complete merged settings record (post-update).
    """
    try:
        upsert_gm_schedule(
            gm_alias=gm_alias,
            property_id=settings.get("propertyId", ""),
            delivery_time=settings["briefDeliveryTime"],
            timezone_str=settings.get("timezone", "UTC"),
        )
        logger.info(
            "Schedule synced after delivery time change",
            extra={
                "gm_alias": gm_alias,
                "delivery_time": settings["briefDeliveryTime"],
                "timezone": settings.get("timezone", "UTC"),
            },
        )
    except Exception as error:
        # Non-blocking: settings are saved, schedule sync is best-effort
        logger.error(
            "Failed to sync schedule after delivery time change",
            extra={
                "gm_alias": gm_alias,
                "delivery_time": settings["briefDeliveryTime"],
                "error": str(error),
            },
        )
