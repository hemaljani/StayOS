"""LUMI Seed Data - CloudFormation custom resource handler.

Provisions 5 GM accounts in Cognito User Pool, seeds their default
settings in DynamoDB, creates per-GM EventBridge Scheduler schedules,
and populates 5 hotel operations dataset tables (rooms, guests, revenues,
reservations, work-orders) on stack creation. Idempotent on updates.
"""

import json
import urllib.request
from typing import Any, Dict, List

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config
from botocore.exceptions import ClientError

from seed_data import (
    GM_SEED_DATA,
    provision_cognito_users,
    provision_schedules,
    seed_settings_table,
)
from historical_briefs import seed_historical_briefs
from dataset_generator import (
    generate_rooms,
    generate_guests,
    generate_revenue,
    generate_reservations,
    generate_work_orders,
    reconcile_room_status,
    BatchWriter,
)

# Module-level logger configured before handler per PYQUALITY-03
logger = Logger(service="stayos-seed-data")

# Module-level DynamoDB resource for idempotency checks (connection reuse)
_dynamodb = boto3.resource(
    "dynamodb",
    config=Config(retries={"mode": "standard"}),
)

# CloudFormation custom resource response statuses
SUCCESS = "SUCCESS"
FAILED = "FAILED"

# Dataset table environment variable names (used in Step 5)
DATASET_TABLE_ENV_VARS: List[str] = [
    "ROOMS_TABLE_NAME",
    "GUESTS_TABLE_NAME",
    "REVENUES_TABLE_NAME",
    "RESERVATIONS_TABLE_NAME",
    "WORK_ORDERS_TABLE_NAME",
]

# Safety cap for _clear_tables to prevent Lambda timeout on unexpectedly large tables.
# Current dataset is ~24k items total across 5 tables; 50k per table is generous headroom.
MAX_ITEMS_PER_TABLE = 50000

# Number of items to read per DynamoDB Scan page during table clearing.
# Keeps individual scan calls bounded to avoid read-capacity spikes.
SCAN_PAGE_LIMIT = 500

# ---------------------------------------------------------------------------
# Destructive-operation guard (Requirement 8.1, 8.2)
# ---------------------------------------------------------------------------
# The bulk table-clear reseed (_clear_tables) is destructive. Requirement 8.1
# forbids the scheduled/automated roll-forward path from ever invoking it, and
# Requirement 8.2 requires the Force reseed to be reachable ONLY via explicit
# manual invocation AND explicit confirmation.
#
# Gating is therefore two-factor and fail-closed:
#   1. Force               - the event MUST carry Force == True.
#   2. ConfirmClear token  - the event MUST carry a confirmation token that
#                            EXACTLY matches CLEAR_CONFIRMATION_TOKEN.
#
# A scheduled EventBridge Scheduler trigger and the orchestrator's upsert-only
# roll-forward path send NEITHER field, so they can never satisfy the guard.
# CloudFormation Create/Update events likewise never send them, so a normal
# deploy is upsert-only. Only a deliberate, hand-crafted manual invocation
# (e.g. `aws lambda invoke` / `make reseed`) that sets BOTH fields clears data.
#
# The token is intentionally an explicit literal (not a random value): a demo
# operator must type it on purpose, which is the "explicit confirmation" the
# requirement calls for. It is NOT a secret and NOT read from the environment,
# so a misconfigured environment variable can never silently authorize a wipe.
CLEAR_CONFIRMATION_TOKEN = "CONFIRM-CLEAR-TABLES"

# Event field names carrying the two-factor destructive-op confirmation.
FORCE_FIELD = "Force"
CONFIRM_CLEAR_FIELD = "ConfirmClear"


