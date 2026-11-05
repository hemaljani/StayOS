"""Invoke forecasting and validate its completed, property-scoped result.

The handler ships inside the shared Lambda package as
``pulse.forecast_invoker.lambda_handler``. It consumes the runtime's JSON
response so a transport success cannot hide a rejected or failed forecast.
Failures are logged and raised for Lambda's invocation retry/error handling.
An empty runtime ARN is an explicit skip during initial deployment.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from typing import Any

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config

logger = Logger(service="pulse-forecaster-invoker")

ENV_FORECAST_AGENT_RUNTIME_ARN = "FORECAST_AGENT_RUNTIME_ARN"
DEFAULT_QUALIFIER = "DEFAULT"
MIN_SESSION_ID_LENGTH = 33
MAX_RESPONSE_BYTES = 64 * 1024

# Allow time for the JSON forecast result rather than just the session start.
# Bound SDK attempts and idle waits; Lambda enforces a 600-second total deadline.
BOTO_CONFIG = Config(
    retries={"mode": "adaptive", "total_max_attempts": 2},
    connect_timeout=5,
    read_timeout=240,
    max_pool_connections=50,
)

# Initialize lazily so importing the packaged handler needs no AWS credentials
# or metadata access. Retain the client across warm invocations.
_agentcore_client: Any = None


def _session_id_for(property_id: str) -> str:
    """Build a unique runtime session identifier of at least 33 characters.

    Args:
        property_id: The property bound to this invocation.

    Returns:
        The new runtime session identifier.
    """
    session_id = f"pulse-forecast-{property_id}-{uuid.uuid4().hex}"
    return session_id.ljust(MIN_SESSION_ID_LENGTH, "0")


def _read_forecast_summary(
    response: dict[str, Any], property_id: str
) -> dict[str, Any]:
    """Consume and validate the runtime summary, always closing its body.

    Args:
        response: The InvokeAgentRuntime SDK response with its streaming body.
        property_id: The property requested by this invocation.

    Returns:
        The validated, completed forecast summary.

    Raises:
        RuntimeError: If transport, JSON, or forecast-result validation fails.
    """
    body = response.get("response")
    try:
        if response.get("statusCode") != 200:
            raise RuntimeError("Forecast runtime returned an unsuccessful status")
        content_type = response.get("contentType", "").split(";", 1)[0].strip()
        if content_type.lower() != "application/json":
            raise RuntimeError("Forecast runtime did not return a JSON summary")
        if body is None:
            raise RuntimeError("Forecast runtime returned no response body")
        chunks = []
        size = 0
        while True:
            chunk = body.read(min(8192, MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise RuntimeError("Forecast runtime summary exceeds the size limit")
            chunks.append(chunk)
        # Reading to EOF also lets StreamingBody verify Content-Length, so a
        # truncated stream cannot count as a completed forecast.
        raw = b"".join(chunks)
        try:
            summary = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError("Forecast runtime returned invalid JSON") from exc
        if not isinstance(summary, dict):
            raise RuntimeError("Forecast runtime summary must be an object")
        if summary.get("failed") or summary.get("rejected"):
            raise RuntimeError("Forecast runtime reported a failed or rejected run")
        if summary.get("propertyId") != property_id:
            raise RuntimeError("Forecast runtime returned a different property")
        for key in ("predictionsWritten", "resolved"):
            if type(summary.get(key)) is not int or summary[key] < 0:
                raise RuntimeError(f"Forecast runtime returned an invalid {key}")
        if type(summary.get("noAlert")) is not bool or summary["noAlert"] != (
            summary["predictionsWritten"] == 0
        ):
            raise RuntimeError("Forecast runtime returned an invalid noAlert")
        return summary
    finally:
        if body is not None:
            body.close()


@logger.inject_lambda_context
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Invoke one property's forecast and report its validated completion.

    Args:
        event: Scheduler target input containing a non-empty ``propertyId``.
        context: Lambda context used for structured logging.

    Returns:
        Completion metadata and forecast counts, or an explicit skip when the
        runtime has not yet been configured.

    Raises:
        ValueError: If the property identifier is missing or invalid.
        Exception: If invocation or response validation fails. The failure is
            logged and re-raised for Lambda error/retry handling.
    """
    global _agentcore_client
    property_id = event.get("propertyId") if isinstance(event, dict) else None
    session_id = None
    try:
        if not isinstance(property_id, str) or not property_id.strip():
            raise ValueError("propertyId must be a non-empty string")
        logger.info("Forecast invocation received", extra={"propertyId": property_id})
        runtime_arn = os.environ.get(ENV_FORECAST_AGENT_RUNTIME_ARN, "").strip()
        if not runtime_arn:
            logger.warning(
                "Forecast runtime not configured; skipping invocation",
                extra={"propertyId": property_id},
            )
            return {
                "invoked": False,
                "completed": False,
                "propertyId": property_id,
                "reason": "runtime_not_configured",
            }
        session_id = _session_id_for(property_id)
        if _agentcore_client is None:
            _agentcore_client = boto3.client("bedrock-agentcore", config=BOTO_CONFIG)
        response = _agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id,
            qualifier=DEFAULT_QUALIFIER,
            contentType="application/json",
            accept="application/json",
            payload=json.dumps({"propertyId": property_id}).encode("utf-8"),
        )
        summary = _read_forecast_summary(response, property_id)
    except Exception:
        # Matches the existing pipeline ForecastRunFailures metric filter.
        logger.exception(
            "Forecast run failure",
            extra={"propertyId": property_id, "runtimeSessionId": session_id},
        )
        raise

    result = {
        "invoked": True,
        "completed": True,
        "propertyId": property_id,
        "runtimeSessionId": session_id,
        "completedAt": datetime.now(UTC).isoformat(),
        "predictionsWritten": summary["predictionsWritten"],
        "resolved": summary["resolved"],
        "noAlert": summary["noAlert"],
    }
    logger.info("Forecast invocation completed", extra=result)
    return result
