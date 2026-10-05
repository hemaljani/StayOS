"""BedrockAgentCoreApp entrypoint for the PULSE Forecasting Agent (thin orchestrator).

Entry point for the AgentCore Runtime container. Mirrors the Triage Agent's
``server.py`` shape one-for-one: a ``BedrockAgentCoreApp`` whose ``@app.entrypoint``
opens the shared StayOS Gateway MCP connection (via the shared
``pulse.ops_read.gateway.GatewayToolClient``), builds a Strands agent over the
read-only tool subset, and delegates to a thin orchestrator.

The orchestrator, :func:`run_forecast_session`, contains **no** forecasting logic
(PYQUALITY-05). It only parses the session input, gates the invocation, and
sequences the domain modules that do the real work:

    validate_invocation -> read_signals -> run_forecast
        -> author_remediation_plan (per actionable prediction)
        -> reconcile_alerts (upsert + resolve + publish)

Advisory posture (Req 5.1): this entrypoint is **read + advise only**. It ever
only calls the ``forecast.*`` modules, which write predictive heads-ups to the
existing ``pulse-alerts`` table via the Alert Writer. It NEVER calls the Action
Executor or any operational write tool. The Strands agent is registered with
only the three read-only Gateway tools (``get_occupancy``, ``get_revenue``,
``get_sister_property_availability``); the Signal Reader itself enforces the
read-only allowlist.

Data isolation (Req 6.1): a run binds to exactly one ``propertyId`` (the value
returned by :func:`forecast.invocation.validate_invocation`) and every downstream
call is scoped to that single id.

Error containment (Req 8.3): the whole orchestration body is wrapped so any
failure is logged with ``propertyId`` and returned as a structured failure
summary. A failure never escapes to affect another property's independent
scheduled run.

Testability: the collaborators of :func:`run_forecast_session` (the Gateway
``call_tool`` seam, the loaded config, and the alert reconciler) are all
injectable, so the orchestration can be exercised without a Strands/MCP client,
Bedrock, or DynamoDB (the task 10.3 example tests mock these). The heavy runtime
imports (``strands`` / ``bedrock_agentcore`` and the shared
``pulse.ops_read.gateway.GatewayToolClient``) are performed lazily inside the
AgentCore ``invoke`` entrypoint and the agent-builder helper, so ``import app``
succeeds in a bare test environment that lacks those packages.

Tracing (Req 8.3; Design: Observability -> Tracing): end-to-end AWS X-Ray
instrumentation is added additively and entirely behind the runtime ``invoke``
path so it never affects ``import app`` in a bare test environment. At container
start (:func:`_build_app`) ``boto3`` is patched (:func:`_patch_boto3_for_tracing`)
so the DynamoDB ``pulse-alerts`` writes and the Bedrock **Converse** call the
``forecast.*`` modules make later attach as X-Ray **subsegments**. Per
invocation the entrypoint continues the incoming trace header
(:func:`_begin_trace_segment`), annotates the trace with ``propertyId`` (and
``predictionsWritten`` / ``resolved`` metadata), and propagates the trace header
into the Gateway MCP calls (:func:`_traced_tool_caller`) so the Gateway/tool hop
is linked. ``aws_xray_sdk`` is imported lazily inside each of these helpers and
guarded with ``try/except ImportError``, so the SDK is required only inside the
container - not to import this module or run the unit tests. When it is absent
every tracing helper is a safe no-op.

Environment variables (see ``forecast/config.py``): FORECAST_ENABLED_PROPERTY_IDS,
FORECAST_HORIZON_DAYS, FORECAST_CONFIDENCE_THRESHOLD, FORECAST_MIN_DATA_POINTS,
ALERTS_TABLE_NAME, GATEWAY_ENDPOINT_URL, REALTIME_HTTP_ENDPOINT, FORECAST_MODEL_ID,
POWERTOOLS_SERVICE_NAME, LOG_LEVEL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from aws_lambda_powertools import Logger

# Domain modules are lightweight siblings (no heavy runtime deps at import), so
# they are imported at module load. The Gateway / Strands / AgentCore imports are
# deferred to the runtime entrypoint so ``import app`` works without them.
from forecast import alerts as alerts_module
from forecast import config as config_module
from forecast import engine as engine_module
from forecast import invocation as invocation_module
from forecast import narrative as narrative_module
from forecast import signals as signals_module
from forecast.config import ForecastConfig
from forecast.invocation import InvalidInvocationError
from forecast.signals import SignalSet, ToolCaller

# The Gateway inbound-auth service namespace for the SigV4 signer, identical to
# the Triage Agent (aws_service="bedrock-agentcore").
GATEWAY_AWS_SERVICE = "bedrock-agentcore"

# The three READ-ONLY Gateway tools this advisory agent is allowed to register
# and call (Req 2.5, 5.1). The agent never registers a write tool.
READ_ONLY_TOOL_NAMES: tuple[str, ...] = (
    signals_module.TOOL_GET_OCCUPANCY,
    signals_module.TOOL_GET_REVENUE,
    signals_module.TOOL_GET_SISTER_PROPERTY_AVAILABILITY,
)

# Structured markers logged on every run outcome so the observability metric
# filters (task 16) can match them on the pipeline log group.
_MARKER_RUN_FAILURE = "Forecast run failure"
_MARKER_NO_ALERT = "Forecast no-alert outcome"

# X-Ray tracing constants (Design: Observability -> Tracing). The subsegment
# name wraps one per-property forecasting session; propertyId is attached as a
# filterable X-Ray annotation, and the tool-hop keys carry the trace header into
# the Gateway MCP call arguments so the Gateway/tool span links back.
_TRACE_SEGMENT_NAME = "forecast-session"
_TRACE_ANNOTATION_PROPERTY_ID = "propertyId"
# The header key AgentCore / upstream callers use to pass the X-Ray trace header
# (case-insensitive on the wire; the AgentCore session input echoes it as-is).
_TRACE_HEADER_KEYS: tuple[str, ...] = ("X-Amzn-Trace-Id", "x-amzn-trace-id")
# The argument key the Gateway tool call carries the propagated trace header on,
# so the downstream Gateway hop can continue the same trace.
_TRACE_HEADER_ARG_KEY = "_xrayTraceHeader"

# Module-level structured logger (Powertools works outside Lambda too).
logger: Logger = Logger(service=config_module.DEFAULT_SERVICE_NAME)

# The alert reconciler seam: signature-compatible with
# ``forecast.alerts.reconcile_alerts``. Kept as a type alias so the orchestrator
# can accept an injected fake in tests without importing DynamoDB.
AlertReconciler = Callable[..., "alerts_module.ReconcileResult"]


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string.

    Returns:
        The current UTC time, e.g. ``"2026-08-17T14:33:10+00:00"``.
    """
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# X-Ray tracing helpers (additive; guarded so bare `import app` never needs the
# aws_xray_sdk package - the import is lazy and every helper no-ops on absence).
# ---------------------------------------------------------------------------


