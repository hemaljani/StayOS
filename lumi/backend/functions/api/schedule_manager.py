"""LUMI Schedule Manager - creates/updates per-GM EventBridge Scheduler schedules.

Provides a shared interface for managing per-GM brief delivery schedules.
Used by the settings handler (on delivery time change) and the seed-data
Lambda (on initial provisioning). Each GM gets a dedicated EventBridge
Scheduler schedule that fires at their configured local time.

Satisfies REQ-SCHED-1 (Individual EventBridge Scheduler per GM) and
REQ-SCHED-2 (Schedule Lifecycle on Settings Change).
"""

import json
import os
from typing import Any, Dict

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config

logger = Logger(service="stayos-api")

# Lazy-initialized scheduler client for connection reuse across invocations.
# Lazy initialization avoids credential lookups at import time, which
# breaks unit tests where moto does not support EventBridge Scheduler.
_scheduler_config = Config(
    retries={"total_max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=10,
)
_scheduler_client = None


def _get_scheduler_client() -> Any:
    """Return the scheduler client, creating it on first call."""
    global _scheduler_client
    if _scheduler_client is None:
        _scheduler_client = boto3.client("scheduler", config=_scheduler_config)
    return _scheduler_client

# Configuration from environment variables per PYQUALITY-06
SCHEDULE_GROUP_NAME = os.environ.get("SCHEDULE_GROUP_NAME", "stayos-briefs")
SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN", "")
ORCHESTRATOR_ARN = os.environ.get("ORCHESTRATOR_ARN", "")
STACK_PREFIX = os.environ.get("STACK_PREFIX", "lumi")


def upsert_gm_schedule(
    gm_alias: str,
    property_id: str,
    delivery_time: str,
    timezone_str: str,
) -> str:
    """Create or update an EventBridge Scheduler schedule for a GM.

    Uses create-first pattern: attempts CreateSchedule, falls back to
    UpdateSchedule if the schedule already exists (ConflictException).
    This aligns with least-privilege IAM - the seed-data Lambda (which
    only ever creates new schedules) needs only scheduler:CreateSchedule,
    while the API Lambda (which updates existing schedules on delivery
    time changes) additionally holds scheduler:UpdateSchedule. It remains
    idempotent.

    EventBridge Scheduler's native timezone support handles DST
    transitions automatically - no manual UTC offset calculation needed.

    Args:
        gm_alias: The GM's unique alias (e.g., "jsmith").
        property_id: The property identifier (e.g., "ALOHA-CHI-001").
        delivery_time: Preferred delivery time in HH:MM format (e.g., "06:30").
        timezone_str: IANA timezone string (e.g., "America/Chicago").

    Returns:
        The schedule name that was created or updated.

    Raises:
        botocore.exceptions.ClientError: For non-recoverable scheduler errors
            (e.g., invalid cron expression, missing permissions).
    """
    schedule_name = f"{STACK_PREFIX}-brief-{gm_alias}"

    # Parse HH:MM into cron components
    hour, minute = delivery_time.split(":")

    # EventBridge Scheduler cron format: minutes hours day-of-month month day-of-week year
    cron_expression = f"cron({minute} {hour} * * ? *)"

    # Payload identifies which GM's brief to generate
    payload = json.dumps({
        "source": "scheduler",
        "action": "generate-single",
        "gmAlias": gm_alias,
        "propertyId": property_id,
    })

    schedule_params: Dict[str, Any] = {
        "Name": schedule_name,
        "GroupName": SCHEDULE_GROUP_NAME,
        "ScheduleExpression": cron_expression,
        "ScheduleExpressionTimezone": timezone_str,
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": {
            "Arn": ORCHESTRATOR_ARN,
            "RoleArn": SCHEDULER_ROLE_ARN,
            "Input": payload,
        },
        "State": "ENABLED",
        "Description": f"Daily brief for {gm_alias} at {delivery_time} {timezone_str}",
    }

    try:
        # Create-first: seed-data provisioning creates net-new schedules and
        # only needs scheduler:CreateSchedule permission on this path.
        _get_scheduler_client().create_schedule(**schedule_params)
        logger.info(
            "Schedule created",
            extra={
                "schedule_name": schedule_name,
                "delivery_time": delivery_time,
                "timezone": timezone_str,
            },
        )
    except _get_scheduler_client().exceptions.ConflictException:
        # Schedule already exists (e.g. GM changing delivery time) - update it.
        # Requires scheduler:UpdateSchedule (held by the API Lambda role).
        _get_scheduler_client().update_schedule(**schedule_params)
        logger.info(
            "Schedule updated",
            extra={
                "schedule_name": schedule_name,
                "delivery_time": delivery_time,
                "timezone": timezone_str,
            },
        )

    return schedule_name


def schedule_exists(gm_alias: str) -> bool:
    """Check if an EventBridge Scheduler schedule exists for a GM.

    Used by the seed-data Lambda to avoid overwriting GM-customized
    delivery times on stack Update events.

    Args:
        gm_alias: The GM's unique alias.

    Returns:
        True if the schedule exists, False otherwise.
    """
    schedule_name = f"{STACK_PREFIX}-brief-{gm_alias}"

    try:
        _get_scheduler_client().get_schedule(
            Name=schedule_name,
            GroupName=SCHEDULE_GROUP_NAME,
        )
        return True
    except _get_scheduler_client().exceptions.ResourceNotFoundException:
        return False
