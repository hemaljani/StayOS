"""Roll-forward starter Lambda for the StayOS Unified Data Orchestrator.

This thin handler is the target of the per-property EventBridge Scheduler rules
(one per pilot property, firing at that property's local midnight). It exists so
the daily roll-forward start path runs through the per-property concurrency
guard (Requirement 1.6): rather than the schedule starting the state machine
directly with an auto-generated (always-unique) execution name, the schedule
invokes this Lambda, which derives a *deterministic* execution name from
``{propertyId + referenceDate}`` and starts the orchestrator via
:func:`concurrency_guard.start_guarded_roll_forward`. A second fire for the same
property/day collides with ``ExecutionAlreadyExists`` and is logged + skipped
instead of running an overlapping roll-forward.

The state-machine ARN is read from the environment (PYQUALITY-06); when the
schedule payload omits ``referenceDate`` it defaults to UTC today via the shared
resolver (the per-property local date is supplied by the schedule input when
present).

Satisfies: Requirements 1.4 (single-property roll-forward), 1.6 (per-property
concurrency guard + skip logging), 9.1/9.2/9.3 (structured start decision that
records a skip rather than masking it).
"""

from __future__ import annotations

import os
from typing import Any, Dict

import boto3
from aws_lambda_powertools import Logger
from botocore.config import Config

from concurrency_guard import start_guarded_roll_forward
from orchestrator_common import (
    MODE_ROLL_FORWARD,
    SERVICE_NAME,
    StepInput,
    parse_step_input,
    resolve_reference_date,
)

logger = Logger(service=SERVICE_NAME)

# State-machine ARN from the environment (NAMING / PYQUALITY-06); never hardcoded.
ENV_STATE_MACHINE_ARN = "STATE_MACHINE_ARN"
# Optional prefix override for the deterministic execution name.
ENV_EXECUTION_NAME_PREFIX = "EXECUTION_NAME_PREFIX"
DEFAULT_EXECUTION_NAME_PREFIX = "rf"

# Module-level client with an explicit standard-mode retry config (PYQUALITY-06),
# created once per container for connection reuse across warm invocations.
_SFN_CLIENT = boto3.client(
    "stepfunctions",
    config=Config(retries={"mode": "standard", "max_attempts": 5}),
)


def _coerce_roll_forward_input(event: Dict[str, Any]) -> StepInput:
    """Parse the schedule event into a validated roll-forward StepInput.

    The EventBridge Scheduler payload carries ``{mode:"roll-forward",
    propertyId}`` and optionally ``referenceDate``. This forces ``mode`` to
    ``roll-forward`` (the only mode this starter serves) and resolves the
    reference date via the shared resolver so an omitted date defaults to UTC
    today.

    Args:
        event: The raw schedule/manual invocation event.

    Returns:
        A validated single-property :class:`StepInput`.

    Raises:
        OrchestratorInputError: If ``propertyId`` is missing or the reference
            date is malformed.
    """
    normalized = dict(event)
    normalized["mode"] = MODE_ROLL_FORWARD
    # Resolve here too so the deterministic name uses the same date the steps do.
    normalized["referenceDate"] = resolve_reference_date(event.get("referenceDate"))
    return parse_step_input(normalized)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Start a guarded, single-property roll-forward.

    Parses the schedule event, resolves the state-machine ARN from the
    environment, and starts the orchestrator through the concurrency guard. An
    overlapping run for the same property/day is skipped (logged, not raised),
    so the returned decision distinguishes ``started`` from ``skipped``
    (Requirement 1.6).

    Args:
        event: The EventBridge Scheduler (or manual) invocation event carrying
            ``{propertyId, referenceDate?}``.
        context: The Lambda context object.

    Returns:
        A small decision dict: ``{decision, executionName, executionArn,
        propertyId, referenceDate}``.
    """
    step_input = _coerce_roll_forward_input(event)
    state_machine_arn = os.environ[ENV_STATE_MACHINE_ARN]
    name_prefix = os.environ.get(ENV_EXECUTION_NAME_PREFIX, DEFAULT_EXECUTION_NAME_PREFIX)

    decision = start_guarded_roll_forward(
        sfn_client=_SFN_CLIENT,
        state_machine_arn=state_machine_arn,
        step_input=step_input,
        name_prefix=name_prefix,
    )

    return {
        "decision": decision.decision,
        "executionName": decision.execution_name,
        "executionArn": decision.execution_arn,
        "propertyId": decision.property_id,
        "referenceDate": decision.reference_date,
    }


lambda_handler = logger.inject_lambda_context(lambda_handler)  # type: ignore[assignment]