def _patch_boto3_for_tracing() -> None:
    """Patch ``boto3`` so downstream AWS calls attach as X-Ray subsegments.

    Called once at container start (from :func:`_build_app`). ``aws_xray_sdk``'s
    ``patch(['boto3'])`` instruments the shared botocore machinery, so the
    ``bedrock-runtime`` and DynamoDB clients the ``forecast.*`` modules create
    lazily later are traced automatically - the DynamoDB ``pulse-alerts`` writes
    and the Bedrock **Converse** call each become a subsegment of the active
    session segment (Design: Observability -> Tracing).

    The import is lazy and guarded so this is a safe no-op when ``aws_xray_sdk``
    is not installed (e.g. a bare unit-test host); tracing is only expected
    inside the container image, whose ``requirements.txt`` pins the SDK.

    Returns:
        None.
    """
    try:
        from aws_xray_sdk.core import patch
    except ImportError:
        logger.debug("aws_xray_sdk not installed; skipping boto3 trace patching")
        return
    # Patch only boto3/botocore (not every supported library) so the DynamoDB
    # writes and the Bedrock Converse call attach as subsegments.
    patch(["boto3"])
    logger.debug("Patched boto3 for X-Ray subsegment tracing")


def _extract_trace_header(
    payload: Mapping[str, Any], context: Any
) -> Optional[str]:
    """Return the incoming X-Ray trace header, if any, to continue the trace.

    The upstream invoker Lambda runs with ``TracingConfig: Active`` and
    propagates its trace header to the AgentCore start-session call. The header
    may reach this entrypoint either echoed in the session ``payload`` (under an
    ``X-Amzn-Trace-Id`` key) or on the AgentCore runtime ``context``; both are
    checked defensively so a missing header simply starts a fresh trace.

    Args:
        payload: The AgentCore session input mapping.
        context: The AgentCore runtime context object (may be ``None``).

    Returns:
        The trace-header string to continue, or ``None`` when none is present.
    """
    for key in _TRACE_HEADER_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    # AgentCore contexts vary by SDK version; read a trace header attribute
    # defensively without assuming a concrete type.
    for attr in ("trace_id", "trace_header", "x_amzn_trace_id"):
        candidate = getattr(context, attr, None)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _begin_trace_segment(trace_header: Optional[str]) -> Any:
    """Begin (continue) an X-Ray subsegment for one forecasting session.

    Opens a subsegment named :data:`_TRACE_SEGMENT_NAME` under the active X-Ray
    segment so the whole session - including the boto3 subsegments produced by
    :func:`_patch_boto3_for_tracing` - is captured under one span. When an
    incoming ``trace_header`` is present it is applied to the recorder first so
    the session continues the upstream trace (Scheduler -> invoker Lambda ->
    AgentCore Runtime) rather than starting a disconnected one.

    The import is lazy and guarded so this is a safe no-op (returns ``None``)
    when ``aws_xray_sdk`` is absent.

    Args:
        trace_header: The incoming ``X-Amzn-Trace-Id`` header to continue, or
            ``None`` to let the recorder manage the trace context itself.

    Returns:
        The opened X-Ray subsegment, or ``None`` when tracing is unavailable or
        could not be started.
    """
    try:
        from aws_xray_sdk.core import xray_recorder
        from aws_xray_sdk.core.models.trace_header import TraceHeader
    except ImportError:
        return None
    try:
        if trace_header:
            # Seed the recorder's entity context from the incoming header so the
            # subsegment attaches to the upstream trace.
            parsed = TraceHeader.from_header_str(trace_header)
            if parsed.root:
                xray_recorder.begin_segment(
                    name=_TRACE_SEGMENT_NAME,
                    traceid=parsed.root,
                    parent_id=parsed.parent,
                )
                return xray_recorder.current_segment()
        return xray_recorder.begin_subsegment(_TRACE_SEGMENT_NAME)
    except Exception as error:  # noqa: BLE001 - tracing must never break a run
        logger.debug(
            "Could not begin X-Ray session segment (non-fatal)",
            extra={"error": str(error)},
        )
        return None


