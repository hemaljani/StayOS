"""PULSE Forecaster invoker Lambda (``pulse-forecaster-invoker``).

This Lambda is the scheduling -> invocation hop for the PULSE Predictive
Forecasting Agent. Amazon EventBridge Scheduler fires one schedule per property
on a fixed cadence with a static target input of ``{"propertyId": "..."}``. This
handler reads that ``propertyId`` and starts a ``pulse-forecaster`` AgentCore
Runtime session by calling ``bedrock-agentcore:InvokeAgentRuntime`` with the
``propertyId`` as the session payload. The runtime itself performs all
forecasting work; this Lambda contains no forecasting logic (PYQUALITY-05) and
only orchestrates the start-session call.

Design mapping:
    * Components -> ``functions/forecast-invoker.lambda_handler``
    * Architecture -> Scheduling & invocation

Runtime configuration (PYQUALITY-06 / NAMING-03, never hardcoded):
    * ``FORECAST_AGENT_RUNTIME_ARN`` -- the ``pulse-forecaster`` AgentCore
      Runtime ARN, sourced at deploy from SSM ``/pulse/forecast/runtime-arn``.

Retry / failure contract (Requirement 1.6): a failed start-session attempt is
logged with context and the underlying error is re-raised so the EventBridge
Scheduler ``RetryPolicy`` retries the invocation. Errors are never silently
swallowed.

The ``InvokeAgentRuntime`` call shape (``agentRuntimeArn``, ``runtimeSessionId``
>= 33 characters, ``qualifier`` ``DEFAULT``, ``payload`` as JSON bytes) mirrors
the Triage Agent invoker (``pulse.rule_engine.triage_invoker_handler``) so both
invokers use the identical, verified AgentCore data-plane contract.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any, Dict

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config
from botocore.exceptions import ClientError

# Structured logger with automatic Lambda context enrichment (function name,
# request id, cold-start detection). Powertools is the recommended Lambda logger
# (PYQUALITY-03).
logger = Logger(service="pulse-forecaster-invoker")

# Environment variable holding the pulse-forecaster AgentCore Runtime ARN.
# Injected by CloudFormation from SSM /pulse/forecast/runtime-arn once the
# runtime exists. Never hardcoded (PYQUALITY-06 / NAMING-03).
ENV_FORECAST_AGENT_RUNTIME_ARN = "FORECAST_AGENT_RUNTIME_ARN"

# AgentCore data-plane service used for InvokeAgentRuntime.
AGENTCORE_SERVICE_NAME = "bedrock-agentcore"

# Target the runtime's default (published) endpoint. The forecaster runtime is a
# stateless request-response service, so a fresh session per invocation is fine.
DEFAULT_QUALIFIER = "DEFAULT"

# InvokeAgentRuntime requires a runtimeSessionId of at least 33 characters.
MIN_SESSION_ID_LENGTH = 33

# Shared client configuration: an explicit adaptive retry policy (client-side
# rate limiting on top of standard retries) rather than legacy defaults, plus
# bounded timeouts, matching the PULSE-wide boto3 config (PYQUALITY-06).
BOTO_CONFIG = Config(
    retries={"mode": "adaptive", "max_attempts": 5},
    connect_timeout=5,
    read_timeout=30,
    max_pool_connections=50,
)

# Module-level client for connection reuse across warm invocations; created once
# here rather than inside the handler or a loop (PYQUALITY-06). Clients are
# thread-safe once constructed.
_agentcore_client = boto3.client(AGENTCORE_SERVICE_NAME, config=BOTO_CONFIG)


def _session_id_for(property_id: str) -> str:
    """Build a runtime session id for a forecast invocation.

    ``InvokeAgentRuntime`` requires a session id of at least 33 characters. The
    id embeds the ``propertyId`` for traceability and appends a random suffix so
    each scheduled run starts its own session, then is padded to guarantee the
    33-character minimum even for short property ids.

    Args:
        property_id: The property identifier from the Scheduler input.

    Returns:
        A session id of at least 33 characters.
    """
    session_id = f"pulse-forecast-{property_id}-{uuid.uuid4().hex}"
    return session_id.ljust(MIN_SESSION_ID_LENGTH, "0")


@logger.inject_lambda_context
def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Start a ``pulse-forecaster`` AgentCore Runtime session for one property.

    Thin orchestrator (PYQUALITY-05): reads ``propertyId`` from the EventBridge
    Scheduler target input and issues a single ``InvokeAgentRuntime`` call with
    that ``propertyId`` as the session payload. No forecasting logic lives here.

    Args:
        event: The EventBridge Scheduler target input, ``{"propertyId": "..."}``.
        context: The Lambda context (unused; present for the handler contract).

    Returns:
        A small summary dict describing the started session:
        ``{"invoked": bool, "propertyId": str, "runtimeSessionId": str}``.

    Raises:
        botocore.exceptions.ClientError: If ``InvokeAgentRuntime`` fails. The
            error is logged with context and re-raised so the Scheduler
            RetryPolicy retries the invocation (Requirement 1.6).
    """
    property_id = event.get("propertyId", "")
    # Log every received invocation for auditability (Req 1.6 relies on the
    # invoker recording each attempt, including ones that later fail).
    logger.info("Forecast invocation received", extra={"propertyId": property_id})

    # Resource identifier comes from the environment, never hardcoded.
    runtime_arn = os.environ[ENV_FORECAST_AGENT_RUNTIME_ARN]
    session_id = _session_id_for(property_id)

    try:
        # Start the forecaster session. The runtime does all forecasting work;
        # we pass the propertyId as the session payload (JSON bytes) and target
        # the DEFAULT published endpoint. We do not read the streaming response.
        _agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id,
            qualifier=DEFAULT_QUALIFIER,
            payload=json.dumps({"propertyId": property_id}).encode("utf-8"),
        )
    except ClientError:
        # Record the failed start-session attempt with context, then re-raise so
        # the EventBridge Scheduler RetryPolicy can retry (Req 1.6). Never
        # swallow the error.
        logger.exception(
            "Failed to start forecaster runtime session",
            extra={"propertyId": property_id, "runtimeSessionId": session_id},
        )
        raise

    # Record the successful session-start outcome.
    logger.info(
        "Forecaster runtime session started",
        extra={"propertyId": property_id, "runtimeSessionId": session_id},
    )
    return {
        "invoked": True,
        "propertyId": property_id,
        "runtimeSessionId": session_id,
    }


__all__ = [
    "ENV_FORECAST_AGENT_RUNTIME_ARN",
    "lambda_handler",
]