def _is_clear_authorized(event: Dict[str, Any]) -> bool:
    """Decide whether a bulk table-clear is explicitly authorized.

    Implements the two-factor, fail-closed destructive-op guard (Requirements
    8.1, 8.2). A clear is authorized ONLY when the event carries both an
    explicit ``Force`` flag set to boolean ``True`` AND a ``ConfirmClear``
    token that exactly matches :data:`CLEAR_CONFIRMATION_TOKEN`.

    The scheduled roll-forward path and CloudFormation Create/Update events
    never send either field, so this function returns ``False`` for them and
    the seed path stays upsert-only. A ``Force`` flag without the matching
    confirmation token is rejected (returns ``False``) so a partial/accidental
    force can never wipe data.

    Args:
        event: The Lambda invocation event.

    Returns:
        ``True`` only when both factors are present and valid; ``False``
        otherwise.
    """
    force = event.get(FORCE_FIELD, False)
    confirm = event.get(CONFIRM_CLEAR_FIELD)

    # Factor 1: Force must be a real boolean True (not a truthy string), so an
    # accidental "Force": "false" or "Force": 0 cannot authorize a wipe.
    if force is not True:
        return False

    # Factor 2: the confirmation token must match exactly. A Force without a
    # valid confirmation token is a misuse - log it and refuse.
    if confirm != CLEAR_CONFIRMATION_TOKEN:
        logger.warning(
            "Force reseed requested WITHOUT valid confirmation token - refusing "
            "to clear tables (destructive-op guard, Requirement 8.2)",
            extra={
                "force": force,
                "confirmation_present": confirm is not None,
                "request_type": event.get("RequestType", "DirectInvoke"),
            },
        )
        return False

    logger.info(
        "Destructive clear authorized via explicit manual invocation with "
        "confirmation token (Requirement 8.2)",
        extra={"request_type": event.get("RequestType", "DirectInvoke")},
    )
    return True


def _tables_already_seeded(table_names: List[str]) -> bool:
    """Check if any of the dataset tables already contain data.

    Performs a Scan with Limit=1 on each table to detect existing items.
    If ANY table already has data, returns True to skip re-seeding.
    This ensures idempotent behavior on CloudFormation updates.

    Args:
        table_names: List of DynamoDB table name strings to check.

    Returns:
        True if any table already contains at least one item, False if all
        tables are empty and safe to seed.
    """
    for table_name in table_names:
        try:
            table = _dynamodb.Table(table_name)
            # Scan with Limit=1 is the cheapest way to check for existing data
            response = table.scan(Limit=1)
            item_count = len(response.get("Items", []))

            if item_count > 0:
                logger.info(
                    "Table already contains data, skipping dataset generation",
                    extra={"table_name": table_name, "items_found": item_count},
                )
                return True
        except ClientError as error:
            error_code = error.response["Error"]["Code"]
            logger.warning(
                "Failed to check table for existing data, will attempt seeding",
                extra={
                    "table_name": table_name,
                    "error_code": error_code,
                    "error_message": error.response["Error"]["Message"],
                },
            )
            # If we can't check, proceed with seeding (idempotent writes)
            return False

    return False


def _clear_tables(table_names: List[str]) -> int:
    """Delete all items from the specified DynamoDB tables.

    Used by the Force reseed path to clear stale data before regeneration.
    Scans each table in pages of SCAN_PAGE_LIMIT items and batch-deletes them.
    Stops after MAX_ITEMS_PER_TABLE deletions per table to prevent Lambda timeout.

    Args:
        table_names: List of DynamoDB table name strings to clear.

    Returns:
        Total number of items deleted across all tables.
    """
    total_deleted = 0
    dynamodb = boto3.resource("dynamodb")

    for table_name in table_names:
        table = dynamodb.Table(table_name)
        # Get the key schema to know which attributes to include in delete
        key_attrs = [k["AttributeName"] for k in table.key_schema]

        logger.info(
            "Clearing table for reseed",
            extra={"table_name": table_name, "key_attributes": key_attrs},
        )

        # Scan and delete in batches with a per-page limit to avoid
        # read-capacity spikes and a per-table cap to prevent timeout
        scan_kwargs: Dict[str, Any] = {
            "ProjectionExpression": ", ".join(key_attrs),
            "Limit": SCAN_PAGE_LIMIT,
        }
        deleted_count = 0
        cap_reached = False

        while True:
            response = table.scan(**scan_kwargs)
            items = response.get("Items", [])

            if not items:
                break

            with table.batch_writer() as batch:
                for item in items:
                    key = {k: item[k] for k in key_attrs}
                    batch.delete_item(Key=key)
                    deleted_count += 1

                    # Safety cap: stop deleting if we hit the per-table limit
                    if deleted_count >= MAX_ITEMS_PER_TABLE:
                        cap_reached = True
                        break

            if cap_reached:
                logger.warning(
                    "Safety cap reached - stopped clearing table to prevent "
                    "Lambda timeout. Remaining items were NOT deleted.",
                    extra={
                        "table_name": table_name,
                        "items_deleted": deleted_count,
                        "cap": MAX_ITEMS_PER_TABLE,
                    },
                )
                break

            # Continue scanning if more items exist
            if "LastEvaluatedKey" in response:
                scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
            else:
                break

        logger.info(
            "Table cleared",
            extra={
                "table_name": table_name,
                "items_deleted": deleted_count,
                "cap_reached": cap_reached,
            },
        )
        total_deleted += deleted_count

    return total_deleted