def _end_trace_segment(segment: Any, trace_header: Optional[str]) -> None:
    """Close the session X-Ray segment/subsegment opened by :func:`_begin_trace_segment`.

    Args:
        segment: The entity returned by :func:`_begin_trace_segment` (``None``
            when tracing was unavailable).
        trace_header: The incoming trace header; when present a full segment was
            opened, so it (not a subsegment) is ended.

    Returns:
        None.
    """
    if segment is None:
        return
    try:
        from aws_xray_sdk.core import xray_recorder

        if trace_header:
            xray_recorder.end_segment()
        else:
            xray_recorder.end_subsegment()
    except Exception as error:  # noqa: BLE001 - tracing must never break a run
        logger.debug(
            "Could not end X-Ray session segment (non-fatal)",
            extra={"error": str(error)},
        )


def _annotate_property(property_id: Optional[str]) -> None:
    """Attach ``propertyId`` to the active X-Ray segment as a filterable annotation.

    Annotations (not metadata) are indexed by X-Ray, so annotating ``propertyId``
    lets a trace be filtered to one property and cross-references the structured
    logs (which carry ``propertyId`` on every line) for that run (Design:
    Observability -> Tracing / Logs Insights correlation).

    Args:
        property_id: The bound run property, or ``None`` when unknown (skipped).

    Returns:
        None.
    """
    if not property_id:
        return
    try:
        from aws_xray_sdk.core import xray_recorder

        entity = xray_recorder.current_subsegment() or xray_recorder.current_segment()
        if entity is not None:
            entity.put_annotation(_TRACE_ANNOTATION_PROPERTY_ID, property_id)
    except Exception as error:  # noqa: BLE001 - tracing must never break a run
        logger.debug(
            "Could not annotate X-Ray trace with propertyId (non-fatal)",
            extra={"error": str(error)},
        )


def _annotate_run_summary(summary: Mapping[str, Any]) -> None:
    """Attach useful run-summary values to the active X-Ray segment as metadata.

    The entrypoint-visible run summary exposes counts (``predictionsWritten``,
    ``resolved``) rather than per-prediction ``dedupeKey`` / ``confidence`` (those
    live inside the reconciler), so they are recorded as X-Ray **metadata** for
    context while ``propertyId`` remains the indexed annotation.

    Args:
        summary: The run-summary mapping returned by
            :func:`run_forecast_session`.

    Returns:
        None.
    """
    try:
        from aws_xray_sdk.core import xray_recorder

        entity = xray_recorder.current_subsegment() or xray_recorder.current_segment()
        if entity is None:
            return
        for key in ("predictionsWritten", "resolved", "noAlert", "rejected", "failed"):
            if key in summary:
                entity.put_metadata(key, summary[key], namespace="forecast")
    except Exception as error:  # noqa: BLE001 - tracing must never break a run
        logger.debug(
            "Could not attach X-Ray run-summary metadata (non-fatal)",
            extra={"error": str(error)},
        )


