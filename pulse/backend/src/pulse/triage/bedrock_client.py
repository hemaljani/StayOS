"""Bedrock invocation and triage-brief generation for the Triage Agent.

This module owns the Bedrock I/O and the tier latency budget; the parsing and
validation of the model output (Property 18) and the structural specializations
(Walk Strategy, complaint/OOO options) live in sibling pure modules so this
seam stays thin and the logic stays unit-testable without a live model.

Flow of :func:`generate_triage_brief`:
    1. Render the per-alert-type strict-JSON prompt (:mod:`pulse.triage.prompts`).
    2. Invoke Bedrock through the injectable ``invoker`` seam, measuring elapsed
       time against the tier latency target (CRITICAL 5 s, WARNING 15 s). If the
       budget is exceeded, or the model errors, a :class:`TriageFailure` is
       raised so the Rule Engine delivers the alert without a brief and records
       a triage-failure event (Requirements 1.7, 10.6).
    3. Parse and validate the JSON into a :class:`TriageBrief`, then apply the
       alert-type specialization (attach the Walk_Strategy, enforce complaint
       option bounds, or assemble matched OOO replacement options).

The Bedrock model id is read from the ``TRIAGE_MODEL_ID`` environment variable
(never hardcoded; the approved model can change without code, design Decision
5). The boto3 ``bedrock-runtime`` client comes from the shared cached factory
with the adaptive-retry ``Config`` (PYQUALITY-06).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Optional

from pulse.common.config import ENV_TRIAGE_MODEL_ID, get_optional_env
from pulse.common.errors import ConfigurationError, TriageFailure
from pulse.common.logging import get_logger
from pulse.common.models import AlertDraft, AlertTier, AlertType, TriageBrief
from pulse.triage import prompts
from pulse.triage.context import SituationContext
from pulse.triage.specializations import (
    build_complaint_options,
    build_ooo_replacement_options,
    build_vip_options,
    build_walk_strategy,
)
from pulse.triage.validation import (
    parse_and_validate_brief,
    parse_summary_and_confidence,
)

logger = get_logger("pulse-triage-agent")

# Tier latency targets for producing a triage brief (Requirement 10.6). If the
# model does not respond within the tier's budget, the brief is treated as
# failed so the alert is delivered without one.
TIER_LATENCY_BUDGET_SEC: dict[AlertTier, float] = {
    AlertTier.CRITICAL: 5.0,
    AlertTier.WARNING: 15.0,
    # INFO opportunity briefs (premium cancellation, VIP check-in) are advisory
    # and not latency-critical; give them the same headroom as WARNING. The
    # AgentCore runtime neutralizes this internal budget with a frozen clock and
    # enforces its own relaxed budget (see services/triage-agent/server.py).
    AlertTier.INFO: 15.0,
}

# The INFO alert types that receive an advisory "opportunity" brief. Unlike the
# CRITICAL/WARNING types, these have no deterministic specialization and no
# executable write-back; the model's schema-validated ranked options are the
# whole brief.
_ADVISORY_INFO_TYPES: frozenset[AlertType] = frozenset(
    {AlertType.PREMIUM_CANCELLATION, AlertType.VIP_CHECKIN}
)

# A Bedrock invoker takes (model_id, prompt) and returns the raw model text.
# Injectable so tests never call a live model; ``None`` uses the default
# converse-based invoker.
BedrockInvoker = Callable[[str, str], str]


def _resolve_model_id() -> str:
    """Return the configured Bedrock model id.

    Returns:
        The value of the ``TRIAGE_MODEL_ID`` environment variable.

    Raises:
        ConfigurationError: If the variable is unset (a deploy/config bug).
    """
    model_id = get_optional_env(ENV_TRIAGE_MODEL_ID)
    if not model_id:
        raise ConfigurationError(
            "TRIAGE_MODEL_ID is not set; the Triage Agent cannot select a model",
            variable=ENV_TRIAGE_MODEL_ID,
        )
    return model_id


def _default_bedrock_invoker(model_id: str, prompt: str) -> str:
    """Invoke Bedrock via the Converse API and return the model's text output.

    Uses the model-agnostic Converse API so the approved model id can be swapped
    without code changes. Deterministic settings (temperature 0) are used so a
    triage brief is reproducible for a given situation.

    Args:
        model_id: The Bedrock model id to invoke.
        prompt: The rendered strict-JSON prompt.

    Returns:
        The raw text content of the model's response.

    Raises:
        TriageFailure: If Bedrock returns an error or an unexpected shape.
    """
    from pulse.common.aws import get_client

    client = get_client("bedrock-runtime")
    try:
        # Converse: send a single user turn; request low-temperature, bounded
        # output so the model returns compact strict JSON.
        response = client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 1024, "temperature": 0.0},
        )
    except client.exceptions.ClientError as exc:  # pragma: no cover - live path
        raise TriageFailure(
            f"Bedrock invocation failed: {exc}", reason="bedrock_error"
        ) from exc
    try:
        return response["output"]["message"]["content"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:  # pragma: no cover - live path
        raise TriageFailure(
            "Bedrock response had an unexpected shape", reason="bedrock_error"
        ) from exc


def _invoke_within_budget(
    model_id: str,
    prompt: str,
    tier: AlertTier,
    invoker: BedrockInvoker,
    clock: Callable[[], float],
) -> str:
    """Invoke the model and enforce the tier latency budget.

    Args:
        model_id: The Bedrock model id.
        prompt: The rendered prompt.
        tier: The alert tier (selects the latency budget).
        invoker: The Bedrock invoker seam.
        clock: A monotonic clock, injectable for tests.

    Returns:
        The raw model text.

    Raises:
        TriageFailure: If the invoker errors, or the elapsed time exceeds the
            tier latency target (Requirement 10.6).
    """
    budget = TIER_LATENCY_BUDGET_SEC.get(tier)
    start = clock()
    try:
        raw = invoker(model_id, prompt)
    except TriageFailure:
        raise
    except Exception as exc:  # noqa: BLE001 - any model error becomes a triage failure
        raise TriageFailure(
            f"Triage model invocation error: {exc}", reason="bedrock_error"
        ) from exc
    elapsed = clock() - start
    if budget is not None and elapsed > budget:
        raise TriageFailure(
            f"Triage exceeded the {tier.value} latency target of {budget}s "
            f"(took {elapsed:.3f}s)",
            reason="timeout",
        )
    return raw


def _extract_json_text(raw: str) -> str:
    """Recover a JSON object substring from raw model text (tolerant).

    LLMs frequently wrap strict-JSON output in markdown code fences
    (```json ... ```) or add a lead-in/trailing sentence, despite an explicit
    "JSON only" instruction. A Strands Agent that ran tool-use turns is
    especially prone to this. Strict ``json.loads`` on such text fails, which
    caused every triage brief to be dropped as ``invalid_json`` (no "Agent
    ready" badge). This helper defends against those two common shapes:

        1. Strip a fenced code block, preferring a ```json fence, else any
           triple-backtick fence.
        2. If the text is still not pure JSON, slice from the first ``{`` to the
           matching last ``}`` (the outermost object), discarding surrounding
           prose.

    It is deliberately conservative: it only narrows the text; the caller still
    runs ``json.loads`` and fails cleanly if what remains is not valid JSON.

    Args:
        raw: The raw model text.

    Returns:
        The best-effort JSON substring (the input trimmed when nothing better is
        found).
    """
    text = raw.strip()

    # 1. Strip a markdown code fence if present. Handle ```json and bare ```.
    if text.startswith("```"):
        # Drop the opening fence line (``` or ```json) ...
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1 :]
        # ... and a trailing closing fence.
        closing = text.rfind("```")
        if closing != -1:
            text = text[:closing]
        text = text.strip()

    # 2. If prose still surrounds the object, slice the outermost braces.
    if not text.startswith("{"):
        first = text.find("{")
        last = text.rfind("}")
        if first != -1 and last != -1 and last > first:
            text = text[first : last + 1]

    return text.strip()


