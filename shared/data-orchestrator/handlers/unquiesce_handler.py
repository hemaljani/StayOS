"""UnQuiesce step Lambda for the StayOS Unified Data Orchestrator.

Resume PULSE rule-engine evaluation after reconciliation (Requirement 5.4). This
step is ALSO the target of the state machine ``Catch`` so PULSE is never left
permanently suppressed on failure (Requirement 5.5).

Boundary: the mechanism is owned by PULSE (design "Component 3"); this step only
*calls* the PULSE seam ``unquiesce_rule_engine`` via :mod:`pulse_quiesce_shim`.
That seam re-enables the rule-engine stream event-source-mapping(s) and, per
Requirement 5.5, retries with bounded attempts and emits a CRITICAL log on
continued failure instead of raising -- so this handler always completes its
result envelope even on the Catch path. It degrades to a structured no-op when
the seam is unavailable or its ESM UUIDs are not configured in this environment.

Satisfies: Requirements 1.1, 1.5, 5.4, 5.5, 9.1, 9.2.
"""

from __future__ import annotations

from typing import Any, Dict

from aws_lambda_powertools import Logger

import pulse_quiesce_shim
from orchestrator_common import (
    SERVICE_NAME,
    StepInput,
    build_step_result,
    parse_step_input,
)

logger = Logger(service=SERVICE_NAME)

STEP_NAME = "UnQuiesce"

# Marker returned when the PULSE seam is not wired in this environment.
_NOOP_MECHANISM = "noop-seam-unavailable"


def unquiesce_pulse(step_input: StepInput) -> Dict[str, Any]:
    """Resume PULSE rule-engine evaluation.

    Delegates to the PULSE-owned seam (``unquiesce_rule_engine``), which
    re-enables the rule-engine stream event-source-mapping(s) so normal
    evaluation resumes and subsequent genuine changes fire again (Requirement
    5.4). The seam itself owns the retry and the CRITICAL log on continued
    failure so PULSE is never left permanently suppressed (Requirement 5.5); it
    returns rather than raising, so this handler always completes even on the
    ``Catch`` path.

    Degrades to a structured no-op when the seam is unavailable or its ESM UUIDs
    are not configured in this environment.

    Args:
        step_input: The parsed step input.

    Returns:
        Structured detail describing the un-quiesce action taken, always
        including ``quiesced`` (False once evaluation is resumed) and
        ``mechanism``.
    """
    if not pulse_quiesce_shim.SEAM_AVAILABLE or pulse_quiesce_shim.unquiesce_rule_engine is None:
        logger.warning(
            "PULSE un-quiesce seam unavailable; nothing to resume (no-op)",
            extra={"step": STEP_NAME, **step_input.to_context()},
        )
        return {"quiesced": False, "mechanism": _NOOP_MECHANISM, "uuids": []}

    try:
        result = pulse_quiesce_shim.unquiesce_rule_engine()
    except pulse_quiesce_shim.QuiesceError as exc:  # type: ignore[misc]
        # Only "no UUIDs configured" reaches here (the seam swallows toggle
        # failures with a CRITICAL log rather than raising). Treat unconfigured
        # as a no-op so the Catch path completes cleanly.
        logger.warning(
            "PULSE un-quiesce not configured; nothing to resume (no-op)",
            extra={"step": STEP_NAME, "detail": exc.message, **step_input.to_context()},
        )
        return {"quiesced": False, "mechanism": _NOOP_MECHANISM, "uuids": []}

    logger.info(
        "resumed PULSE rule engine",
        extra={
            "step": STEP_NAME,
            "mechanism": result.get("mechanism"),
            "attempts": result.get("attempts"),
            "stillDisabled": result.get("failed"),
            **step_input.to_context(),
        },
    )
    return result


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Thin UnQuiesce handler: parse input, delegate, wrap the result.

    Also reachable via the state machine ``Catch`` path; it must succeed even
    when an upstream step failed, so it depends only on the step contract and
    the seam never raises on a toggle failure (Requirement 5.5).

    Args:
        event: Step Functions state input carrying the step contract.
        context: Lambda context object.

    Returns:
        A serialized step-result envelope for the next state.
    """
    step_input = parse_step_input(event)
    details = unquiesce_pulse(step_input)
    return build_step_result(
        step=STEP_NAME,
        step_input=step_input,
        summary="PULSE rule engine resumed after roll-forward",
        details=details,
    )


lambda_handler = logger.inject_lambda_context(lambda_handler)  # type: ignore[assignment]
