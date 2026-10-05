"""BedrockAgentCoreApp request-response server for the PULSE Triage Agent.

Entry point for the AgentCore Runtime container. Unlike the LUMI chat agent
(a WebSocket chat service), this is a **request-response** service: the Rule
Engine invokes it asynchronously via ``bedrock-agentcore:InvokeAgentRuntime``
(Decision 8) with a payload ``{alertId, alertType, propertyId, tier}`` and it
triages exactly one alert.

Flow (``handle_triage``):
    1. **Gather facts via the shared Gateway (MCP).** A Strands Agent is built
       with the Gateway-discovered tools (mirroring the LUMI chat agent), and the
       per-alert-type facts are gathered by calling the relevant Gateway tools
       (``situation.build_situation_context``), always passing ``propertyId`` so
       the Gateway scopes the result server-side.
    2. **Generate + validate the brief.** ``pulse.triage.bedrock_client.
       generate_triage_brief`` renders the strict-JSON prompt, gets the narrative
       from the Strands-backed model invoker, then parses/validates (Property 18)
       and applies the deterministic specializations (Walk_Strategy, complaint /
       OOO options). The model id comes from ``TRIAGE_MODEL_ID`` (never
       hardcoded).
    3. **Attach + publish (Decision 8).** ``attach.attach_and_publish`` writes the
       ``triageBrief`` to ``pulse-alerts`` conditional on the alert not being
       terminal (idempotent), then publishes ``ALERT_UPDATED (hasTriageBrief=
       true)`` via the shared ``realtime_publish`` helper (best-effort).

Latency (Decision 8, Requirement 10.6): the relaxed tier targets are CRITICAL
<= 60 s and WARNING <= 120 s (relaxed from 5/15 s to accommodate the agentic
tool-calling loop, AgentCore cold start, AND one automatic retry on a transient
model-output flake). The runtime enforces this budget itself; on breach it
records a triage-failure and leaves the already-delivered alert brief-less
(Requirement 1.7). It never blocks or retries forever.

Environment variables (see ``config.py``): GATEWAY_ENDPOINT_URL, TRIAGE_MODEL_ID,
ALERTS_TABLE_NAME, AWS_DEFAULT_REGION, REALTIME_HTTP_ENDPOINT.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Optional

from attach import attach_and_publish
from aws_lambda_powertools import Logger
from bedrock_agentcore import BedrockAgentCoreApp
from bedrock_agentcore.runtime.models import PingStatus
from config import GATEWAY_AWS_SERVICE, load_runtime_config
from gateway import GatewayToolClient, ToolCaller
from narrative import build_strands_agent, make_strands_invoker
from situation import build_situation_context

from pulse.common.errors import PulseError, TriageFailure
from pulse.common.models import AlertDraft, AlertTier, AlertType
from pulse.delivery import realtime_publish as rt
from pulse.triage.bedrock_client import BedrockInvoker, generate_triage_brief

# Module-level structured logger (Powertools works outside Lambda too).
logger: Logger = Logger(service="pulse-triage-agent")

# Relaxed tier latency targets for producing a triage brief (Decision 8,
# Requirement 10.6). Exceeding the target records a triage-failure and the
# already-delivered alert simply remains without a brief (Requirement 1.7).
RELAXED_TIER_BUDGET_SEC: dict[AlertTier, float] = {
    AlertTier.CRITICAL: 60.0,
    AlertTier.WARNING: 120.0,
    # INFO opportunity briefs (premium cancellation, VIP check-in) are advisory;
    # bound them so triage never runs unbounded, reusing the WARNING budget.
    AlertTier.INFO: 120.0,
}

# Custom metric namespace/name for triage failures (best-effort EMF emission).
_METRIC_NAMESPACE = "PULSE/Triage"
_METRIC_TRIAGE_FAILURES = "TriageFailures"

# Initialize the AgentCore app (Starlette/Uvicorn under the hood).
app: BedrockAgentCoreApp = BedrockAgentCoreApp()


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with a ``Z`` suffix.

    Returns:
        The current time, e.g. ``"2026-08-17T14:33:10Z"``.
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _frozen_clock() -> float:
    """Return a constant so ``generate_triage_brief``'s internal budget is a no-op.

    The reused ``generate_triage_brief`` enforces the pre-Decision-8 tier budget
    (CRITICAL 5 s / WARNING 15 s) via its ``clock`` seam. Those values belong to
    the removed Lambda path; the runtime enforces the relaxed Decision 8 budget
    (60 s / 120 s) itself around the whole invocation, so the internal check is
    neutralized by reporting zero elapsed time.

    Returns:
        The constant ``0.0``.
    """
    return 0.0


def _emit_failure_metric(tier_value: str) -> None:
    """Emit a best-effort ``TriageFailures`` count metric via EMF.

    Args:
        tier_value: The alert tier value, used as the ``Tier`` dimension.
    """
    try:
        from aws_lambda_powertools.metrics import MetricUnit, single_metric

        with single_metric(
            name=_METRIC_TRIAGE_FAILURES,
            unit=MetricUnit.Count,
            value=1,
            namespace=_METRIC_NAMESPACE,
        ) as metric:
            metric.add_dimension(name="Tier", value=tier_value)
    except Exception as error:  # noqa: BLE001 - metrics are best-effort
        logger.debug(
            "Triage failure metric emission failed (non-fatal)",
            extra={"error": str(error)},
        )


def _record_triage_failure(
    alert_id: str, tier_value: str, reason: Optional[str], message: str
) -> dict[str, Any]:
    """Record a triage failure and return a non-throwing result payload.

    Logs the failure with context (Powertools) and emits the best-effort
    failure metric. The already-delivered alert remains without a brief
    (Requirements 1.7, 10.6).

    Args:
        alert_id: The affected alert id.
        tier_value: The alert tier value (for the metric dimension).
        reason: The machine-friendly failure reason.
        message: The human-readable failure description.

    Returns:
        A result dict describing the (handled) failure.
    """
    logger.error(
        "Triage failed; alert remains without a brief",
        extra={"alertId": alert_id, "reason": reason, "error": message},
    )
    _emit_failure_metric(tier_value)
    return {
        "triageFailure": {
            "alertId": alert_id,
            "reason": reason,
            "message": message,
        }
    }


def _parse_payload(payload: dict[str, Any]) -> tuple[str, AlertType, str, AlertTier, Optional[str]]:
    """Validate and unpack the invocation payload.

    Args:
        payload: The ``InvokeAgentRuntime`` payload
            ``{alertId, alertType, propertyId, tier}`` (snake_case accepted too),
            plus an optional ``blockId`` for OOO_CLUSTER alerts.

    Returns:
        An ``(alert_id, alert_type, property_id, tier, block_id)`` tuple;
        ``block_id`` is ``None`` when not supplied.

    Raises:
        TriageFailure: If a required field is missing or has an invalid value.
    """
    try:
        alert_id = str(payload.get("alertId") or payload["alert_id"])
        property_id = str(payload.get("propertyId") or payload["property_id"])
        alert_type = AlertType(payload.get("alertType") or payload["type"])
        tier = AlertTier(payload["tier"])
    except (KeyError, ValueError) as exc:
        raise TriageFailure(
            f"Invalid triage invocation payload: {exc}", reason="invalid_payload"
        ) from exc
    block_id_raw = payload.get("blockId") or payload.get("block_id")
    block_id = str(block_id_raw) if block_id_raw else None
    return alert_id, alert_type, property_id, tier, block_id


def _draft_for(
    alert_id: str, property_id: str, alert_type: AlertType, tier: AlertTier, now: str
) -> AlertDraft:
    """Build the minimal ``AlertDraft`` ``generate_triage_brief`` needs.

    ``generate_triage_brief`` uses the draft's ``type`` (prompt + specialization
    dispatch), ``tier`` (budget, neutralized here), ``alert_id`` and
    ``property_id`` (logging). The remaining draft fields are not consulted, so
    placeholders are used.

    Args:
        alert_id: The alert id.
        property_id: The owning property.
        alert_type: The alert type.
        tier: The alert tier.
        now: An ISO 8601 timestamp used for the placeholder ``created_at``.

    Returns:
        A minimal :class:`AlertDraft`.
    """
    return AlertDraft(
        alert_id=alert_id,
        property_id=property_id,
        tier=tier,
        type=alert_type,
        title=f"{alert_type.value} triage",
        detail=f"Asynchronous triage for {alert_type.value}",
        created_at=now,
        dedupe_key=f"{alert_type.value}#{property_id}#{alert_id}",
        source_entity_ref={},
    )


def handle_triage(
    payload: dict[str, Any],
    *,
    tool_caller: ToolCaller,
    invoker: BedrockInvoker,
    alerts_table_name: str,
    table_getter: Any = None,
    realtime_publisher: Optional[rt.PublisherFn] = None,
    wall_clock: Callable[[], float] = time.monotonic,
    now_fn: Callable[[], str] = _utc_now_iso,
) -> dict[str, Any]:
    """Triage one alert: gather facts, generate the brief, attach + publish.

    All I/O boundaries are injected as seams (the Gateway tool caller, the model
    invoker, the DynamoDB table getter, the realtime publisher, the clock), so
    this orchestration is fully unit-testable without the Gateway, Strands,
    Bedrock, or DynamoDB.

    Args:
        payload: The invocation payload ``{alertId, alertType, propertyId, tier}``.
        tool_caller: The Gateway tool-call seam (fact gathering).
        invoker: The narrative model invoker seam (``generate_triage_brief``).
        alerts_table_name: The ``pulse-alerts`` physical table name.
        table_getter: DynamoDB table-getter seam (defaults to the shared getter).
        realtime_publisher: Realtime publisher seam (best-effort; default
            resolved).
        wall_clock: Monotonic wall clock for the relaxed tier budget (injectable).
        now_fn: ISO 8601 timestamp source (injectable).

    Returns:
        ``{"triageBrief": {...}, "attached": bool, "published": bool}`` on
        success, or ``{"triageFailure": {...}}`` when triage fails (never
        raises; the alert stays brief-less per Requirements 1.7, 10.6).
    """
    # Resolve the table getter lazily so importing this module never requires an
    # AWS session (tests inject a fake getter).
    if table_getter is None:
        from pulse.common.dynamo import get_table

        table_getter = get_table

    try:
        alert_id, alert_type, property_id, tier, block_id = _parse_payload(payload)
    except TriageFailure as failure:
        return _record_triage_failure(
            str(payload.get("alertId", "unknown")),
            str(payload.get("tier", "")),
            failure.reason,
            failure.message,
        )

    logger.append_keys(alertId=alert_id, propertyId=property_id, type=alert_type.value)
    budget = RELAXED_TIER_BUDGET_SEC.get(tier)
    start = wall_clock()

    try:
        # 1. Gather per-alert-type facts via the shared Gateway MCP tools.
        context = build_situation_context(
            alert_type, property_id, tool_caller, block_id=block_id
        )

        # 2. Generate + validate the brief (narrative from the model; structure
        #    guaranteed by pulse.triage). The internal 5/15 s budget is
        #    neutralized (frozen clock); the relaxed budget is enforced below.
        draft = _draft_for(alert_id, property_id, alert_type, tier, now_fn())
        brief = generate_triage_brief(
            draft, context, invoker=invoker, clock=_frozen_clock
        )
    except TriageFailure as failure:
        return _record_triage_failure(
            alert_id, tier.value, failure.reason, failure.message
        )
    except PulseError as failure:  # ConfigurationError etc. -> handled failure
        return _record_triage_failure(
            alert_id, tier.value, "pulse_error", failure.message
        )

    # Relaxed tier-latency enforcement (Decision 8 / Requirement 10.6): if the
    # whole gather+generate exceeded the tier budget, treat as a timeout and
    # leave the alert brief-less rather than attaching a late brief.
    elapsed = wall_clock() - start
    if budget is not None and elapsed > budget:
        return _record_triage_failure(
            alert_id,
            tier.value,
            "timeout",
            f"Triage exceeded the {tier.value} latency target of {budget}s "
            f"(took {elapsed:.3f}s)",
        )

    # 3. Attach (conditional on non-terminal) + publish ALERT_UPDATED.
    attach_result = attach_and_publish(
        alert_id,
        brief,
        alerts_table_name=alerts_table_name,
        now=now_fn(),
        table_getter=table_getter,
        realtime_publisher=realtime_publisher,
    )

    from attach import brief_to_item

    return {
        "triageBrief": brief_to_item(brief),
        "attached": attach_result.attached,
        "published": attach_result.published,
        "reason": attach_result.reason,
    }


@app.ping
def ping_handler() -> PingStatus:
    """AgentCore health check (GET /ping).

    The triage runtime is a stateless request-response service, so it always
    reports Healthy; AgentCore may reclaim the microVM between invocations.

    Returns:
        ``PingStatus.HEALTHY``.
    """
    return PingStatus.HEALTHY


@app.entrypoint
def invoke(payload: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """AgentCore Runtime entrypoint (POST /invocations).

    Opens a shared-Gateway MCP connection, builds the Strands Agent + narrative
    invoker, and triages the single alert described by ``payload``. Any failure
    is recorded and returned as a structured result (the invocation never
    raises, so the already-delivered alert simply stays brief-less).

    Args:
        payload: The ``{alertId, alertType, propertyId, tier}`` invocation body.
        context: The AgentCore runtime context (unused).

    Returns:
        The triage result dict from :func:`handle_triage`.
    """
    config = load_runtime_config()
    try:
        with GatewayToolClient(
            config.gateway_endpoint_url, config.region, GATEWAY_AWS_SERVICE
        ) as gateway:
            # Discover Gateway tools and build the Strands Agent (mirrors the LUMI
            # chat agent). Fact gathering is orchestrated deterministically via
            # gateway.call_tool; the agent supplies the narrative.
            tools = gateway.discover_tools()
            agent = build_strands_agent(config.triage_model_id, tools)
            invoker = make_strands_invoker(agent)
            return handle_triage(
                payload,
                tool_caller=gateway.call_tool,
                invoker=invoker,
                alerts_table_name=config.alerts_table_name,
            )
    except TriageFailure as failure:
        # Gateway connect/discovery failure: record and return (non-throwing).
        return _record_triage_failure(
            str(payload.get("alertId", "unknown")),
            str(payload.get("tier", "")),
            failure.reason,
            failure.message,
        )


if __name__ == "__main__":
    # AgentCore Runtime starts Uvicorn on port 8080 with /ping and /invocations.
    logger.info("Starting PULSE triage agent server on AgentCore Runtime")
    app.run()