def _current_trace_header() -> Optional[str]:
    """Return the active X-Ray trace header string for downstream propagation.

    Builds an ``X-Amzn-Trace-Id`` header from the current segment/subsegment so
    it can be forwarded into the Gateway MCP call arguments, linking the
    Gateway/tool hop into the same trace (Design: Observability -> Tracing).

    Returns:
        The trace-header string, or ``None`` when tracing is unavailable.
    """
    try:
        from aws_xray_sdk.core import xray_recorder
        from aws_xray_sdk.core.models.trace_header import TraceHeader

        segment = xray_recorder.current_segment()
        if segment is None:
            return None
        subsegment = xray_recorder.current_subsegment()
        parent_id = subsegment.id if subsegment is not None else segment.id
        return TraceHeader(
            root=segment.trace_id,
            parent=parent_id,
            sampled=segment.sampled,
        ).to_header_str()
    except Exception as error:  # noqa: BLE001 - tracing must never break a run
        logger.debug(
            "Could not build X-Ray trace header for propagation (non-fatal)",
            extra={"error": str(error)},
        )
        return None


def _traced_tool_caller(call_tool: "ToolCaller") -> "ToolCaller":
    """Wrap a Gateway ``call_tool`` seam so each call carries the trace header.

    Returns a thin pass-through that injects the current X-Ray trace header into
    the tool arguments (under :data:`_TRACE_HEADER_ARG_KEY`) before delegating,
    so the Gateway/tool hop links into the session trace. The wrapper is a no-op
    passthrough when no trace header is available (``aws_xray_sdk`` absent or no
    active segment), preserving the exact seam behavior for tests.

    Args:
        call_tool: The underlying Gateway read-only tool-call seam.

    Returns:
        A ``ToolCaller`` that propagates the trace header and then delegates.
    """

    def _traced_call(tool_name: str, arguments: dict[str, Any]) -> Any:
        """Inject the active trace header into ``arguments`` then delegate.

        Args:
            tool_name: The Gateway tool name to invoke.
            arguments: The tool arguments (always includes ``propertyId``).

        Returns:
            The wrapped tool call's result, unchanged.
        """
        header = _current_trace_header()
        if header:
            # Copy so we never mutate the caller's dict; carry the trace header
            # alongside the existing arguments for the Gateway hop to continue.
            arguments = {**arguments, _TRACE_HEADER_ARG_KEY: header}
        return call_tool(tool_name, arguments)

    return _traced_call


def _coerce_int(value: Any) -> Optional[int]:
    """Best-effort coercion of a raw signal value to a non-negative ``int``.

    The raw Gateway tool shapes are demo data, so this tolerates ints, numeric
    strings, and floats, returning ``None`` for anything unparseable so the
    caller can treat the date as having insufficient data.

    Args:
        value: A raw value read from a signal record.

    Returns:
        The parsed integer, or ``None`` when the value cannot be interpreted.
    """
    if isinstance(value, bool):
        # bool is an int subclass; a boolean is not a valid room count.
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(float(text)) if text else None
        except ValueError:
            return None
    return None


def _iter_signal_records(raw: Any) -> list[dict[str, Any]]:
    """Yield the per-date record dicts from a raw signal result (thin adapter).

    The Gateway tool results are demo-shaped: either a bare list of record dicts
    or a dict enveloping the rows under one of the common list keys. This does a
    minimal best-effort unwrap and returns only dict rows; non-dict rows are
    ignored. Signal parsing is intentionally thin here (PYQUALITY-05) - the
    engine and alert writer do the real work.

    Args:
        raw: The raw ``get_occupancy`` result (or ``None``).

    Returns:
        A list of record dicts, empty when nothing usable is present.
    """
    if raw is None:
        return []
    rows: Any = raw
    if isinstance(raw, dict):
        for list_key in signals_module.RECORD_LIST_KEYS:
            if isinstance(raw.get(list_key), list):
                rows = raw[list_key]
                break
        else:
            # A single record dict, not an envelope: treat it as one row.
            rows = [raw]
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _signals_to_daily_demands(
    signals: SignalSet,
) -> tuple[engine_module.DailyDemand, ...]:
    """Adapt the raw occupancy signal into the engine's ``DailyDemand`` inputs.

    A thin, best-effort mapping from the demo-shaped ``get_occupancy`` records to
    the pure engine's per-date demand inputs. Each record is expected to carry a
    date plus occupied/arriving/capacity counts under camelCase keys; a record
    missing any required count is emitted as ``has_sufficient_data=False`` so the
    engine records it unevaluable and continues (Req 3.3, 8.2) rather than the
    adapter guessing values. Revenue pace is not decomposed here; the engine's
    confidence model consumes the demand shape it is given.

    NOTE (demo data): the raw tool shapes are not contract-fixed, so this mapping
    is deliberately minimal and defensive. It reads the first present key from a
    small set of aliases per field and coerces numeric strings/floats to ints.

    Args:
        signals: The :class:`SignalSet` read for the bound property.

    Returns:
        The per-date :class:`DailyDemand` tuple for the engine, in record order.
    """
    demands: list[engine_module.DailyDemand] = []
    for record in _iter_signal_records(signals.occupancy):
        record_date = record.get("date") or record.get("stayDate")
        occupied = _coerce_int(
            record.get("occupiedRooms", record.get("occupied"))
        )
        arriving = _coerce_int(
            record.get("arrivingRooms", record.get("arriving"))
        )
        capacity = _coerce_int(
            record.get("sellableCapacity", record.get("capacity"))
        )
        has_sufficient = (
            isinstance(record_date, str)
            and occupied is not None
            and arriving is not None
            and capacity is not None
        )
        demands.append(
            engine_module.DailyDemand(
                date=record_date if isinstance(record_date, str) else "",
                occupied_rooms=occupied or 0,
                arriving_rooms=arriving or 0,
                sellable_capacity=capacity or 0,
                has_sufficient_data=has_sufficient,
            )
        )
    return tuple(demands)


