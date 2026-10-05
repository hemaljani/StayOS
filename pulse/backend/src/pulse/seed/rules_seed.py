"""PULSE Rules Seed - CloudFormation custom resource handler.

Seeds the ``pulse-rules`` table with the default, enabled rule set (one rule per
alert-producing type, per property) on stack create/update, so the rule engine
can produce alerts immediately after deploy. Without this the table is empty and
every stream evaluation finds no applicable rule and creates zero alerts.

Mirrors ``pulse.seed.kitchen_seed`` (the kitchen snapshot seed) and LUMI's
``Custom::SeedData`` handler: a ResponseURL PUT that is skipped when
``ResponseURL`` is absent (so the same Lambda supports a direct-invoke reseed),
an idempotency check (skip a property whose rules already exist), a Delete no-op
that preserves data, table name and property list read from environment
variables (PYQUALITY-06), Powertools structured logging, and a try/except that
always sends a CloudFormation response so a deploy never hangs.

The handler is a thin orchestrator (PYQUALITY-05): it parses the event and
delegates rule construction to ``pulse.rule_engine.rule_validation``
(``default_rule_templates`` + ``default_template_item``) and the write to
``_seed_rules_for_property``.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Dict, List

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config
from botocore.exceptions import ClientError

from pulse.rule_engine.rule_validation import (
    default_rule_templates,
    default_template_item,
)

# Module-level logger configured before the handler (PYQUALITY-03).
logger = Logger(service="pulse-rules-seed")

# Module-level DynamoDB resource for connection reuse across warm invocations
# (PYQUALITY-06). Standard retries for the seed's put/query calls.
_dynamodb = boto3.resource(
    "dynamodb",
    config=Config(retries={"mode": "standard"}),
)

# Environment variable carrying the pulse-rules physical table name. Matches
# pulse.common.config.ENV_RULES_TABLE so the handler and the rule engine agree.
ENV_RULES_TABLE = "RULES_TABLE_NAME"

# Comma-separated property ids to seed rules for (PYQUALITY-06: never hardcode
# resource ids). Falls back to the five pilot demo properties when unset, so a
# fresh deploy is self-contained.
ENV_ESTATE_PROPERTY_IDS = "ESTATE_PROPERTY_IDS"
DEFAULT_PROPERTY_IDS: tuple[str, ...] = (
    "ALOHA-CHI-001",
    "ALOHA-MIA-001",
    "ALOHA-TYO-001",
    "ALOHA-MAD-001",
    "ALOHA-BOM-001",
)

# The pulse-rules key schema (design "pulse-rules" model): propertyId (PK) +
# ruleType (SK). Used for the per-property idempotency probe.
RULES_PK = "propertyId"

# CloudFormation custom resource response statuses.
SUCCESS = "SUCCESS"
FAILED = "FAILED"

# Stable physical id for this custom resource instance.
PHYSICAL_RESOURCE_ID = "PulseRulesSeedResource"


def _resolve_property_ids() -> List[str]:
    """Resolve the property ids to seed rules for.

    Reads the ``ESTATE_PROPERTY_IDS`` environment variable (comma-separated) and
    falls back to the pilot demo estate when unset, so a fresh deploy seeds a
    working rule set without any external input.

    Returns:
        The sorted, de-duplicated list of property ids to seed.
    """
    raw = os.environ.get(ENV_ESTATE_PROPERTY_IDS, "")
    configured = [pid.strip() for pid in raw.split(",") if pid.strip()]
    property_ids = configured or list(DEFAULT_PROPERTY_IDS)
    return sorted(set(property_ids))


def _property_already_seeded(table_name: str, property_id: str) -> bool:
    """Check whether a property already has at least one rule.

    Queries the property partition with ``Limit=1`` (the cheapest existence
    probe) so a CloudFormation update is idempotent and does not overwrite rules
    that may have been edited via the ``/rules`` admin API since the last deploy.

    Args:
        table_name: The ``pulse-rules`` physical table name.
        property_id: The property partition key to probe.

    Returns:
        True if the property already holds at least one rule; False when it has
        none or existence could not be determined (seeding then proceeds, and
        each put is a safe upsert).
    """
    try:
        table = _dynamodb.Table(table_name)
        response = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key(RULES_PK).eq(
                property_id
            ),
            Limit=1,
        )
        if response.get("Items"):
            logger.info(
                "Property already has rules, skipping",
                extra={"table_name": table_name, "propertyId": property_id},
            )
            return True
    except ClientError as error:
        # If the probe fails, proceed with the idempotent upsert rather than
        # blocking the deploy (PYQUALITY-02: log with context, do not swallow).
        logger.warning(
            "Rules seed idempotency check failed, will attempt seeding",
            extra={
                "table_name": table_name,
                "propertyId": property_id,
                "error_code": error.response["Error"]["Code"],
                "error_message": error.response["Error"]["Message"],
            },
        )
    return False


def _seed_rules_for_property(table_name: str, property_id: str) -> int:
    """Put the default rule set for one property into the pulse-rules table.

    Reuses the shared ``default_rule_templates`` / ``default_template_item`` so
    the seeded items match exactly what the ``/rules`` admin API and the rule
    engine expect (one enabled rule per alert-producing type). Each put is a
    plain upsert keyed by (propertyId, ruleType), so a reseed refreshes the set.

    Args:
        table_name: The ``pulse-rules`` physical table name.
        property_id: The property to seed rules for.

    Returns:
        The number of rule items written.

    Raises:
        ClientError: If a DynamoDB put fails (surfaced to the handler).
    """
    table = _dynamodb.Table(table_name)
    items = [default_template_item(rule) for rule in default_rule_templates(property_id)]
    # BatchWriter batches the puts (upserts) and retries unprocessed items.
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)
    logger.info(
        "Seeded default rules for property",
        extra={
            "table_name": table_name,
            "propertyId": property_id,
            "ruleCount": len(items),
        },
    )
    return len(items)


def _seed_all(table_name: str, property_ids: List[str], *, force: bool) -> Dict[str, Any]:
    """Seed default rules for every configured property.

    Args:
        table_name: The ``pulse-rules`` physical table name.
        property_ids: The properties to seed.
        force: When True, seed even if a property already has rules (used by a
            direct-invoke reseed); when False, skip already-seeded properties.

    Returns:
        A summary dict of properties seeded vs skipped and the total rule count.
    """
    seeded: List[str] = []
    skipped: List[str] = []
    total_rules = 0
    for property_id in property_ids:
        if not force and _property_already_seeded(table_name, property_id):
            skipped.append(property_id)
            continue
        total_rules += _seed_rules_for_property(table_name, property_id)
        seeded.append(property_id)
    return {"seeded": seeded, "skipped": skipped, "ruleCount": total_rules}


def send_cfn_response(
    event: Dict[str, Any],
    status: str,
    reason: str = "",
    physical_resource_id: str = PHYSICAL_RESOURCE_ID,
) -> None:
    """Send a response to CloudFormation via the pre-signed S3 ResponseURL.

    CloudFormation custom resources require a response PUT to the ResponseURL in
    the event to confirm success or failure. When ``ResponseURL`` is absent the
    PUT is skipped so the same Lambda can be invoked directly (a reseed) without
    a CloudFormation round-trip.

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
def lambda_handler(event: Dict[str, Any], context: Any) -> None:
    """CloudFormation custom resource handler for the default rules seed.

    Create/Update seeds the default rule set for every configured property
    idempotently (skips a property whose rules already exist). Delete is a no-op
    that preserves the rules. A response is always sent to CloudFormation so a
    deploy never hangs, and the ResponseURL PUT is skipped for a direct invoke
    (reseed). A direct invoke may pass ``"Force": true`` to reseed regardless of
    existing rules.

    Args:
        event: CloudFormation custom resource event with RequestType and
            (for a CFN-driven invoke) ResponseURL, StackId, RequestId, and
            LogicalResourceId. A direct invoke may include ``Force``.
        context: Lambda context object (runtime metadata).

    Returns:
        None. The outcome is reported to CloudFormation via ``send_cfn_response``.
    """
    request_type = event.get("RequestType", "Create")
    logger.info(
        "Processing rules seed custom resource event",
        extra={
            "request_type": request_type,
            "stack_id": event.get("StackId", ""),
            "logical_resource_id": event.get("LogicalResourceId", ""),
        },
    )

    try:
        if request_type in ("Create", "Update"):
            # Resource identifier from the environment (PYQUALITY-06).
            rules_table_name = os.environ[ENV_RULES_TABLE]
            property_ids = _resolve_property_ids()
            force = bool(event.get("Force", False))

            summary = _seed_all(rules_table_name, property_ids, force=force)
            send_cfn_response(
                event=event,
                status=SUCCESS,
                reason=(
                    f"Seeded {summary['ruleCount']} rules for "
                    f"{len(summary['seeded'])} properties "
                    f"(skipped {len(summary['skipped'])} already-seeded)"
                ),
            )

        elif request_type == "Delete":
            # No-op on delete: preserve rules for potential redeployment.
            logger.info("Delete event received - no-op, keeping rules")
            send_cfn_response(
                event=event,
                status=SUCCESS,
                reason="Delete is a no-op - rules preserved",
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
        error_msg = f"Rules seed failed: {type(exc).__name__}: {exc}"
        logger.error(
            error_msg,
            extra={"error_type": type(exc).__name__},
            exc_info=True,
        )
        send_cfn_response(event=event, status=FAILED, reason=error_msg)