# Failure reasons that indicate a transient model-output flake (the model
# returned unparseable/invalid content or too few options this turn). These are
# worth ONE automatic retry because a fresh invocation frequently produces a
# valid brief (BUG-021: e.g. the complaint prompt occasionally overshoots or
# emits malformed options, dropping the "Agent ready" badge). Reasons NOT listed
# are not retried: ``bedrock_error`` (infra/throttling - the SDK already retries
# via the adaptive Config), ``timeout`` (retrying would blow the budget further),
# and ``unsupported_type`` (a deterministic logic/config error).
_RETRYABLE_REASONS: frozenset[str] = frozenset(
    {"invalid_json", "invalid_schema", "malformed_option", "insufficient_options"}
)

# One automatic retry (two attempts total) on a retryable failure.
_MAX_TRIAGE_ATTEMPTS = 2


def _parse_json(raw: str) -> Any:
    """Parse raw model text into JSON, tolerating fences / surrounding prose.

    The strict-JSON prompt asks the model for bare JSON, but models often add
    markdown fences or a lead-in sentence anyway. :func:`_extract_json_text`
    recovers the JSON object substring first; only genuinely unparseable output
    fails as ``invalid_json`` (so the alert is delivered without a brief).

    Args:
        raw: The raw model text.

    Returns:
        The decoded JSON structure.

    Raises:
        TriageFailure: If no valid JSON object can be recovered from the text.
    """
    try:
        return json.loads(_extract_json_text(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        raise TriageFailure(
            "Triage model did not return valid JSON", reason="invalid_json"
        ) from exc


def _assemble_brief(
    alert_type: AlertType, raw: Any, context: SituationContext
) -> TriageBrief:
    """Assemble the final brief for an alert type from validated model output.

    Dispatches to the alert-type specialization so structural guarantees are
    enforced deterministically rather than trusted to the model:
        * Walk Risk: validate the base brief, then attach the Walk_Strategy.
        * VIP Room Not Ready: validate the base brief; ensure rush-clean and
          room-move options are present (falling back to the standard pair).
        * Complaint: require a well-formed summary/confidence and build 3-5
          cost/risk options (raising when fewer than 3 exist, Requirement 5.4).
        * OOO Cluster: require a well-formed summary/confidence and assemble up
          to 5 matched replacement options (zero when none, Requirement 7.4),
          which is exempt from the general 2-5 option rule.
        * Premium Cancellation / VIP Check-In (INFO): advisory opportunity
          briefs validated against the standard schema (2-5 ranked options); no
          deterministic specialization and no executable write-back.

    Args:
        alert_type: The alert type being triaged.
        raw: The parsed model output.
        context: The situation context supplying structured facts.

    Returns:
        The assembled, validated :class:`TriageBrief`.

    Raises:
        TriageFailure: If validation or a specialization constraint fails.
    """
    if alert_type is AlertType.WALK_RISK:
        brief = parse_and_validate_brief(raw)
        brief.walk_strategy = build_walk_strategy(context)
        return brief

    if alert_type is AlertType.VIP_ROOM_NOT_READY:
        brief = parse_and_validate_brief(raw)
        # Guarantee a rush-clean and room-move pair regardless of model output.
        if len(brief.options) < 2:
            brief.options = build_vip_options(context)
        return brief

    if alert_type is AlertType.COMPLAINT_ESCALATION:
        summary, confidence = parse_summary_and_confidence(raw)
        candidates = raw.get("options") if isinstance(raw, dict) else None
        options = build_complaint_options(candidates or [])
        return TriageBrief(
            summary=summary,
            confidence=confidence,
            options=options,
            execute_label=raw.get("executeLabel") if isinstance(raw, dict) else None,
        )

    if alert_type is AlertType.OOO_CLUSTER:
        summary, confidence = parse_summary_and_confidence(raw)
        # Requirement 7.4: zero options is valid when no replacement matches, so
        # the general 2-5 rule does not apply here.
        options = build_ooo_replacement_options(context)
        return TriageBrief(summary=summary, confidence=confidence, options=options)

    if alert_type in _ADVISORY_INFO_TYPES:
        # INFO opportunity briefs (premium cancellation, VIP check-in) are
        # advisory: the model produces 2-5 ranked suggestions the GM can act on
        # manually. There is no deterministic specialization and no executable
        # write-back (INFO is excluded from the Action Executor's
        # RESOLVABLE_TYPES), so the standard schema validation is the only
        # structural guarantee needed.
        return parse_and_validate_brief(raw)

    # Any other type is not triage-eligible and should never reach here.
    raise TriageFailure(
        f"Alert type {alert_type.value} is not triage-eligible",
        reason="unsupported_type",
    )


def generate_triage_brief(
    draft: AlertDraft,
    context: SituationContext,
    *,
    invoker: Optional[BedrockInvoker] = None,
    clock: Callable[[], float] = time.monotonic,
) -> TriageBrief:
    """Generate a validated triage brief for an alert draft.

    Renders the alert-type prompt, invokes Bedrock within the tier latency
    budget, and assembles a validated brief with its type specialization.

    Args:
        draft: The alert draft to triage (supplies tier and type).
        context: The situation context supplying structured facts and seams.
        invoker: Optional Bedrock invoker seam; the default Converse-based
            invoker is used when omitted (injected in tests to avoid a live
            model).
        clock: Monotonic clock used to enforce the latency budget; injectable
            for tests.

    Returns:
        A validated :class:`TriageBrief` for the alert.

    Raises:
        TriageFailure: On latency breach, model error, invalid JSON, schema
            violation, or an unsatisfied specialization constraint. The caller
            delivers the alert without a brief (Requirements 1.7, 10.6).
    """
    model_id = _resolve_model_id()
    active_invoker = invoker if invoker is not None else _default_bedrock_invoker
    prompt = prompts.render_prompt(draft.type, context)

    # Attempt generation, retrying ONCE on a transient model-output flake
    # (invalid JSON / schema / malformed or too-few options). Each attempt
    # re-invokes the model within the tier latency budget; non-retryable
    # failures (bedrock_error / timeout / unsupported_type) propagate
    # immediately. This makes the "Agent ready" brief reliably attach despite
    # occasional non-conforming model output (BUG-021).
    last_failure: Optional[TriageFailure] = None
    for attempt in range(1, _MAX_TRIAGE_ATTEMPTS + 1):
        try:
            raw_text = _invoke_within_budget(
                model_id, prompt, draft.tier, active_invoker, clock
            )
            raw = _parse_json(raw_text)
            brief = _assemble_brief(draft.type, raw, context)
        except TriageFailure as failure:
            last_failure = failure
            if failure.reason in _RETRYABLE_REASONS and attempt < _MAX_TRIAGE_ATTEMPTS:
                logger.warning(
                    "Triage attempt failed on a retryable reason; retrying once",
                    extra={
                        "alertId": draft.alert_id,
                        "type": draft.type.value,
                        "attempt": attempt,
                        "reason": failure.reason,
                    },
                )
                continue
            raise
        logger.info(
            "Triage brief generated",
            extra={
                "alertId": draft.alert_id,
                "type": draft.type.value,
                "tier": draft.tier.value,
                "confidence": brief.confidence,
                "optionCount": len(brief.options),
                "attempt": attempt,
            },
        )
        return brief

    # Unreachable in practice (the loop either returns a brief or raises), but
    # satisfies the type checker and guards against a logic change.
    raise last_failure or TriageFailure(
        "Triage failed to produce a brief", reason="invalid_json"
    )


def make_rule_engine_invoker(
    context_provider: Callable[[AlertDraft], SituationContext],
    *,
    invoker: Optional[BedrockInvoker] = None,
) -> Callable[[AlertDraft], Optional[TriageBrief]]:
    """Build an invoker matching the Rule Engine's ``TRIAGE_INVOKER`` seam.

    The Rule Engine's ``route_for_triage`` calls a ``Callable[[AlertDraft],
    Optional[TriageBrief]]`` and catches any exception as a triage failure
    (delivering the alert without a brief). This adapter supplies the situation
    context for a draft via ``context_provider`` and delegates to
    :func:`generate_triage_brief`.

    Wire it at Lambda cold start with, e.g.::

        from pulse.rule_engine import handler
        handler.TRIAGE_INVOKER = make_rule_engine_invoker(build_context)

    Args:
        context_provider: Builds a :class:`SituationContext` for an alert draft
            (typically from SPOG lookups).
        invoker: Optional Bedrock invoker seam forwarded to
            :func:`generate_triage_brief`.

    Returns:
        A callable suitable for ``pulse.rule_engine.handler.TRIAGE_INVOKER``.
    """

    def _invoke(draft: AlertDraft) -> Optional[TriageBrief]:
        context = context_provider(draft)
        return generate_triage_brief(draft, context, invoker=invoker)

    return _invoke


__all__ = [
    "TIER_LATENCY_BUDGET_SEC",
    "BedrockInvoker",
    "generate_triage_brief",
    "make_rule_engine_invoker",
]