def _sister_availability_pairs(
    signals: SignalSet,
) -> tuple[tuple[str, int], ...]:
    """Project sister availability into the engine's ``(id, rooms)`` pairs.

    Reuses the Signal Reader's pure projection so ONLY the availability value per
    sister ``propertyId`` crosses the property boundary (Req 6.5, 6.6); every
    other sister operational field is discarded. Unparseable availability values
    are skipped.

    Args:
        signals: The :class:`SignalSet` whose ``sister_availability`` holds the
            raw ``get_sister_property_availability`` result (``None`` when
            overflow was not evaluated).

    Returns:
        A tuple of ``(sister_property_id, available_rooms)`` pairs; empty when no
        sister availability is present.
    """
    if signals.sister_availability is None:
        return ()
    projected = signals_module.project_sister_availability(
        signals.sister_availability, signals.property_id
    )
    pairs: list[tuple[str, int]] = []
    for sister_id, raw_rooms in projected.items():
        rooms = _coerce_int(raw_rooms)
        if isinstance(sister_id, str) and sister_id and rooms is not None:
            pairs.append((sister_id, rooms))
    return tuple(pairs)


def _prediction_is_partial(
    prediction: engine_module.OversellPrediction, signals: SignalSet
) -> bool:
    """Return whether a prediction was built from partial required signals.

    A required signal (``get_occupancy`` / ``get_revenue``) missing after retries
    flags the prediction as partial-data (Req 2.8). The engine also carries its
    own ``based_on_partial_data`` marker; either source marks the prediction
    partial.

    Args:
        prediction: The engine prediction under consideration.
        signals: The signal set the run read.

    Returns:
        ``True`` when the prediction is based on partial input data.
    """
    required_missing = any(
        tool in signals.missing_signals
        for tool in (
            signals_module.TOOL_GET_OCCUPANCY,
            signals_module.TOOL_GET_REVENUE,
        )
    )
    return bool(prediction.based_on_partial_data or required_missing)


def _build_reconcile_prediction(
    prediction: engine_module.OversellPrediction,
    signals: SignalSet,
    config: ForecastConfig,
) -> Optional[dict[str, Any]]:
    """Author the plan and assemble the reconciler input for one prediction.

    Thin per-prediction assembly (PYQUALITY-05): it derives the dedupeKey, authors
    the Remediation_Plan around the engine's fixed numbers via
    ``narrative.author_remediation_plan`` (the numbers are never altered by the
    model, Req 3.9), and builds the ``pulse-alerts`` item via
    ``alerts.build_forecast_alert_item``. On a dedupeKey-derivation failure the
    prediction is skipped (``None`` returned) and the error is logged, leaving
    existing records unchanged (Req 7.6).

    Args:
        prediction: An actionable engine prediction (confidence >= 50).
        signals: The signal set the run read (for the partial-data flag).
        config: The loaded forecasting configuration (model id, threshold).

    Returns:
        A reconciler-ready mapping ``{"item": ..., "dedupe_key": ...}``, or
        ``None`` when the prediction must be skipped.
    """
    try:
        dedupe_key = alerts_module.dedupe_key_for_prediction(
            {"anticipated_date": prediction.anticipated_date},
            prediction.property_id,
            condition_type=prediction.condition_type,
        )
    except alerts_module.DedupeKeyError as error:
        logger.error(
            "Skipping prediction with an underivable dedupeKey",
            extra={
                "propertyId": prediction.property_id,
                "anticipatedDate": prediction.anticipated_date,
                "failedInput": error.failed_input,
            },
        )
        return None

    based_on_partial_data = _prediction_is_partial(prediction, signals)

    # Author the Remediation_Plan around the engine's fixed numbers. When no
    # model is configured (or it fails), a deterministic template plan carrying
    # the identical numbers is returned (Req 3.10).
    plan = narrative_module.author_remediation_plan(
        rooms_oversold=prediction.rooms_oversold,
        anticipated_date=prediction.anticipated_date,
        confidence=prediction.confidence,
        overflow_sister_property_id=prediction.overflow_sister_property_id,
        overflow_available_rooms=prediction.overflow_available_rooms,
        model_id=config.model_id,
    )

    item = alerts_module.build_forecast_alert_item(
        property_id=prediction.property_id,
        plan=plan,
        rooms_oversold=prediction.rooms_oversold,
        anticipated_date=prediction.anticipated_date,
        confidence=prediction.confidence,
        dedupe_key=dedupe_key,
        based_on_partial_data=based_on_partial_data,
        threshold=config.confidence_threshold,
        condition_type=prediction.condition_type,
    )
    return {"item": item, "dedupe_key": dedupe_key}


