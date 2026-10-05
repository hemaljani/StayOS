"""LUMI Seed Data - GM-to-property mapping and provisioning functions.

Contains the 5 GM seed data records and functions to provision Cognito
users, seed DynamoDB settings, and create per-GM EventBridge Scheduler
schedules for the LUMI pilot deployment.
Supports REQ-28 (GM and Property Seed Data) and REQ-SCHED-5 (Seed Schedules).
"""

import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import boto3
from aws_lambda_powertools import Logger

from schedule_manager import schedule_exists, upsert_gm_schedule

# Module-level clients initialized outside functions for connection reuse
# across Lambda invocations per PYQUALITY-06
_cognito_client = boto3.client("cognito-idp")
_dynamodb_resource = boto3.resource("dynamodb")

# Module-level logger
logger = Logger(service="stayos-seed-data")

# Demo password must be provided via environment variable at deploy time.
# CloudFormation passes this from the AppPassword parameter (NoEcho: true).
# Read lazily (not at module import) so importing this module - e.g. during
# test collection - does not require DEMO_PASSWORD to be set (review finding
# Env-1). The deployed Lambda still gets it from the CloudFormation-set env var
# at the point it is actually needed.
DEMO_PASSWORD_ENV = "DEMO_PASSWORD"


def _get_demo_password() -> str:
    """Return the demo GM password from the environment.

    Returns:
        The demo password used to create the pilot GM Cognito users.

    Raises:
        KeyError: If ``DEMO_PASSWORD`` is not set at the point of use (a deploy
            misconfiguration), surfaced loudly here rather than at import time.
    """
    try:
        return os.environ[DEMO_PASSWORD_ENV]
    except KeyError as exc:
        raise KeyError(
            f"{DEMO_PASSWORD_ENV} environment variable is not set; "
            "CloudFormation must pass the AppPassword parameter to this function"
        ) from exc

# Complete GM-to-property mapping for 5 pilot properties across 4 regions.
# Each entry contains all fields needed for Cognito user creation and
# DynamoDB settings seeding. Data sourced from design doc section 8.1.
GM_SEED_DATA: List[Dict[str, Any]] = [
    # === United States (2 properties) ===
    {
        "gmAlias": "jsmith",
        "gmName": "Jennifer Smith",
        "email": "jsmith@aloha.com",
        "propertyId": "ALOHA-CHI-001",
        "propertyName": "Aloha Grand Chicago",
        "brand": "Aloha Grand",
        "city": "Chicago, IL",
        "timezone": "America/Chicago",
        "totalRooms": 368,
        "language": "en-US",
    },
    {
        "gmAlias": "mrodriguez",
        "gmName": "Miguel Rodriguez",
        "email": "mrodriguez@aloha.com",
        "propertyId": "ALOHA-MIA-001",
        "propertyName": "Aloha Resort & Spa Miami",
        "brand": "Aloha Resort & Spa",
        "city": "Miami, FL",
        "timezone": "America/New_York",
        "totalRooms": 425,
        "language": "en-US",
    },
    # === Japan (1 property) ===
    {
        "gmAlias": "ttanaka",
        "gmName": "Takeshi Tanaka",
        "email": "ttanaka@aloha.com",
        "propertyId": "ALOHA-TYO-001",
        "propertyName": "Aloha Grand Tokyo",
        "brand": "Aloha Grand",
        "city": "Tokyo",
        "timezone": "Asia/Tokyo",
        "totalRooms": 480,
        "language": "ja-JP",
    },
    # === Europe (1 property) ===
    {
        "gmAlias": "cgarcia",
        "gmName": "Carlos Garcia",
        "email": "cgarcia@aloha.com",
        "propertyId": "ALOHA-MAD-001",
        "propertyName": "Aloha Resort & Spa Madrid",
        "brand": "Aloha Resort & Spa",
        "city": "Madrid, Spain",
        "timezone": "Europe/Madrid",
        "totalRooms": 380,
        "language": "es-ES",
    },
    # === India (1 property) ===
    {
        "gmAlias": "pdesai",
        "gmName": "Priya Desai",
        "email": "pdesai@aloha.com",
        "propertyId": "ALOHA-BOM-001",
        "propertyName": "Aloha Resort & Spa Mumbai",
        "brand": "Aloha Resort & Spa",
        "city": "Mumbai",
        "timezone": "Asia/Kolkata",
        "totalRooms": 355,
        "language": "en-US",
    },
]


