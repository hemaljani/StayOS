"""Asynchronous Triage Agent invocation seam for the Rule Engine.

The Triage Agent is a containerized Strands agent hosted on Amazon Bedrock
AgentCore Runtime (design Decision 7). Once the Rule Engine has persisted and
delivered a CRITICAL or WARNING alert, it invokes the runtime **asynchronously
and best-effort** (design Decision 8): the runtime gathers facts over the shared
StayOS Gateway, generates and validates the ``triageBrief``, and then attaches
the brief to ``pulse-alerts`` and publishes ``ALERT_UPDATED`` itself. The Rule
Engine therefore does not wait on, or parse, a synchronous brief response and it
never attaches the brief.

**Why a dedicated invoker Lambda (design Decision 8, corrected).**
``bedrock-agentcore:InvokeAgentRuntime`` is a *synchronous request-response*
call that blocks for the full agentic triage (tens of seconds). The Rule Engine
runs on a DynamoDB Streams event source, so it must not block on that call:
blocking would stall the shard and risk the handler timeout. AgentCore has no
native fire-and-forget flag. Dispatching the blocking call on an in-process
daemon thread does not work either -- Lambda freezes the execution environment
the instant the handler returns, so a thread started just before return never
gets CPU and the invocation is silently lost.

The Rule Engine therefore performs a fast, durable hand-off: it issues an
**asynchronous** ``lambda:Invoke`` (``InvocationType="Event"``) to a dedicated
``pulse-triage-invoker`` Lambda and returns immediately. That invoker Lambda
(see :mod:`pulse.rule_engine.triage_invoker_handler`) owns the blocking
``InvokeAgentRuntime`` round trip on its own timeout budget. This keeps the
Rule Engine non-blocking and delivery-first, preserves the async / best-effort
contract, and still lets the runtime attach the brief itself -- no synchronous
triage Lambda is reintroduced and delivery is never blocked on the brief.

This module is a thin, injectable seam (PYQUALITY-05) so the handler stays a
thin orchestrator and the dispatch is unit-testable without live AWS:

    * The ``lambda`` client is the shared, cached, adaptive-retry client
      (PYQUALITY-06); tests inject a fake client instead.
    * The invoker function name is read from the ``TRIAGE_INVOKER_FUNCTION_NAME``
      environment variable (NAMING-03 / PYQUALITY-06); it is never hardcoded.

Best-effort contract (Requirements 1.4, 1.7): the alert is already delivered
before this runs, so a missing invoker name or a dispatch error only logs a
warning and continues -- it never fails alert creation or delivery. The brief is
a later, asynchronous attach performed by the runtime.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from pulse.common.aws import get_client
from pulse.common.config import get_optional_env
from pulse.common.logging import get_logger
from pulse.common.models import AlertDraft, AlertType

logger = get_logger("pulse-rule-evaluator")

# Environment variable holding the name of the dedicated triage-invoker Lambda.
# Injected by CloudFormation (see pulse-pipeline.yaml) as the physical function
# name ``${StackPrefix}-triage-invoker``. Never hardcoded (NAMING-03).
ENV_TRIAGE_INVOKER_FUNCTION_NAME = "TRIAGE_INVOKER_FUNCTION_NAME"

# Lambda control-plane service used for the asynchronous hand-off invoke.
_LAMBDA_SERVICE = "lambda"

# Asynchronous ("Event") invocation: Lambda queues the invoke and returns a 202
# immediately, so the Rule Engine handler never blocks on the downstream
# InvokeAgentRuntime round trip and the dispatch survives the env freeze.
_ASYNC_INVOCATION_TYPE = "Event"


def build_invoker_event(draft: AlertDraft) -> dict[str, str]:
    """Build the async event payload the triage-invoker Lambda consumes.

    The invoker (and, downstream, the runtime entrypoint) expects
    ``{alertId, alertType, propertyId, tier}`` (camelCase). Enum values are
    stringified via their ``.value``. For OOO_CLUSTER alerts a ``blockId`` is
    also included (parsed from the dedupe key ``OOO_CLUSTER#<propertyId>#<blockId>``)
    so the triage situation builder can reference the real group block instead
    of a placeholder.

    Args:
        draft: The persisted, delivered alert draft to triage.

    Returns:
        The event dict for the ``pulse-triage-invoker`` Lambda.
    """
    event = {
        "alertId": draft.alert_id,
        "alertType": draft.type.value,
        "propertyId": draft.property_id,
        "tier": draft.tier.value,
    }
    # OOO dedupe key is OOO_CLUSTER#<propertyId>#<blockId>; the block id is the
    # trailing segment. Include it so the OOO triage names the real block.
    if draft.type is AlertType.OOO_CLUSTER:
        parts = draft.dedupe_key.split("#")
        if len(parts) >= 3 and parts[-1]:
            event["blockId"] = parts[-1]
    return event


def invoke_triage_async(
    draft: AlertDraft,
    *,
    client: Optional[Any] = None,
    function_name: Optional[str] = None,
) -> None:
    """Dispatch triage for a delivered alert via an async invoker Lambda.

    Issues a fast, non-blocking ``lambda:Invoke`` (``InvocationType="Event"``)
    to the dedicated ``pulse-triage-invoker`` Lambda, which owns the blocking
    ``InvokeAgentRuntime`` round trip. Returns as soon as Lambda accepts the
    async invoke (HTTP 202), so the Rule Engine handler is never blocked on the
    agentic triage.

    Best-effort and non-blocking (design Decision 8): the alert is already
    persisted and delivered, so a missing invoker name or a dispatch error only
    logs a warning and returns -- it never raises and never fails alert creation
    or delivery (Requirements 1.4, 1.7).

    Args:
        draft: The persisted, delivered alert draft to triage.
        client: The ``lambda`` client; the shared cached client is used when
            omitted. Injectable for testing.
        function_name: The invoker function name; read from
            ``TRIAGE_INVOKER_FUNCTION_NAME`` when omitted. Injectable for
            testing.
    """
    resolved_name = (
        function_name
        if function_name is not None
        else get_optional_env(ENV_TRIAGE_INVOKER_FUNCTION_NAME)
    )
    if not resolved_name:
        # Deploy sets TRIAGE_INVOKER_FUNCTION_NAME to the invoker function name;
        # until then the alert is simply delivered without a later brief.
        logger.warning(
            "Triage invoker function not configured; skipping triage dispatch",
            extra={"alertId": draft.alert_id},
        )
        return

    active_client = client if client is not None else get_client(_LAMBDA_SERVICE)
    try:
        # Async hand-off: Event invocation queues the call and returns 202
        # immediately; we neither wait for nor parse the invoker's result.
        active_client.invoke(
            FunctionName=resolved_name,
            InvocationType=_ASYNC_INVOCATION_TYPE,
            Payload=json.dumps(build_invoker_event(draft)).encode("utf-8"),
        )
        logger.info(
            "Triage dispatch queued to invoker Lambda",
            extra={
                "alertId": draft.alert_id,
                "propertyId": draft.property_id,
                "tier": draft.tier.value,
                "type": draft.type.value,
                "invokerFunction": resolved_name,
            },
        )
    except Exception as exc:  # noqa: BLE001 - best-effort, never fail delivery
        # The alert is already delivered; a dispatch failure must not propagate.
        # Log a warning identifying the alert and continue.
        logger.warning(
            "Triage dispatch failed; alert delivered without a brief",
            extra={"alertId": draft.alert_id, "error": str(exc)},
        )


__all__ = [
    "ENV_TRIAGE_INVOKER_FUNCTION_NAME",
    "build_invoker_event",
    "invoke_triage_async",
]
