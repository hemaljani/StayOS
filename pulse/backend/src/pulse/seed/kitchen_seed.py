"""PULSE Kitchen Seed - CloudFormation custom resource handler.

Seeds the ``pulse-kitchen`` table with a curated Kitchen/F&B demo snapshot for
every pilot property on stack create/update, mirroring LUMI's ``Custom::SeedData``
handler and PULSE's ``pulse.seed.rules_seed``: a ResponseURL PUT that is skipped
when ``ResponseURL`` is absent (so the same Lambda supports a direct-invoke
reseed), a per-property idempotency check (skip a property whose snapshot already
exists), a Delete no-op that preserves data, table name and property list read
from environment variables (PYQUALITY-06), Powertools structured logging, and a
try/except that still sends a CloudFormation response on failure so a deploy
never hangs.

Seeding every property (not just the canonical demo one) is what lets the
Kitchen tab work for all demo GMs: each GM is scoped to their own property, and a
missing snapshot returns 404 "Kitchen snapshot not found".

The handler is a thin orchestrator (PYQUALITY-05): it parses the event and
delegates the snapshot build to ``pulse.seed.kitchen_snapshot`` and the write to
``_seed_kitchen`` / ``_seed_all``.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config
from botocore.exceptions import ClientError

from pulse.seed.kitchen_snapshot import build_kitchen_snapshot

# Module-level logger configured before the handler (PYQUALITY-03).
logger = Logger(service="pulse-kitchen-seed")

# Module-level DynamoDB resource for connection reuse across warm invocations
# (PYQUALITY-06). Adaptive retries for the seed's put/scan calls.
_dynamodb = boto3.resource(
    "dynamodb",
    config=Config(retries={"mode": "standard"}),
)

# Environment variable carrying the pulse-kitchen physical table name. Matches
# pulse.common.config.ENV_KITCHEN_TABLE so the handler and the API agree.
ENV_KITCHEN_TABLE = "KITCHEN_TABLE_NAME"

# Comma-separated property ids to seed a snapshot for (PYQUALITY-06: never
# hardcode resource ids). Falls back to the five pilot demo properties when
# unset, so a fresh deploy seeds a snapshot for every GM's property (not just
# the canonical demo one) and no property 404s on the Kitchen tab.
ENV_ESTATE_PROPERTY_IDS = "ESTATE_PROPERTY_IDS"
DEFAULT_PROPERTY_IDS: tuple[str, ...] = (
    "ALOHA-CHI-001",
    "ALOHA-MIA-001",
    "ALOHA-TYO-001",
    "ALOHA-MAD-001",
    "ALOHA-BOM-001",
)

# The pulse-kitchen key schema: propertyId (PK) only. Used for the per-property
# idempotency probe (a single get_item).
KITCHEN_PK = "propertyId"

# CloudFormation custom resource response statuses.
SUCCESS = "SUCCESS"
FAILED = "FAILED"

# Stable physical id for this custom resource instance.
PHYSICAL_RESOURCE_ID = "PulseKitchenSeedResource"


def _resolve_property_ids() -> list[str]:
    """Resolve the property ids to seed a kitchen snapshot for.

    Reads the ``ESTATE_PROPERTY_IDS`` environment variable (comma-separated) and
    falls back to the pilot demo estate when unset, so a fresh deploy seeds a
    snapshot for every pilot property without any external input.

    Returns:
        The sorted, de-duplicated list of property ids to seed.
    """
    raw = os.environ.get(ENV_ESTATE_PROPERTY_IDS, "")
    configured = [pid.strip() for pid in raw.split(",") if pid.strip()]
    property_ids = configured or list(DEFAULT_PROPERTY_IDS)
    return sorted(set(property_ids))


def _property_already_seeded(table_name: str, property_id: str) -> bool:
    """Check whether a property already has a kitchen snapshot.

    Performs a single ``get_item`` on the property key (the cheapest existence
    probe for this PK-only table) so a CloudFormation update is idempotent and
    does not overwrite a snapshot that may have been mutated since the last
    deploy.

    Args:
        table_name: The ``pulse-kitchen`` physical table name.
        property_id: The property partition key to probe.

    Returns:
        True if the property already holds a snapshot; False when it has none or
        existence could not be determined (seeding then proceeds as a safe
        upsert).
    """
    try:
        table = _dynamodb.Table(table_name)
        response = table.get_item(Key={KITCHEN_PK: property_id})
        if response.get("Item"):
            logger.info(
                "Property already has a kitchen snapshot, skipping",
                extra={"table_name": table_name, "propertyId": property_id},
            )
            return True
    except ClientError as error:
        # If the probe fails, proceed with the idempotent upsert rather than
        # blocking the deploy (PYQUALITY-02: log with context, do not swallow).
        logger.warning(
            "Kitchen seed idempotency check failed, will attempt seeding",
            extra={
                "table_name": table_name,
                "propertyId": property_id,
                "error_code": error.response["Error"]["Code"],
                "error_message": error.response["Error"]["Message"],
            },
        )
    return False


def _seed_kitchen(table_name: str, property_id: str) -> None:
    """Put one property's curated Kitchen snapshot into the pulse-kitchen table.

    Uses the DynamoDB resource interface so the nested snapshot is written with
    native Python types (no manual AttributeValue marshalling). The put is a
    plain upsert, so a direct-invoke reseed refreshes the snapshot.

    Args:
        table_name: The ``pulse-kitchen`` physical table name.
        property_id: The property to seed (partition key).

    Raises:
        ClientError: If the DynamoDB put fails (surfaced to the handler).
    """
    table = _dynamodb.Table(table_name)
    item = build_kitchen_snapshot(property_id)
    # put_item overwrites any existing snapshot for the property (upsert).
    table.put_item(Item=item)
    logger.info(
        "Seeded kitchen snapshot",
        extra={"table_name": table_name, "propertyId": property_id},
    )


def _seed_all(
    table_name: str, property_ids: list[str], *, force: bool
) -> dict[str, Any]:
    """Seed a kitchen snapshot for every configured property.

    Args:
        table_name: The ``pulse-kitchen`` physical table name.
        property_ids: The properties to seed.
        force: When True, seed even if a property already has a snapshot (used by
            a direct-invoke reseed); when False, skip already-seeded properties.

    Returns:
        A summary dict of properties seeded vs skipped.
    """
    seeded: list[str] = []
    skipped: list[str] = []
    for property_id in property_ids:
        if not force and _property_already_seeded(table_name, property_id):
            skipped.append(property_id)
            continue
        _seed_kitchen(table_name, property_id)
        seeded.append(property_id)
    return {"seeded": seeded, "skipped": skipped}


def send_cfn_response(
    event: dict[str, Any],
    status: str,
    reason: str = "",
    physical_resource_id: str = PHYSICAL_RESOURCE_ID,
) -> None:
    """Send a response to CloudFormation via the pre-signed S3 ResponseURL.

    CloudFormation custom resources require a response PUT to the ResponseURL in
    the event to confirm success or failure. When ``ResponseURL`` is absent the
    PUT is skipped so the same Lambda can be invoked directly (a ``make``
    reseed) without a CloudFormation round-trip.

    Args:
        event: CloudFormation custom resource event (may carry ResponseURL).
        status: Either SUCCESS or FAILED.
        reason: Human-readable reason for the status (surfaced in the console).
        physical_resource_id: Stable identifier for this custom resource.

    Raises:
        urllib.error.URLError: If the ResponseURL is present but unreachable.
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

    encoded_body = response_body.encode("utf-8")

    # Skip the PUT for direct invocation (no ResponseURL) - reseed path.
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

    # PUT the status to CloudFormation's pre-signed S3 URL.
    with urllib.request.urlopen(request) as response:  # noqa: S310 (fixed CFN URL)
        logger.info(
            "CloudFormation response sent",
            extra={"http_status": response.status},
        )