def run_forecast_session(
    session_input: Mapping[str, Any],
    *,
    call_tool: ToolCaller,
    config: Optional[ForecastConfig] = None,
    reconcile: AlertReconciler = alerts_module.reconcile_alerts,
    now_fn: Callable[[], str] = _utc_now_iso,
) -> dict[str, Any]:
    """Run one per-property forecasting session (thin orchestrator).

    Parses ``{propertyId}`` from the session input, gates the invocation, binds
    exactly one property to the run (Req 6.1), and sequences the domain modules
    that do the real work (PYQUALITY-05 - this function holds NO forecasting
    logic):

    1. ``validate_invocation`` against the configured enabled ids; on
       :class:`InvalidInvocationError`, terminate the run and return a rejected
       summary, leaving any prior forecast unchanged (Req 1.4, 1.5).
    2. Build the horizon from the configured ``horizon_days`` and read the
       operational signals (``read_signals``) for the bound property.
    3. Run the pure engine (``run_forecast``). When it produces an actionable
       prediction, re-read with sister availability (two-phase overflow, Req 2.3)
       and re-run so the deterministic overflow facts are fixed.
    4. Author a Remediation_Plan per actionable prediction and reconcile the
       ``pulse-alerts`` records (upsert + resolve + publish) via ``reconcile``.
    5. Return a small run-summary dict. When no prediction meets the threshold,
       record a successful no-alert outcome (Req 8.4).

    Advisory posture (Req 5.1): this function only ever calls the ``forecast.*``
    modules (which write to ``pulse-alerts``); it never invokes the Action
    Executor or any operational write tool.

    Error containment (Req 8.3): any error is caught, logged with ``propertyId``,
    and returned as a failure summary, so a failure never escapes to affect
    another property's independent run.

    Args:
        session_input: The AgentCore session input; the bound property is read
            from its ``propertyId`` key.
        call_tool: The Gateway read-only tool-call seam passed to the Signal
            Reader. Injectable so the orchestration is testable without a
            Strands/MCP client.
        config: The loaded forecasting configuration. Defaults to
            ``forecast.config.load_config()``; injectable for tests.
        reconcile: The alert reconciler seam. Defaults to
            ``forecast.alerts.reconcile_alerts``; injectable so tests can assert
            the write set without DynamoDB.
        now_fn: ISO 8601 timestamp source for the reconciler. Injectable.

    Returns:
        A run-summary dict. On success:
        ``{"propertyId", "predictionsWritten", "resolved", "noAlert"}``. On a
        rejected invocation: ``{"propertyId", "rejected": True, "reason"}``. On a
        contained failure: ``{"propertyId", "failed": True, "reason"}``.
    """
    active_config = config if config is not None else config_module.load_config()

    # 1. Invocation gate (Req 1.4, 1.5). Runs BEFORE any read; on rejection the
    #    run terminates and any previously generated forecast is left unchanged.
    try:
        property_id = invocation_module.validate_invocation(
            session_input, active_config.enabled_property_ids
        )
    except InvalidInvocationError as error:
        logger.warning(
            "Rejected forecasting invocation; terminating run before any read",
            extra={
                "receivedPropertyId": error.received_value,
                "reason": error.reason,
            },
        )
        return {
            "propertyId": None,
            "rejected": True,
            "reason": error.reason,
        }

    # Bind the single property to this run for the remaining structured logs.
    logger.append_keys(propertyId=property_id)

    try:
        return _orchestrate_bound_run(
            property_id,
            call_tool=call_tool,
            config=active_config,
            reconcile=reconcile,
            now_fn=now_fn,
        )
    except Exception as error:  # noqa: BLE001 - contain any failure to this run
        # Error containment (Req 8.3): log with propertyId and return a failure
        # summary. Never let the exception escape to affect another property.
        logger.exception(
            _MARKER_RUN_FAILURE,
            extra={"propertyId": property_id, "error": repr(error)},
        )
        return {
            "propertyId": property_id,
            "failed": True,
            "reason": type(error).__name__,
        }


