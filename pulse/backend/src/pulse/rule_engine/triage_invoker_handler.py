"""Dedicated Triage Agent invoker Lambda (``pulse-triage-invoker``).

This Lambda exists solely to own the *blocking* ``InvokeAgentRuntime`` round
trip to the Triage Agent runtime, decoupled from the Rule Engine's DynamoDB
Streams event source (design Decision 8).

Flow: the Rule Engine (``pulse.rule_engine.triage_invoker.invoke_triage_async``)
issues an asynchronous ``lambda:Invoke`` (``InvocationType="Event"``) to this
function with a ``{alertId, alertType, propertyId, tier}`` event and returns
immediately, so alert delivery is never blocked on the agentic triage. This
Lambda then performs the synchronous, potentially slow (tens of seconds)
``bedrock-agentcore:InvokeAgentRuntime`` call. The runtime gathers facts over
the shared StayOS Gateway, generates and validates the ``triageBrief``, attaches
it to ``pulse-alerts``, and publishes ``ALERT_UPDATED`` itself -- this Lambda
neither parses the streaming response nor attaches the brief.

Design invariants preserved (see ``pulse/AGENTS.md``):
    * Triage stays an async agentic call; delivery is never blocked on the
      brief; the runtime still attaches the brief and publishes ``ALERT_UPDATED``.
    * No synchronous triage Lambda is reintroduced -- this Lambda only fires the
      runtime and returns; it does not compute or attach the brief.

Runtime configuration (PYQUALITY-06 / NAMING-03, never hardcoded):
    * ``TRIAGE_RUNTIME_ARN`` -- the Triage Agent AgentCore runtime ARN, sourced
      at deploy from SSM ``/pulse/triage/runtime-arn``.

Best-effort contract (Requirements 1.4, 1.7): the alert was already delivered
before this Lambda runs, so a missing runtime ARN or an invocation error only
logs a warning and returns a status -- it never raises in a way that would
affect alert creation or delivery. Because the Rule Engine invokes this function
asynchronously, Lambda additionally retries a raised failure up to twice.
"""

from __future__ import annotations

import json
from typing import Any

from pulse.common.aws import get_client
from pulse.common.config import get_optional_env
from pulse.common.logging import get_logger
from pulse.common.tracing import get_tracer

logger = get_logger("pulse-triage-invoker")
tracer = get_tracer("pulse-triage-invoker")

# Environment variable holding the Triage Agent runtime ARN. Injected by
# CloudFormation (TriageRuntimeArn stack param), whose value is set post-deploy
# from SSM /pulse/triage/runtime-arn once the runtime exists. Never hardcoded.
ENV_TRIAGE_RUNTIME_ARN = "TRIAGE_RUNTIME_ARN"

# AgentCore data-plane service used for InvokeAgentRuntime.
_AGENTCORE_SERVICE = "bedrock-agentcore"

# Target the runtime's default (published) endpoint. The runtime is a stateless
# request-response service, so a fresh session per alert is fine.
_DEFAULT_QUALIFIER = "DEFAULT"

# InvokeAgentRuntime requires a runtimeSessionId of at least 33 characters.
_MIN_SESSION_ID_LENGTH = 33


def _session_id_for(alert_id: str) -> str:
    """Build a runtime session id for an alert.

    ``InvokeAgentRuntime`` requires a session id of at least 33 characters. The
    id is derived from the ``alertId`` (which is itself unique per condition) so
    invocations for the same alert share a session, which aids tracing and is
    harmless because the runtime's attach is idempotent.

    Args:
        alert_id: The alert identifier (e.g. ``"alert-<hex>"``).

    Returns:
        A session id of at least 33 characters.
    """
    session_id = f"pulse-triage-{alert_id}"
    # Guarantee the 33-character minimum even for unusually short alert ids.
    return session_id.ljust(_MIN_SESSION_ID_LENGTH, "0")


def invoke_runtime(
    event: dict[str, Any],
    *,
    client: Any | None = None,
    runtime_arn: str | None = None,
) -> dict[str, Any]:
    """Perform the blocking ``InvokeAgentRuntime`` call for a triage event.

    Delegated business logic (PYQUALITY-05) so the handler stays a thin
    orchestrator and this is unit-testable without live AWS. The Triage Agent's
    entrypoint expects the same ``{alertId, alertType, propertyId, tier}``
    payload the Rule Engine built, so the incoming event is forwarded verbatim.

    Args:
        event: The invoker event ``{alertId, alertType, propertyId, tier}``.
        client: The ``bedrock-agentcore`` client; the shared cached client is
            used when omitted. Injectable for testing.
        runtime_arn: The runtime ARN; read from ``TRIAGE_RUNTIME_ARN`` when
            omitted. Injectable for testing.

    Returns:
        A small status dict describing whether the runtime was invoked.
    """
    alert_id = event.get("alertId", "")
    resolved_arn = (
        runtime_arn
        if runtime_arn is not None
        else get_optional_env(ENV_TRIAGE_RUNTIME_ARN)
    )
    if not resolved_arn:
        # Deploy sets TRIAGE_RUNTIME_ARN once the runtime exists; until then the
        # alert is simply delivered without a (later-attached) brief.
        logger.warning(
            "Triage runtime ARN not configured; skipping runtime invocation",
            extra={"alertId": alert_id},
        )
        return {"invoked": False, "reason": "runtime-arn-not-configured"}

    active_client = client if client is not None else get_client(_AGENTCORE_SERVICE)

    # Forward the invoker event to the runtime entrypoint verbatim (camelCase
    # {alertId, alertType, propertyId, tier}). The runtime attaches the brief
    # and publishes ALERT_UPDATED itself, so we neither await nor read the
    # streaming response body.
    active_client.invoke_agent_runtime(
        agentRuntimeArn=resolved_arn,
        runtimeSessionId=_session_id_for(alert_id),
        qualifier=_DEFAULT_QUALIFIER,
        payload=json.dumps(event).encode("utf-8"),
    )
    logger.info(
        "Triage Agent runtime invoked",
        extra={
            "alertId": alert_id,
            "propertyId": event.get("propertyId"),
            "tier": event.get("tier"),
            "type": event.get("alertType"),
        },
    )
    return {"invoked": True, "alertId": alert_id}


@tracer.capture_lambda_handler
def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Async invoker Lambda entry point (thin orchestrator, PYQUALITY-05).

    Parses the ``{alertId, alertType, propertyId, tier}`` event dispatched by
    the Rule Engine and delegates to :func:`invoke_runtime`, which performs the
    blocking ``InvokeAgentRuntime`` call.

    Args:
        event: The invoker event ``{alertId, alertType, propertyId, tier}``.
        context: The Lambda context (unused; present for the handler contract).

    Returns:
        A status dict describing whether the runtime was invoked.
    """
    # Correlate the triage segment with the alert so the rule-eval -> triage ->
    # delivery -> resolve hops share an annotation in the X-Ray service map.
    tracer.put_annotation(key="alertId", value=str(event.get("alertId", "")))
    return invoke_runtime(event)


__all__ = [
    "ENV_TRIAGE_RUNTIME_ARN",
    "invoke_runtime",
    "lambda_handler",
]