def send_cfn_response(
    event: Dict[str, Any],
    status: str,
    reason: str = "",
    physical_resource_id: str = "SeedDataResource",
) -> None:
    """Send response to CloudFormation via pre-signed S3 URL.

    CloudFormation custom resources require a response to be PUT to the
    ResponseURL provided in the event. This confirms whether the resource
    operation succeeded or failed.

    Args:
        event: CloudFormation custom resource event containing ResponseURL.
        status: Either SUCCESS or FAILED.
        reason: Human-readable reason for the status (required if FAILED).
        physical_resource_id: Unique identifier for the custom resource instance.

    Raises:
        urllib.error.URLError: If the response URL is unreachable.
    """
    response_body = json.dumps({
        "Status": status,
        "Reason": reason,
        "PhysicalResourceId": physical_resource_id,
        "StackId": event.get("StackId", ""),
        "RequestId": event.get("RequestId", ""),
        "LogicalResourceId": event.get("LogicalResourceId", ""),
        "Data": {},
    })

    logger.info(
        "Sending CloudFormation response",
        extra={
            "cfn_status": status,
            "request_type": event.get("RequestType", "Unknown"),
            "logical_resource_id": event.get("LogicalResourceId", ""),
        },
    )

    # Encode the response body for the HTTPS PUT request
    encoded_body = response_body.encode("utf-8")

    # Skip CFN response if no ResponseURL (direct invocation via make reseed)
    if "ResponseURL" not in event:
        logger.info(
            "No ResponseURL - direct invocation mode, skipping CFN response",
            extra={"cfn_status": status, "reason": reason},
        )
        return

    request = urllib.request.Request(
        url=event["ResponseURL"],
        data=encoded_body,
        method="PUT",
    )
    request.add_header("Content-Type", "")
    request.add_header("Content-Length", str(len(encoded_body)))

    # Send the response to CloudFormation's pre-signed S3 URL
    with urllib.request.urlopen(request) as response:
        logger.info(
            "CloudFormation response sent",
            extra={"http_status": response.status},
        )