def _orchestrate_bound_run(
    property_id: str,
    *,
    call_tool: ToolCaller,
    config: ForecastConfig,
    reconcile: AlertReconciler,
    now_fn: Callable[[], str],
) -> dict[str, Any]:
    """Orchestrate the read -> forecast -> author -> reconcile flow for one property.

    Split out of :func:`run_forecast_session` so the outer function stays a thin
    gate + try/except while this body sequences the collaborators (still no
    forecasting logic of its own - the engine and alert writer do the work).

    Args:
        property_id: The single bound run property (Req 6.1).
        call_tool: The Gateway read-only tool-call seam.
        config: The loaded forecasting configuration.
        reconcile: The alert reconciler seam.
        now_fn: ISO 8601 timestamp source for the reconciler.

    Returns:
        The success run-summary dict (see :func:`run_forecast_session`).
    """
    # 2. Build the horizon from the configured length and read signals. The
    #    reference date is the current UTC calendar day (the engine treats the
    #    horizon start as the local calendar date).
    reference_date = datetime.now(timezone.utc).date()
    horizon = engine_module.build_forecast_horizon(
        reference_date, config.horizon_days
    )
    signals = signals_module.read_signals(property_id, horizon, call_tool)

    # 3. First pass (no overflow read). If any oversell is actionable, do the
    #    second-phase sister-availability read and re-run so the deterministic
    #    overflow facts are fixed (two-phase overflow, Req 2.3).
    result = engine_module.run_forecast(
        _signals_to_daily_demands(signals),
        horizon,
        property_id,
        config.min_data_points,
        actionable_confidence=engine_module.ACTIONABLE_CONFIDENCE_THRESHOLD,
    )
    if result.predictions and signals.sister_availability is None:
        signals = signals_module.read_signals(
            property_id, horizon, call_tool, needs_overflow=True
        )
        result = engine_module.run_forecast(
            _signals_to_daily_demands(signals),
            horizon,
            property_id,
            config.min_data_points,
            sister_availability=_sister_availability_pairs(signals),
            actionable_confidence=engine_module.ACTIONABLE_CONFIDENCE_THRESHOLD,
        )

    # 4a. Author a plan per actionable prediction that also meets the write
    #     threshold, and assemble the reconciler inputs. Predictions with an
    #     underivable dedupeKey are skipped (Req 7.6).
    reconcile_predictions: list[dict[str, Any]] = []
    for prediction in result.predictions:
        if not alerts_module.meets_threshold(
            prediction.confidence, config.confidence_threshold
        ):
            # Below the write threshold: no alert for this prediction (Req 4.8).
            continue
        assembled = _build_reconcile_prediction(prediction, signals, config)
        if assembled is not None:
            reconcile_predictions.append(assembled)

    # 4b. Reconcile: upsert this run's predictions, resolve stale open alerts,
    #     and publish delivery events (all inside the Alert Writer; best-effort
    #     delivery never rolls back a write). This is the ONLY write path, and it
    #     targets pulse-alerts - never the executor (Req 5.1).
    reconcile_result = reconcile(
        reconcile_predictions,
        property_id=property_id,
        alerts_table_name=config.alerts_table_name,
        now=now_fn(),
        realtime_publisher=None,
    )

    predictions_written = len(reconcile_result.upserts)
    resolved_count = len(reconcile_result.resolved)
    no_alert = predictions_written == 0

    # 5. Record a successful no-alert outcome when nothing met the threshold
    #    (Req 8.4). This is a healthy outcome, logged at INFO for the metric
    #    filter, not an error.
    if no_alert:
        logger.info(
            _MARKER_NO_ALERT,
            extra={
                "propertyId": property_id,
                "unevaluableDates": list(result.unevaluable_dates),
            },
        )

    return {
        "propertyId": property_id,
        "predictionsWritten": predictions_written,
        "resolved": resolved_count,
        "noAlert": no_alert,
    }


def build_forecast_agent(model_id: str, tools: list[Any]) -> Any:
    """Build the per-invocation Strands agent (Claude Sonnet + read-only tools).

    Mirrors the Triage Agent's ``build_strands_agent``: a ``BedrockModel`` plus the
    Gateway-discovered tools, with ``callback_handler=None`` (request-response, not
    an interactive stream). Only the read-only tool subset is registered on the
    agent (Req 5.1); a write tool is never attached. Strands is imported lazily so
    this module imports cleanly where ``strands`` is absent.

    Args:
        model_id: The Bedrock model id (from ``FORECAST_MODEL_ID``).
        tools: The read-only Gateway-discovered tools to attach to the agent.

    Returns:
        A constructed Strands ``Agent`` instance.
    """
    from strands import Agent
    from strands.models.bedrock import BedrockModel

    model = BedrockModel(model_id=model_id)
    return Agent(model=model, tools=tools, callback_handler=None)


