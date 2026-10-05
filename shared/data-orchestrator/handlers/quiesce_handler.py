"""Quiesce step Lambda for the StayOS Unified Data Orchestrator.

Bounded, reversible suppression of PULSE rule-engine evaluation so the daily
bulk roll-forward does not emit an alert storm (Requirements 5.1, 5.3).

Boundary: the suppression MECHANISM is owned by PULSE (design "Component 3:
PULSE quiesce seam"); this step only *calls* that seam via
:mod:`pulse_quiesce_shim`. PULSE chose the preferred mechanism -- toggling the
rule-engine DynamoDB Streams event-source-mapping(s) disabled around the rewrite
window (see ``pulse/backend/src/pulse/rule_engine/quiesce.py``) -- which pauses
consumption without disabling the DynamoDB stream itself (Requirement 8.3).

When the PULSE seam is importable AND its ESM UUIDs are configured (via the
``RULE_ENGINE_ESM_UUIDS`` environment variable injected on this Lambda), this
step disables those mappings. When the seam is unavailable or unconfigured (for
example an orchestrator-only unit environment), it degrades to a structured
no-op so the state machine contract still holds.

Satisfies: Requirements 1.1, 1.5, 5.1, 5.2, 5.3, 9.1, 9.2.
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

STEP_NAME = "Quiesce"

# Marker returned when the PULSE seam is not wired in this environment, so the
# scaffold contract (quiesced True) still holds without a real ESM toggle.
_NOOP_MECHANISM = "noop-seam-unavailable"


def quiesce_pulse(step_input: StepInput) -> Dict[str, Any]:
    """Suppress PULSE rule-engine evaluation for the roll-forward window.

    Delegates to the PULSE-owned seam (``quiesce_rule_engine``), which disables
    the rule-engine stream event-source-mapping(s) so bulk upserts are not
    evaluated and produce zero alerts (Requirements 5.1, 5.3). Degrades to a
    structured no-op when the seam is unavailable or its ESM UUIDs are not
    configured in this environment.

    Args:
        step_input: The parsed step input (mode, property, reference date).

    Returns:
        Structured detail describing the quiesce action taken, always including
        ``quiesced`` (True when suppression is in effect) and ``mechanism``.
    """
    if not pulse_quiesce_shim.SEAM_AVAILABLE or pulse_quiesce_shim.quiesce_rule_engine is None:
        logger.warning(
            "PULSE quiesce seam unavailable; skipping suppression (no-op)",
            extra={"step": STEP_NAME, **step_input.to_context()},
        )
        return {"quiesced": True, "mechanism": _NOOP_MECHANISM, "uuids": []}

    try:
        result = pulse_quiesce_shim.quiesce_rule_engine()
    except pulse_quiesce_shim.QuiesceError as exc:  # type: ignore[misc]
        # Not configured in this environment (no ESM UUIDs): treat as a no-op so
        # the orchestrator contract holds. A genuine partial-disable failure
        # re-raises below so the state machine Catch runs Un-Quiesce.
        if getattr(exc, "uuids", None):
            logger.error(
                "PULSE quiesce failed to disable mapping(s); propagating",
                extra={"step": STEP_NAME, "uuids": exc.uuids, **step_input.to_context()},
            )
            raise
        logger.warning(
            "PULSE quiesce not configured; skipping suppression (no-op)",
            extra={"step": STEP_NAME, "detail": exc.message, **step_input.to_context()},
        )
        return {"quiesced": True, "mechanism": _NOOP_MECHANISM, "uuids": []}

    logger.info(
        "quiesced PULSE rule engine",
        extra={"step": STEP_NAME, "mechanism": result.get("mechanism"), **step_input.to_context()},
    )
    return result


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Thin Quiesce handler: parse input, delegate, wrap the result.

    Args:
        event: Step Functions state input carrying ``{mode, propertyId,
            referenceDate}`` plus any accumulated state.
        context: Lambda context object (used by the Powertools decorator).

    Returns:
        A serialized step-result envelope for the next state.
    """
    step_input = parse_step_input(event)
    details = quiesce_pulse(step_input)
    return build_step_result(
        step=STEP_NAME,
        step_input=step_input,
        summary="PULSE rule engine quiesced for roll-forward window",
        details=details,
    )


# Attach the Powertools context injector without shadowing the plain function,
# so unit tests can import and call lambda_handler directly.
lambda_handler = logger.inject_lambda_context(lambda_handler)  # type: ignore[assignment]
