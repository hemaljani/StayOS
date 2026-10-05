"""Reconcile step Lambda for the StayOS Unified Data Orchestrator.

Reconcile room status against the new ``referenceDate`` (CHECKED_IN today,
OPEN/IN_PROGRESS work orders). This step (Task 3) invokes the Task 1
``reconcile_room_status`` generator via :mod:`generation_runner`, passing the
resolved ``reference_date`` and returning real per-status reconciliation counts.

The rooms table name is read from an environment variable (PYQUALITY-06 /
NAMING). Reconciliation issues only per-item UpdateItem calls, so re-running
with the same reference date converges to the same statuses (idempotent,
Requirement 2.4).

Satisfies: Requirements 1.1, 1.5, 2.2, 2.3, 9.1, 9.2.
Supports: Properties 1, 2.
"""

from __future__ import annotations

from typing import Any, Dict, List

from aws_lambda_powertools import Logger

from generation_runner import run_reconcile
from orchestrator_common import (
    SERVICE_NAME,
    StepInput,
    build_step_result,
    parse_step_input,
    resolve_target_properties,
)

logger = Logger(service=SERVICE_NAME)

STEP_NAME = "Reconcile"


def reconcile_status(step_input: StepInput, pilot_property_ids: List[str]) -> Dict[str, Any]:
    """Reconcile room status against the reference date.

    Rebuilds the deterministic window for the resolved ``reference_date`` and
    applies ``reconcile_room_status`` so rooms reflect that date's CHECKED_IN
    reservations and OPEN/IN_PROGRESS work orders (Requirement 2.1).

    Args:
        step_input: The parsed step input (mode, propertyId, referenceDate).
        pilot_property_ids: Full pilot property list for ``seed`` fan-out
            reporting.

    Returns:
        Structured detail: target properties and rooms reconciled per resolved
        status.
    """
    targets = resolve_target_properties(step_input, pilot_property_ids)
    logger.info(
        "reconciling room status",
        extra={
            "step": STEP_NAME,
            "targetProperties": targets,
            **step_input.to_context(),
        },
    )

    counts = run_reconcile(step_input.reference_date)

    return {
        "targetProperties": targets,
        "reconciledCounts": counts,
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Thin Reconcile handler: parse input, delegate, wrap the result.

    Args:
        event: Step Functions state input carrying the step contract.
        context: Lambda context object.

    Returns:
        A serialized step-result envelope for the next state.
    """
    step_input = parse_step_input(event)
    # Reconciliation operates on the full deterministic window; property scoping
    # is reported for observability. Pilot list is empty here because reconcile
    # does not fan out writes per property (it converges the whole estate).
    details = reconcile_status(step_input, pilot_property_ids=[])
    return build_step_result(
        step=STEP_NAME,
        step_input=step_input,
        summary="Reconciled room status against reference date",
        details=details,
    )


lambda_handler = logger.inject_lambda_context(lambda_handler)  # type: ignore[assignment]
