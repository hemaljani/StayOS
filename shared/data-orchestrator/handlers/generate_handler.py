"""Generate step Lambda for the StayOS Unified Data Orchestrator.

Window re-anchor + idempotent upsert of the LUMI operational tables to a new
``referenceDate``. This step (Task 3) invokes the Task 1 ``dataset_generator``
generators via :mod:`generation_runner`, passing the resolved
``reference_date`` and writing with Idempotent_Upsert so a re-run is a no-op.

It supports both orchestration modes:

* ``seed`` fans out over the full pilot estate (Requirement 1.3) and, before
  the dataset fan-out, drives the full first-deploy application seed (Cognito
  users, settings, schedules, historical briefs, PULSE rules / kitchen) by
  reusing the existing idempotent LUMI/PULSE seed Lambdas via
  :mod:`seed_provisioning` - never duplicating that logic.
* ``roll-forward`` scopes reported counts to a single property (Requirement 1.4)
  and NEVER touches application data (upsert-only dataset window, Requirement
  8.1).

Table names come exclusively from environment variables (PYQUALITY-06 / NAMING).

Runtime note: the deployment packages the ``dataset_generator`` package
alongside these handlers (or sets ``DATASET_GENERATOR_PATH``); see
``dataset_generator_shim`` for import resolution.

Satisfies: Requirements 1.1, 1.3, 1.4, 1.5, 2.2, 2.3, 9.1, 9.2.
Supports: Properties 1, 2.
"""

from __future__ import annotations

from typing import Any, Dict, List

from aws_lambda_powertools import Logger

from dataset_generator.config import PROPERTY_IDS
from generation_runner import run_generation
from orchestrator_common import (
    MODE_SEED,
    SERVICE_NAME,
    StepInput,
    build_step_result,
    parse_step_input,
    resolve_target_properties,
)
from seed_provisioning import provision_application_seed

logger = Logger(service=SERVICE_NAME)

STEP_NAME = "Generate"

# Full pilot property estate, sourced from the generator config (never
# hardcoded here) so seed fan-out and roll-forward scoping stay in sync with
# the generators themselves.
PILOT_PROPERTY_IDS: List[str] = list(PROPERTY_IDS)


def generate_window(step_input: StepInput, pilot_property_ids: List[str]) -> Dict[str, Any]:
    """Re-anchor and idempotently upsert the operational window.

    Resolves the target properties for the mode, runs the deterministic
    generator pipeline against the resolved ``reference_date`` with
    Idempotent_Upsert writes, and returns real per-table item counts scoped to
    the target properties.

    Args:
        step_input: The parsed step input (mode, propertyId, referenceDate).
        pilot_property_ids: Full pilot property list for ``seed`` fan-out.

    Returns:
        Structured detail: target properties, per-table item counts, and the
        upsert mode.
    """
    targets = resolve_target_properties(step_input, pilot_property_ids)
    logger.info(
        "generating re-anchored window",
        extra={
            "step": STEP_NAME,
            "targetProperties": targets,
            **step_input.to_context(),
        },
    )

    # First-deploy full seed: mode "seed" owns the WHOLE first-deploy seed
    # (Requirement 1.3), not just the operational dataset window. Before the
    # deterministic dataset fan-out, drive the LUMI + PULSE application seed
    # (Cognito users, settings, schedules, historical briefs, PULSE rules /
    # kitchen) by reusing the existing idempotent seed Lambda rather than
    # duplicating it. The roll-forward path does NOT touch application data, so
    # this runs only in seed mode. The invocation carries no destructive-clear
    # confirmation, so it stays upsert-only (Requirements 8.1, 8.2).
    application_seed: Dict[str, Any] = {}
    if step_input.mode == MODE_SEED:
        application_seed = provision_application_seed(request_type="Create")

    result = run_generation(step_input.reference_date, target_properties=targets)

    return {
        "targetProperties": targets,
        "perTableCounts": result.per_table_counts,
        "generatedCounts": result.generated_counts,
        "upsertMode": "idempotent",
        "applicationSeed": application_seed,
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Thin Generate handler: parse input, delegate, wrap the result.

    Args:
        event: Step Functions state input carrying the step contract.
        context: Lambda context object.

    Returns:
        A serialized step-result envelope for the next state.
    """
    step_input = parse_step_input(event)
    details = generate_window(step_input, pilot_property_ids=PILOT_PROPERTY_IDS)
    return build_step_result(
        step=STEP_NAME,
        step_input=step_input,
        summary="Re-anchored operational window via idempotent upsert",
        details=details,
    )


lambda_handler = logger.inject_lambda_context(lambda_handler)  # type: ignore[assignment]