@logger.inject_lambda_context
def lambda_handler(event: dict[str, Any], context: Any) -> None:
    """CloudFormation custom resource handler for the kitchen snapshot seed.

    Create/Update seeds the ``pulse-kitchen`` snapshot idempotently (skips when
    the table already has an item). Delete is a no-op that preserves the data.
    A response is always sent to CloudFormation so a deploy never hangs, and the
    ResponseURL PUT is skipped for a direct invoke (reseed).

    Args:
        event: CloudFormation custom resource event with RequestType and
            (for a CFN-driven invoke) ResponseURL, StackId, RequestId, and
            LogicalResourceId.
        context: Lambda context object (runtime metadata).

    Returns:
        None. The outcome is reported to CloudFormation via ``send_cfn_response``.
    """
    request_type = event.get("RequestType", "Unknown")
    logger.info(
        "Processing kitchen seed custom resource event",
        extra={
            "request_type": request_type,
            "stack_id": event.get("StackId", ""),
            "logical_resource_id": event.get("LogicalResourceId", ""),
        },
    )

    try:
        if request_type in ("Create", "Update"):
            # Resource identifier from the environment (PYQUALITY-06).
            kitchen_table_name = os.environ[ENV_KITCHEN_TABLE]
            force = bool(event.get("Force", False))
            # A direct-invoke reseed may target a single property; otherwise seed
            # the whole configured estate so every GM's property has a snapshot.
            single_property = event.get("PropertyId")
            if single_property:
                property_ids = [single_property]
            else:
                property_ids = _resolve_property_ids()

            summary = _seed_all(kitchen_table_name, property_ids, force=force)
            send_cfn_response(
                event=event,
                status=SUCCESS,
                reason=(
                    f"Seeded {len(summary['seeded'])} kitchen snapshots "
                    f"(skipped {len(summary['skipped'])} already-seeded)"
                ),
            )

        elif request_type == "Delete":
            # No-op on delete: preserve data for potential redeployment.
            logger.info("Delete event received - no-op, keeping kitchen snapshot")
            send_cfn_response(
                event=event,
                status=SUCCESS,
                reason="Delete is a no-op - kitchen snapshot preserved",
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
        # Missing required environment variable (fail fast, still respond).
        error_msg = f"Missing required environment variable: {exc}"
        logger.error(error_msg, extra={"missing_key": str(exc)})
        send_cfn_response(event=event, status=FAILED, reason=error_msg)

    except Exception as exc:  # noqa: BLE001 - catch-all guarantees a CFN response
        # Catch-all so CloudFormation always gets a response (deploy never hangs).
        error_msg = f"Kitchen seed failed: {type(exc).__name__}: {exc}"
        logger.error(
            error_msg,
            extra={"error_type": type(exc).__name__},
            exc_info=True,
        )
        send_cfn_response(event=event, status=FAILED, reason=error_msg)
