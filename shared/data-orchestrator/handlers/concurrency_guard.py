"""Per-property concurrency guard for the StayOS Unified Data Orchestrator.

Requirement 1.6 states: if two executions for the same ``propertyId`` overlap,
the orchestrator SHALL prevent concurrent Roll-Forward for that property and log
the skipped run. This module implements that guard on the *start* path.

Mechanism (chosen: deterministic execution name)
-------------------------------------------------
Rather than introduce a DynamoDB lock item (an extra table, a TTL, and a
read-modify-write race window to reason about), the guard derives a
**deterministic Step Functions execution name** from ``{propertyId +
referenceDate}``. Step Functions enforces execution-name uniqueness natively:
a second ``StartExecution`` with a name already used (within the service's
retention window) is rejected atomically with ``ExecutionAlreadyExists``. We
catch that specific typed exception and treat it as a *skip* - logging a
structured "skipped overlapping run" warning and returning without raising -
so an overlapping local-midnight fire (or a manual retrigger) for the same
property/day is a no-op instead of a duplicate roll-forward.

Why this over a lock item:

* No new infrastructure (no lock table, no TTL bookkeeping) - simpler and
  matches the design's "deterministic execution name" option.
* The uniqueness check is atomic inside Step Functions, so there is no
  read-then-write race between two near-simultaneous fires.
* A ``roll-forward`` for the *same* property on a *different* ``referenceDate``
  (the normal daily cadence) yields a different name and proceeds. A *different*
  property on the same day also yields a different name and proceeds.

The date bucket in the name means the guard suppresses overlap *within the same
property-day*, which is exactly the roll-forward cadence (one per property per
local day). The state-machine ARN and any name prefix come from configuration
passed in by the caller (PYQUALITY-06 / NAMING) - nothing is hardcoded here.

Satisfies: Requirement 1.6 (prevent overlapping per-property roll-forward and
log the skipped run) and Requirements 9.1, 9.2, 9.3 (structured logging of the
start decision, including a recorded skip that is not masked as a fresh start).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from aws_lambda_powertools import Logger

from orchestrator_common import (
    MODE_ROLL_FORWARD,
    SERVICE_NAME,
    StepInput,
)

logger = Logger(service=SERVICE_NAME)

# Step Functions execution names must be 1-80 chars and may only contain
# a restricted set; anything outside is replaced so a propertyId with unusual
# characters can never produce an invalid (or injectable) name.
_MAX_EXECUTION_NAME_LEN = 80
_NAME_ALLOWED = re.compile(r"[^A-Za-z0-9_-]")

# Start-decision outcomes returned to the caller (also used in logs/tests).
DECISION_STARTED = "started"
DECISION_SKIPPED = "skipped"


@dataclass(frozen=True)
class StartDecision:
    """Outcome of a guarded ``StartExecution`` attempt.

    Attributes:
        decision: :data:`DECISION_STARTED` when a new execution was started,
            or :data:`DECISION_SKIPPED` when an overlapping run for the same
            property/day already existed (Requirement 1.6).
        execution_name: The deterministic execution name that was used.
        execution_arn: The started (or already-existing) execution ARN when
            available; ``None`` for a skip where the ARN was not returned.
        property_id: The scoped property this decision applies to.
        reference_date: The reference date bucket the name was derived from.
    """

    decision: str
    execution_name: str
    execution_arn: Optional[str]
    property_id: Optional[str]
    reference_date: str

    @property
    def started(self) -> bool:
        """Return whether a fresh execution was actually started."""
        return self.decision == DECISION_STARTED


def _sanitize_token(token: str) -> str:
    """Sanitize one token for safe inclusion in a Step Functions name.

    Args:
        token: A raw token (e.g. a ``propertyId`` or ISO date).

    Returns:
        The token with any character outside ``[A-Za-z0-9_-]`` replaced by
        ``-`` so the composed execution name is always valid.
    """
    return _NAME_ALLOWED.sub("-", token)


def build_execution_name(
    property_id: str, reference_date: str, prefix: str = "rf"
) -> str:
    """Build the deterministic per-property-per-day execution name.

    The name is a function of ``{prefix, propertyId, referenceDate}`` only, so a
    second start for the same property on the same reference date produces the
    identical name and collides with ``ExecutionAlreadyExists`` (the skip
    signal). It is sanitized and kept within the Step Functions 80-character
    limit.

    The reference-date suffix is the guard's per-day discriminator, so it MUST
    always survive. A naive ``raw[:80]`` truncation would slice off the trailing
    ``YYYY-MM-DD`` when a sanitized ``propertyId`` is long, collapsing distinct
    days to one name and causing every subsequent day to be skipped as
    ``ExecutionAlreadyExists`` (review finding CR-7). To prevent that, when the
    full name would exceed the limit we replace the property portion with a
    short deterministic hash of it, keeping both the prefix and the full date
    intact while remaining unique per property.

    Args:
        property_id: The scoped property identifier.
        reference_date: The ISO ``YYYY-MM-DD`` reference date (the day bucket).
        prefix: A short, stable prefix so the name reads clearly in the console.

    Returns:
        A deterministic, valid Step Functions execution name whose date suffix
        is always preserved.
    """
    safe_prefix = _sanitize_token(prefix)
    safe_property = _sanitize_token(property_id)
    safe_date = _sanitize_token(reference_date)

    raw = f"{safe_prefix}-{safe_property}-{safe_date}"
    if len(raw) <= _MAX_EXECUTION_NAME_LEN:
        return raw

    # Too long: preserve prefix + full date, and compress the property portion
    # into a deterministic hash so distinct properties still map to distinct
    # names and the date discriminator is never lost.
    property_hash = hashlib.sha256(property_id.encode("utf-8")).hexdigest()
    # Budget for the property hash = limit minus prefix, date, and 2 separators.
    fixed_len = len(safe_prefix) + len(safe_date) + 2
    hash_budget = _MAX_EXECUTION_NAME_LEN - fixed_len
    if hash_budget < 1:
        # Pathological prefix/date lengths: fall back to a bounded hash of the
        # whole name so we still return a valid, deterministic, unique name.
        whole = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return whole[:_MAX_EXECUTION_NAME_LEN]
    truncated_hash = property_hash[:hash_budget]
    return f"{safe_prefix}-{truncated_hash}-{safe_date}"


def start_guarded_roll_forward(
    sfn_client: Any,
    state_machine_arn: str,
    step_input: StepInput,
    name_prefix: str = "rf",
) -> StartDecision:
    """Start a roll-forward with the per-property concurrency guard applied.

    Derives a deterministic execution name from the property/day and calls
    ``StartExecution``. If Step Functions rejects the name because an execution
    with it already exists (an overlapping run for the same property/day), the
    typed ``ExecutionAlreadyExists`` exception is caught and treated as a skip:
    a structured warning is logged and a :data:`DECISION_SKIPPED` decision is
    returned without raising (Requirement 1.6). Any other error propagates so a
    genuine failure is not masked as a skip (Requirement 9.3).

    Args:
        sfn_client: A boto3 Step Functions client (injected for testability).
        state_machine_arn: The orchestrator state-machine ARN (from config).
        step_input: The parsed step input; must be ``roll-forward`` scoped to a
            single ``property_id``.
        name_prefix: Prefix for the deterministic execution name.

    Returns:
        A :class:`StartDecision` describing whether the run started or was
        skipped as an overlap.

    Raises:
        ValueError: If ``step_input`` is not a single-property roll-forward.
    """
    if step_input.mode != MODE_ROLL_FORWARD or not step_input.property_id:
        # The guard is only meaningful for a single-property roll-forward; a
        # seed fan-out is not subject to per-property overlap suppression.
        raise ValueError(
            "start_guarded_roll_forward requires a roll-forward StepInput with a propertyId"
        )

    property_id = step_input.property_id
    reference_date = step_input.reference_date
    execution_name = build_execution_name(property_id, reference_date, prefix=name_prefix)

    payload = {
        "mode": MODE_ROLL_FORWARD,
        "propertyId": property_id,
        "referenceDate": reference_date,
    }

    log_context = {
        "propertyId": property_id,
        "referenceDate": reference_date,
        "executionName": execution_name,
    }

    try:
        response = sfn_client.start_execution(
            stateMachineArn=state_machine_arn,
            name=execution_name,
            input=json.dumps(payload),
        )
    except sfn_client.exceptions.ExecutionAlreadyExists:
        # Overlapping run for the same property/day: skip, do not error.
        # Recorded (not masked) so the skipped run is observable (Req 1.6/9.3).
        logger.warning(
            "skipped overlapping roll-forward: an execution for this property/day "
            "already exists (concurrency guard)",
            extra={**log_context, "decision": DECISION_SKIPPED},
        )
        return StartDecision(
            decision=DECISION_SKIPPED,
            execution_name=execution_name,
            execution_arn=None,
            property_id=property_id,
            reference_date=reference_date,
        )

    execution_arn = response.get("executionArn")
    logger.info(
        "started roll-forward execution (concurrency guard passed)",
        extra={**log_context, "decision": DECISION_STARTED, "executionArn": execution_arn},
    )
    return StartDecision(
        decision=DECISION_STARTED,
        execution_name=execution_name,
        execution_arn=execution_arn,
        property_id=property_id,
        reference_date=reference_date,
    )
