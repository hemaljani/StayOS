"""PrimeBaseline step Lambda for the StayOS Unified Data Orchestrator.

Prime a curated, deterministic PULSE alert baseline per property (reset-then-
prime, bounded, no unbounded growth) after Un-Quiesce, so a presenter opening
PULSE always sees a predictable populated feed (Requirement 6).

The baseline itself is owned by PULSE (design "Component 5"): this thin handler
resolves the target properties and the ``pulse-alerts`` table name from the
environment, then delegates to the PULSE-owned builder through the
``pulse_baseline_shim`` seam. When the PULSE package is not importable (an
isolated orchestrator-only environment) or the alerts table name is not
configured, the step degrades to a structured no-op rather than failing the
workflow -- mirroring how the Quiesce/UnQuiesce steps degrade when their seam is
unavailable. The real builder writes the deterministic baseline directly to
``pulse-alerts`` (implementation choice (b)), so it never depends on the quiesced
bulk stream fallout (Requirement 6.4).

Satisfies: Requirements 1.1, 1.5, 6.1, 6.2, 6.3, 6.4, 9.1, 9.2.
"""

from __future__ import annotations

import os
from typing import Any

import pulse_baseline_shim
from aws_lambda_powertools import Logger
from orchestrator_common import (
    SERVICE_NAME,
    StepInput,
    build_step_result,
    parse_step_input,
    resolve_target_properties,
)

logger = Logger(service=SERVICE_NAME)

STEP_NAME = "PrimeBaseline"

# Environment variable carrying the pulse-alerts physical table name. Matches
# pulse.common.config.ENV_ALERTS_TABLE so the orchestrator and the PULSE read
# path agree on the same table (PYQUALITY-06 / NAMING; never hardcoded).
ALERTS_TABLE_ENV = "ALERTS_TABLE_NAME"


def _resolve_pilot_property_ids() -> list[str]:
    """Resolve the full pilot property list for ``seed`` fan-out.

    Sourced from the LUMI generator config (never hardcoded here) so seed fan-out
    stays in sync with the rest of the orchestrator. Falls back to an empty list
    when the generator package is not importable (orchestrator-only tests), which
    makes ``seed`` mode a bounded no-op rather than a crash.

    Returns:
        The ordered pilot property ids, or an empty list when unresolved.
    """
    try:
        from dataset_generator.config import PROPERTY_IDS

        return list(PROPERTY_IDS)
    except ImportError:
        logger.warning(
            "dataset_generator config unavailable; seed fan-out targets no "
            "properties for baseline priming",
            extra={"step": STEP_NAME},
        )
        return []


def prime_baseline(step_input: StepInput, pilot_property_ids: list[str]) -> dict[str, Any]:
    """Prime the curated PULSE baseline for the target properties.

    For each target property, delegates to the PULSE-owned reset-then-prime
    builder via the ``pulse_baseline_shim`` seam. When the seam is unavailable or
    the alerts table name is unset, returns a structured no-op result (zeroed
    counts) so the workflow still completes and the condition is observable in
    the step summary (Requirement 9.2/9.3).

    Args:
        step_input: The parsed step input (mode, propertyId, referenceDate).
        pilot_property_ids: Full pilot property list for ``seed`` fan-out.

    Returns:
        Structured detail: per-property curated baseline counts and the total
        ``baselineAlertsPrimed`` across all target properties.
    """
    targets = resolve_target_properties(step_input, pilot_property_ids)
    alerts_table_name = os.environ.get(ALERTS_TABLE_ENV)

    if not pulse_baseline_shim.SEAM_AVAILABLE or pulse_baseline_shim.prime_property_baseline is None:
        logger.warning(
            "PULSE baseline seam unavailable; priming is a structured no-op",
            extra={
                "step": STEP_NAME,
                "targetProperties": targets,
                "seamAvailable": pulse_baseline_shim.SEAM_AVAILABLE,
                **step_input.to_context(),
            },
        )
        return {
            "targetProperties": targets,
            "baselineAlertsPrimed": 0,
            "seamAvailable": False,
            "perProperty": [],
        }

    if not alerts_table_name:
        logger.warning(
            "ALERTS_TABLE_NAME is not set; baseline priming is a structured no-op",
            extra={"step": STEP_NAME, "targetProperties": targets, **step_input.to_context()},
        )
        return {
            "targetProperties": targets,
            "baselineAlertsPrimed": 0,
            "seamAvailable": True,
            "perProperty": [],
        }

    logger.info(
        "priming curated PULSE baseline",
        extra={
            "step": STEP_NAME,
            "targetProperties": targets,
            "alertsTable": alerts_table_name,
            **step_input.to_context(),
        },
    )

    per_property: list[dict[str, Any]] = []
    total_primed = 0
    for property_id in targets:
        summary = pulse_baseline_shim.prime_property_baseline(
            property_id,
            alerts_table_name=alerts_table_name,
        )
        per_property.append(summary)
        total_primed += int(summary.get("baselineAlertsPrimed", 0))

    return {
        "targetProperties": targets,
        "baselineAlertsPrimed": total_primed,
        "seamAvailable": True,
        "perProperty": per_property,
    }


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Thin PrimeBaseline handler: parse input, delegate, wrap the result.

    This is the terminal step of the happy path; its result closes the
    per-execution summary.

    Args:
        event: Step Functions state input carrying the step contract.
        context: Lambda context object.

    Returns:
        A serialized step-result envelope terminating the workflow.
    """
    step_input = parse_step_input(event)
    details = prime_baseline(step_input, pilot_property_ids=_resolve_pilot_property_ids())
    return build_step_result(
        step=STEP_NAME,
        step_input=step_input,
        summary="Primed curated deterministic PULSE baseline",
        details=details,
    )


lambda_handler = logger.inject_lambda_context(lambda_handler)  # type: ignore[assignment]
