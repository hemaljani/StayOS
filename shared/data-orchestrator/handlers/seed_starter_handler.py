"""First-deploy seed-starter Lambda for the StayOS Unified Data Orchestrator.

This thin CloudFormation custom-resource handler starts the
``stayos-data-orchestrator`` state machine in ``seed`` mode on stack
create/update so a fresh deploy performs a full seed fan-out over the pilot
estate (Requirement 1.2, 1.3). It decouples the custom-resource lifecycle from
the (potentially long-running) Standard workflow: it fires ``StartExecution``
and returns immediately with SUCCESS rather than waiting for the seed to finish.

Delete is a no-op that never touches data (upsert-only path; Requirement 8.1).
The state-machine ARN is read from the environment (PYQUALITY-06), never
hardcoded.

Satisfies: Requirements 1.2 (custom-resource trigger), 1.3 (full seed fan-out),
8.1 (no destructive operation on the automated path).
"""

from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config

from orchestrator_common import MODE_SEED, SERVICE_NAME

logger = Logger(service=SERVICE_NAME)

# Environment variable carrying the state-machine ARN (NAMING / PYQUALITY-06).
ENV_STATE_MACHINE_ARN = "STATE_MACHINE_ARN"

# CloudFormation custom-resource lifecycle request types.
REQUEST_CREATE = "Create"
REQUEST_UPDATE = "Update"
REQUEST_DELETE = "Delete"

# Module-level client with an explicit standard-mode retry config (PYQUALITY-06).
# Clients are created once per container for connection reuse across invocations.
_SFN_CLIENT = boto3.client(
    "stepfunctions",
    config=Config(retries={"mode": "standard", "max_attempts": 5}),
)


def start_seed_execution(state_machine_arn: str) -> str:
    """Start the orchestrator state machine in seed mode.

    Args:
        state_machine_arn: The ARN of the orchestrator state machine.

    Returns:
        The started execution ARN.

    Raises:
        _SFN_CLIENT.exceptions.StateMachineDoesNotExist: If the ARN is unknown.
    """
    # A deterministic-ish name aids console correlation; timestamp keeps it unique.
    execution_name = f"seed-{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%S%f')}"
    payload = {"mode": MODE_SEED, "propertyId": None, "referenceDate": None}
    # StartExecution kicks off the async Standard workflow; we do not wait.
    response = _SFN_CLIENT.start_execution(
        stateMachineArn=state_machine_arn,
        name=execution_name,
        input=json.dumps(payload),
    )
    return response["executionArn"]


def _send_cfn_response(
    event: Dict[str, Any],
    context: Any,
    status: str,
    data: Optional[Dict[str, Any]] = None,
    reason: Optional[str] = None,
) -> None:
    """Signal the CloudFormation custom-resource response endpoint.

    Args:
        event: The custom-resource request event (carries ``ResponseURL``).
        context: The Lambda context (for the default failure reason).
        status: ``SUCCESS`` or ``FAILED``.
        data: Optional response data returned to the template.
        reason: Optional human-readable reason (defaults to the log stream).
    """
    response_url = event.get("ResponseURL")
    if not response_url:
        # Direct (non-CFN) invocation - nothing to signal.
        logger.info("no ResponseURL present; skipping CFN signal")
        return
    body = json.dumps(
        {
            "Status": status,
            "Reason": reason
            or f"See CloudWatch log stream: {getattr(context, 'log_stream_name', 'n/a')}",
            "PhysicalResourceId": event.get("PhysicalResourceId")
            or getattr(context, "log_stream_name", "seed-custom-resource"),
            "StackId": event.get("StackId"),
            "RequestId": event.get("RequestId"),
            "LogicalResourceId": event.get("LogicalResourceId"),
            "Data": data or {},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        response_url, data=body, method="PUT", headers={"Content-Type": ""}
    )
    with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310 - CFN URL
        logger.info("sent CFN response", extra={"status": status, "httpStatus": resp.status})


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Thin custom-resource handler: start a seed execution, signal CFN.

    On ``Create``/``Update`` it starts a seed-mode execution; on ``Delete`` it
    is a no-op (never destructive). It always signals CloudFormation so the
    stack does not hang, and reports FAILED with context on error rather than
    masking it (Requirement 9.3).

    Args:
        event: The CloudFormation custom-resource request event.
        context: The Lambda context object.

    Returns:
        A small result dict (useful for direct/test invocation).
    """
    request_type = event.get("RequestType", REQUEST_CREATE)
    logger.info("seed-starter invoked", extra={"requestType": request_type})

    try:
        if request_type == REQUEST_DELETE:
            # Upsert-only path: deleting the stack must not touch demo data.
            _send_cfn_response(event, context, "SUCCESS", {"skipped": "delete is a no-op"})
            return {"status": "skipped", "requestType": request_type}

        state_machine_arn = os.environ[ENV_STATE_MACHINE_ARN]
        execution_arn = start_seed_execution(state_machine_arn)
        logger.info("started seed execution", extra={"executionArn": execution_arn})
        _send_cfn_response(event, context, "SUCCESS", {"executionArn": execution_arn})
        return {"status": "started", "executionArn": execution_arn}
    except Exception as exc:  # noqa: BLE001 - custom-resource boundary must always signal
        logger.exception("seed-starter failed")
        _send_cfn_response(event, context, "FAILED", reason=str(exc))
        # Re-raise so the failure is visible in logs/metrics and not masked.
        raise


lambda_handler = logger.inject_lambda_context(lambda_handler)  # type: ignore[assignment]