def _select_read_only_tools(discovered_tools: list[Any]) -> list[Any]:
    """Filter discovered Gateway tools down to the read-only advisory subset.

    Keeps only the three read-only tools this advisory agent is permitted to use
    (Req 2.5, 5.1), matching each discovered tool's bare name (the segment after
    the Gateway's ``<target>___`` namespace prefix) against
    :data:`READ_ONLY_TOOL_NAMES`.

    Args:
        discovered_tools: The tools returned by ``GatewayToolClient.discover_tools``.

    Returns:
        The subset of tools whose bare name is a read-only tool.
    """
    selected: list[Any] = []
    for tool in discovered_tools:
        actual = getattr(tool, "tool_name", None) or getattr(tool, "name", None)
        if not isinstance(actual, str) or not actual:
            continue
        bare = actual.rsplit("___", 1)[-1]
        if bare in READ_ONLY_TOOL_NAMES:
            selected.append(tool)
    return selected


def _build_app() -> Any:
    """Construct the ``BedrockAgentCoreApp`` and register its runtime handlers.

    Deferred behind a function so importing ``app`` in a bare test environment
    (no ``bedrock_agentcore``) does not fail; the container calls this at start.

    Returns:
        The constructed ``BedrockAgentCoreApp`` instance.
    """
    from bedrock_agentcore import BedrockAgentCoreApp
    from bedrock_agentcore.runtime.models import PingStatus

    # Patch boto3 once at container start so the DynamoDB pulse-alerts writes and
    # the Bedrock Converse call (created lazily by the forecast.* modules) attach
    # as X-Ray subsegments (Design: Observability -> Tracing). No-op if the SDK
    # is absent, so this stays safe outside the container.
    _patch_boto3_for_tracing()

    core_app: Any = BedrockAgentCoreApp()

    @core_app.ping
    def ping_handler() -> Any:
        """AgentCore health check (GET /ping).

        Returns:
            ``PingStatus.HEALTHY`` - the forecasting runtime is stateless.
        """
        return PingStatus.HEALTHY

    @core_app.entrypoint
    def invoke(payload: dict[str, Any], context: Any = None) -> dict[str, Any]:
        """AgentCore Runtime entrypoint (POST /invocations).

        Opens the shared Gateway MCP connection (via the shared
        ``pulse.ops_read.gateway.GatewayToolClient``), builds the Strands agent
        over the read-only tool subset, and runs one forecasting session. Any
        failure is contained inside
        :func:`run_forecast_session` and returned as a structured summary.

        Args:
            payload: The ``{propertyId}`` session input.
            context: The AgentCore runtime context (unused).

        Returns:
            The run-summary dict from :func:`run_forecast_session`.
        """
        # Import the shared Gateway scaffold from the shared ``pulse`` package
        # (already staged into this image at /app/pulse) lazily, so it and its
        # heavy transitive deps are not required to import this module in tests.
        # NOTE: this MUST NOT reach across to the sibling triage-agent service
        # directory - that path does not exist inside the forecast container and
        # caused an unlogged 500 at first invocation (see docs/bugs.md BUG-006).
        from pulse.ops_read.gateway import GatewayToolClient

        # Continue the incoming trace header so this session joins the upstream
        # trace (Scheduler -> invoker Lambda -> AgentCore Runtime). No-op when the
        # X-Ray SDK is absent.
        trace_header = _extract_trace_header(payload, context)
        trace_segment = _begin_trace_segment(trace_header)
        try:
            config = config_module.load_config()
            with GatewayToolClient(
                config.gateway_endpoint_url, "us-east-1", GATEWAY_AWS_SERVICE
            ) as gateway:
                discovered = gateway.discover_tools()
                read_only_tools = _select_read_only_tools(discovered)
                # Build the Strands agent over ONLY the read-only tools (Req 5.1).
                # The agent is used by the narrative model wiring; fact-gathering
                # is driven deterministically through gateway.call_tool.
                build_forecast_agent(config.model_id, read_only_tools)
                # Wrap the tool seam so each Gateway MCP call carries the trace
                # header, linking the Gateway/tool hop into this session's trace.
                summary = run_forecast_session(
                    payload,
                    call_tool=_traced_tool_caller(gateway.call_tool),
                    config=config,
                )
            # Annotate the trace with the run's propertyId (indexed/filterable)
            # and record the run-summary counts as metadata for correlation.
            _annotate_property(summary.get("propertyId"))
            _annotate_run_summary(summary)
            return summary
        finally:
            _end_trace_segment(trace_segment, trace_header)

    return core_app


if __name__ == "__main__":
    # AgentCore Runtime starts Uvicorn on port 8080 with /ping and /invocations.
    logger.info("Starting PULSE forecasting agent server on AgentCore Runtime")
    app = _build_app()
    app.run()