@logger.inject_lambda_context
def lambda_handler(event: Dict[str, Any], context: Any) -> None:
    """CloudFormation custom resource handler for seed data provisioning.

    Handles Create, Update, and Delete request types from CloudFormation.
    On Create/Update: provisions Cognito users, seeds DynamoDB settings,
    creates per-GM EventBridge Scheduler schedules, seeds historical briefs,
    and populates the 5 hotel operations dataset tables.
    On Delete: no-op to preserve existing data.

    Execution order (Create/Update):
        1. Provision Cognito users for 5 GMs
        2. Seed default settings in DynamoDB
        3. Create per-GM EventBridge Scheduler schedules
        4. Seed historical brief data
        5. Seed hotel operations dataset (rooms -> guests -> revenue ->
           reservations -> work-orders -> status reconciliation)

    Args:
        event: CloudFormation custom resource event with RequestType,
            ResponseURL, StackId, RequestId, and LogicalResourceId.
        context: Lambda context object (runtime metadata).

    Returns:
        None. Response is sent directly to CloudFormation via ResponseURL.
    """
    import os

    request_type = event.get("RequestType", "Unknown")
    logger.info(
        "Processing CloudFormation custom resource event",
        extra={
            "request_type": request_type,
            "stack_id": event.get("StackId", ""),
            "logical_resource_id": event.get("LogicalResourceId", ""),
        },
    )

    try:
        if request_type in ("Create", "Update"):
            # Read resource identifiers from environment variables per PYQUALITY-06
            user_pool_id = os.environ["COGNITO_USER_POOL_ID"]
            settings_table_name = os.environ["SETTINGS_TABLE_NAME"]

            logger.info(
                "Starting seed data provisioning",
                extra={
                    "user_pool_id": user_pool_id,
                    "settings_table": settings_table_name,
                    "gm_count": len(GM_SEED_DATA),
                },
            )

            # Step 1: Provision Cognito users for all 5 GMs
            users_created = provision_cognito_users(
                user_pool_id=user_pool_id,
                gm_list=GM_SEED_DATA,
            )

            # Step 2: Seed default settings in DynamoDB for each GM
            settings_seeded = seed_settings_table(
                table_name=settings_table_name,
                gm_list=GM_SEED_DATA,
            )

            # Step 3: Create per-GM EventBridge Scheduler schedules (REQ-SCHED-5)
            schedules_created = provision_schedules(gm_list=GM_SEED_DATA)

            # Step 4: Seed historical brief data (REQ-HIST-4)
            # Wrapped in its own try/except to ensure failure does not block
            # the CloudFormation deployment - graceful degradation
            briefs_seeded = 0
            try:
                briefs_table_name = os.environ.get("BRIEFS_TABLE_NAME", "")
                if briefs_table_name:
                    briefs_seeded = seed_historical_briefs(
                        table_name=briefs_table_name,
                        gm_list=GM_SEED_DATA,
                        days=7,
                    )
                else:
                    logger.warning(
                        "BRIEFS_TABLE_NAME not configured, skipping historical brief seeding"
                    )
            except Exception as exc:
                logger.error(
                    "Historical brief seeding failed - continuing",
                    extra={"error": str(exc), "error_type": type(exc).__name__},
                    exc_info=True,
                )

            # Step 5: Seed hotel operations dataset (REQ-DS-8)
            # Wrapped in try/except for graceful degradation - dataset failure
            # should not block the CloudFormation deployment
            dataset_counts: Dict[str, int] = {}
            try:
                # Read dataset table names from environment variables
                rooms_table = os.environ["ROOMS_TABLE_NAME"]
                guests_table = os.environ["GUESTS_TABLE_NAME"]
                revenues_table = os.environ["REVENUES_TABLE_NAME"]
                reservations_table = os.environ["RESERVATIONS_TABLE_NAME"]
                work_orders_table = os.environ["WORK_ORDERS_TABLE_NAME"]

                dataset_table_names = [
                    rooms_table,
                    guests_table,
                    revenues_table,
                    reservations_table,
                    work_orders_table,
                ]

                # Idempotency check: skip if any table already has data
                # (unless an authorized manual Force reseed was requested).
                # The destructive clear is two-factor gated (Requirement 8.2):
                # it requires BOTH Force=True AND a matching ConfirmClear token.
                # The scheduled roll-forward path and CFN deploys send neither,
                # so they are always upsert-only (Requirement 8.1).
                force_reseed = _is_clear_authorized(event)
                if force_reseed:
                    logger.info(
                        "Authorized Force reseed - clearing existing dataset tables"
                    )
                    cleared = _clear_tables(dataset_table_names)
                    logger.info(
                        "Tables cleared for reseed",
                        extra={"items_deleted": cleared},
                    )

                if not force_reseed and _tables_already_seeded(dataset_table_names):
                    logger.info(
                        "Dataset tables already contain data, skipping generation"
                    )
                else:
                    logger.info(
                        "Starting hotel operations dataset generation",
                        extra={
                            "rooms_table": rooms_table,
                            "guests_table": guests_table,
                            "revenues_table": revenues_table,
                            "reservations_table": reservations_table,
                            "work_orders_table": work_orders_table,
                        },
                    )

                    # Create BatchWriter instances for each table
                    rooms_writer = BatchWriter(table_name=rooms_table)
                    guests_writer = BatchWriter(table_name=guests_table)
                    revenue_writer = BatchWriter(table_name=revenues_table)
                    reservations_writer = BatchWriter(
                        table_name=reservations_table
                    )
                    work_orders_writer = BatchWriter(
                        table_name=work_orders_table
                    )

                    # Execute generators in dependency order:
                    # rooms/guests/revenue have no inter-dependencies,
                    # reservations depends on all three,
                    # work-orders depends on rooms,
                    # reconciliation depends on reservations + work-orders
                    rooms_lookup = generate_rooms(rooms_writer)
                    guests_lookup = generate_guests(guests_writer)
                    revenue_lookup = generate_revenue(revenue_writer)

                    reservations = generate_reservations(
                        writer=reservations_writer,
                        rooms_lookup=rooms_lookup,
                        guests_lookup=guests_lookup,
                        revenue_lookup=revenue_lookup,
                    )

                    work_orders = generate_work_orders(
                        writer=work_orders_writer,
                        rooms_lookup=rooms_lookup,
                    )

                    # Reconcile room statuses based on today's reservations
                    # and active work orders
                    reconcile_result = reconcile_room_status(
                        reservations=reservations,
                        work_orders=work_orders,
                        rooms_lookup=rooms_lookup,
                        table_name=rooms_table,
                    )

                    # Collect per-table item counts for reporting
                    dataset_counts = {
                        "rooms": rooms_writer.success_count,
                        "guests": guests_writer.success_count,
                        "revenues": revenue_writer.success_count,
                        "reservations": reservations_writer.success_count,
                        "work_orders": work_orders_writer.success_count,
                    }

                    logger.info(
                        "Hotel operations dataset generation complete",
                        extra={
                            "dataset_counts": dataset_counts,
                            "reconcile_result": reconcile_result,
                        },
                    )

            except KeyError as exc:
                logger.error(
                    "Dataset generation skipped - missing environment variable",
                    extra={
                        "missing_key": str(exc),
                        "error_type": "KeyError",
                    },
                )
            except Exception as exc:
                logger.error(
                    "Hotel operations dataset generation failed - continuing",
                    extra={"error": str(exc), "error_type": type(exc).__name__},
                    exc_info=True,
                )

            # Build summary of all items seeded across dataset tables
            dataset_total = sum(dataset_counts.values()) if dataset_counts else 0

            logger.info(
                "Seed data provisioning complete",
                extra={
                    "users_created": users_created,
                    "settings_seeded": settings_seeded,
                    "schedules_created": schedules_created,
                    "briefs_seeded": briefs_seeded,
                    "dataset_total": dataset_total,
                    "dataset_counts": dataset_counts,
                },
            )

            send_cfn_response(
                event=event,
                status=SUCCESS,
                reason=(
                    f"Provisioned {users_created} users, "
                    f"seeded {settings_seeded} settings, "
                    f"created {schedules_created} schedules, "
                    f"seeded {briefs_seeded} historical briefs, "
                    f"seeded {dataset_total} dataset items"
                ),
            )

        elif request_type == "Delete":
            # No-op on delete - keep data intact for potential redeployment
            logger.info("Delete event received - no-op, keeping existing data")
            send_cfn_response(
                event=event,
                status=SUCCESS,
                reason="Delete is a no-op - seed data preserved",
            )

        else:
            logger.warning(
                "Unknown request type received",
                extra={"request_type": request_type},
            )
            send_cfn_response(
                event=event,
                status=FAILED,
                reason=f"Unknown RequestType: {request_type}",
            )

    except KeyError as exc:
        # Missing required environment variable
        error_msg = f"Missing required environment variable: {exc}"
        logger.error(error_msg, extra={"missing_key": str(exc)})
        send_cfn_response(event=event, status=FAILED, reason=error_msg)

    except Exception as exc:
        # Catch-all to ensure CloudFormation always gets a response
        error_msg = f"Seed data provisioning failed: {type(exc).__name__}: {exc}"
        logger.error(
            error_msg,
            extra={"error_type": type(exc).__name__},
            exc_info=True,
        )
        send_cfn_response(event=event, status=FAILED, reason=error_msg)