def provision_cognito_users(
    user_pool_id: str,
    gm_list: List[Dict[str, Any]],
) -> int:
    """Create Cognito user accounts for each GM in the seed data list.

    Uses AdminCreateUser with SUPPRESS message action (no welcome email
    for demo accounts) followed by AdminSetUserPassword to set a permanent
    password. Handles UsernameExistsException gracefully for idempotency.

    Args:
        user_pool_id: The Cognito User Pool ID to create users in.
        gm_list: List of GM dictionaries containing email, propertyId,
            and gmAlias fields.

    Returns:
        Number of users successfully created (excludes already-existing users).

    Raises:
        botocore.exceptions.ClientError: For non-recoverable Cognito errors
            (e.g., UserPoolNotFoundException, invalid parameters).
    """
    users_created = 0

    for gm in gm_list:
        email = gm["email"]
        gm_alias = gm["gmAlias"]
        property_id = gm["propertyId"]
        # Full display name (e.g. "Jennifer Smith"). Written to the standard
        # Cognito "name" attribute so it rides in the idToken and the shell can
        # greet the GM by name instead of email. Falls back to the alias.
        gm_name = gm.get("gmName", gm_alias)

        try:
            # Create the user with custom attributes and suppressed welcome email
            _cognito_client.admin_create_user(
                UserPoolId=user_pool_id,
                Username=email,
                UserAttributes=[
                    {"Name": "email", "Value": email},
                    {"Name": "email_verified", "Value": "true"},
                    # Standard "name" claim - surfaces in the idToken for display.
                    {"Name": "name", "Value": gm_name},
                    {"Name": "custom:propertyId", "Value": property_id},
                    {"Name": "custom:gmAlias", "Value": gm_alias},
                ],
                MessageAction="SUPPRESS",
            )

            # Set permanent password (bypasses force-change-on-first-login for demo)
            _cognito_client.admin_set_user_password(
                UserPoolId=user_pool_id,
                Username=email,
                Password=_get_demo_password(),
                Permanent=True,
            )

            users_created += 1
            logger.info(
                "Created Cognito user",
                extra={"gm_alias": gm_alias, "property_id": property_id},
            )

        except _cognito_client.exceptions.UsernameExistsException:
            # User already exists - skip on Update events for idempotency
            logger.info(
                "User already exists, skipping",
                extra={"gm_alias": gm_alias, "email": email},
            )

    logger.info(
        "Cognito user provisioning complete",
        extra={"users_created": users_created, "total_gms": len(gm_list)},
    )
    return users_created


def seed_settings_table(
    table_name: str,
    gm_list: List[Dict[str, Any]],
) -> int:
    """Seed default settings records in DynamoDB for each GM.

    Writes initial settings with default values for brief delivery time,
    alert toggles, KPI thresholds, and audio preferences. Uses a condition
    expression to avoid overwriting existing settings (idempotent on re-run).

    Args:
        table_name: Name of the DynamoDB settings table (stayos-settings).
        gm_list: List of GM dictionaries containing gmAlias, propertyId,
            and language fields.

    Returns:
        Number of settings records successfully created (excludes existing).

    Raises:
        botocore.exceptions.ClientError: For non-recoverable DynamoDB errors
            (e.g., table not found, insufficient permissions).
    """
    table = _dynamodb_resource.Table(table_name)
    records_seeded = 0
    now_iso = datetime.now(tz=timezone.utc).isoformat()

    for gm in gm_list:
        gm_alias = gm["gmAlias"]
        property_id = gm["propertyId"]
        language = gm["language"]

        # Default settings record matching design doc section 8.1 schema
        settings_item = {
            "gmAlias": gm_alias,
            "propertyId": property_id,
            "propertyName": gm["propertyName"],
            "gmName": gm["gmName"],
            "timezone": gm["timezone"],
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
                "language": language,
                "briefLength": "standard",
            },
            "createdAt": now_iso,
            "updatedAt": now_iso,
        }

        try:
            # ConditionExpression ensures we don't overwrite existing settings
            # on stack Update events (preserves GM customizations)
            table.put_item(
                Item=settings_item,
                ConditionExpression="attribute_not_exists(gmAlias)",
            )
            records_seeded += 1
            logger.info(
                "Seeded settings record",
                extra={"gm_alias": gm_alias, "property_id": property_id},
            )

        except table.meta.client.exceptions.ConditionalCheckFailedException:
            # Record already exists - skip to preserve existing GM preferences
            logger.info(
                "Settings record already exists, skipping",
                extra={"gm_alias": gm_alias},
            )

    logger.info(
        "DynamoDB settings seeding complete",
        extra={"records_seeded": records_seeded, "total_gms": len(gm_list)},
    )
    return records_seeded


def provision_schedules(gm_list: List[Dict[str, Any]]) -> int:
    """Create per-GM EventBridge Scheduler schedules for all pilot GMs.

    Idempotent: checks if a schedule already exists before creating.
    If a GM has customized their delivery time (schedule exists), the
    existing schedule is preserved. Only missing schedules are created
    with the default 06:30 in the GM's property timezone.

    Satisfies REQ-SCHED-5.

    Args:
        gm_list: List of GM dictionaries containing gmAlias, propertyId,
            and timezone fields.

    Returns:
        Number of schedules successfully created (excludes existing).

    Raises:
        botocore.exceptions.ClientError: For non-recoverable scheduler errors
            (e.g., insufficient permissions, invalid schedule group).
    """
    schedules_created = 0

    for gm in gm_list:
        gm_alias = gm["gmAlias"]
        property_id = gm["propertyId"]
        timezone_str = gm["timezone"]

        # Check if schedule already exists - don't overwrite GM-customized times
        if schedule_exists(gm_alias):
            logger.info(
                "Schedule already exists, skipping",
                extra={"gm_alias": gm_alias},
            )
            continue

        try:
            upsert_gm_schedule(
                gm_alias=gm_alias,
                property_id=property_id,
                delivery_time="06:30",
                timezone_str=timezone_str,
            )
            schedules_created += 1
            logger.info(
                "Schedule created for GM",
                extra={
                    "gm_alias": gm_alias,
                    "property_id": property_id,
                    "timezone": timezone_str,
                },
            )
        except Exception as error:
            # Log but continue - don't fail entire seed for one schedule
            logger.error(
                "Failed to create schedule for GM",
                extra={
                    "gm_alias": gm_alias,
                    "error": str(error),
                },
            )

    logger.info(
        "Schedule provisioning complete",
        extra={"schedules_created": schedules_created, "total_gms": len(gm_list)},
    )
    return schedules_created
