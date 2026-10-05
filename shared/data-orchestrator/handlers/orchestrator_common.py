"""Shared helpers for the StayOS Unified Data Orchestrator step Lambdas.

This module is the single source of truth for the orchestrator step input
contract, the per-step result envelope, and structured logging setup. Every
thin ``lambda_handler`` in this package parses the Step Functions state input
with :func:`parse_step_input`, delegates to a unit-testable business function,
and wraps the outcome with :func:`build_step_result`.

The DynamoDB data model (``docs/data-model.md``) is unchanged by the
orchestrator. This scaffold (Task 2) defines the step seams and returns
structured stub results; later tasks replace the stub bodies with real
generation, quiesce, brief-regen, and baseline-priming logic.

Satisfies: Requirements 1.1, 1.2, 1.5 (orchestration contract) and
9.1, 9.2 (structured per-step logging and a per-step summary).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from aws_lambda_powertools import Logger

# Shared service name so every step Lambda logs under one searchable service.
SERVICE_NAME = "stayos-data-orchestrator"

# Module-level logger configured before any handler runs, per PYQUALITY-03.
logger = Logger(service=SERVICE_NAME)

# The two supported orchestration modes (design "Components and Interfaces").
MODE_SEED = "seed"
MODE_ROLL_FORWARD = "roll-forward"
VALID_MODES = (MODE_SEED, MODE_ROLL_FORWARD)

# Per-step status values used in the step-result envelope and execution summary.
STATUS_OK = "ok"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"


class OrchestratorInputError(ValueError):
    """Raised when a step receives an input that violates the step contract.

    Using a domain-specific exception (per PYQUALITY-02) lets the state
    machine distinguish a malformed-input failure from a downstream service
    failure without string-matching on generic ``ValueError`` messages.
    """


@dataclass
class StepInput:
    """Parsed, validated input shared by every orchestrator step.

    Attributes:
        mode: Orchestration mode, one of :data:`VALID_MODES`. ``seed`` fans
            out over all pilot properties; ``roll-forward`` scopes to one
            ``property_id`` (Requirements 1.3, 1.4).
        property_id: The scoped property for ``roll-forward``; ``None`` for a
            fan-out ``seed`` request.
        reference_date: The ISO ``YYYY-MM-DD`` date the generators anchor to.
            Defaults to UTC today when absent (mirrors the Task 1 resolver).
    """

    mode: str
    property_id: Optional[str]
    reference_date: str

    def to_context(self) -> Dict[str, Any]:
        """Return the logging context threaded through every step log line.

        Returns:
            A dict with ``propertyId`` and ``referenceDate`` keys (Requirement
            9.1). ``propertyId`` is included even when ``None`` so log queries
            can filter on its presence.
        """
        return {"propertyId": self.property_id, "referenceDate": self.reference_date}


@dataclass
class StepResult:
    """Structured result envelope returned by every orchestrator step.

    The state machine passes this envelope from one step to the next, so the
    per-execution summary (Requirement 9.2) can report success or failure of
    each step, and a failing step is recorded rather than masked (Requirement
    9.3).

    Attributes:
        step: The step name (e.g. ``"Generate"``).
        status: One of :data:`STATUS_OK`, :data:`STATUS_SKIPPED`,
            :data:`STATUS_FAILED`.
        mode: The orchestration mode this step ran under.
        property_id: The scoped property, if any.
        reference_date: The reference date this step anchored to.
        summary: A short human-readable summary of what the step did.
        details: Step-specific structured detail (e.g. per-table item counts).
    """

    step: str
    status: str
    mode: str
    property_id: Optional[str]
    reference_date: str
    summary: str
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the result for the Step Functions state payload.

        Returns:
            A JSON-serializable dict with camelCase keys matching the state
            machine's expected shape.
        """
        return {
            "step": self.step,
            "status": self.status,
            "mode": self.mode,
            "propertyId": self.property_id,
            "referenceDate": self.reference_date,
            "summary": self.summary,
            "details": self.details,
        }


def resolve_reference_date(raw: Optional[str]) -> str:
    """Resolve an ISO ``YYYY-MM-DD`` reference date, defaulting to UTC today.

    Args:
        raw: A candidate reference date string, or ``None``/empty to default.

    Returns:
        A validated ISO ``YYYY-MM-DD`` date string.

    Raises:
        OrchestratorInputError: If ``raw`` is present but not a valid ISO date.
    """
    if not raw:
        return datetime.now(tz=timezone.utc).date().isoformat()
    try:
        # date.fromisoformat rejects anything that is not a strict YYYY-MM-DD.
        return date.fromisoformat(raw).isoformat()
    except (ValueError, TypeError) as exc:
        raise OrchestratorInputError(
            f"referenceDate must be ISO YYYY-MM-DD, got {raw!r}"
        ) from exc


def parse_step_input(event: Dict[str, Any]) -> StepInput:
    """Parse and validate a step's Step Functions state input.

    The orchestrator threads a growing state document between steps. Each step
    reads the same top-level ``{mode, propertyId, referenceDate}`` contract;
    ``parse_step_input`` tolerates the extra accumulated keys (e.g. prior step
    results) and extracts only the contract fields.

    Args:
        event: The raw state input dict passed to the step Lambda.

    Returns:
        A validated :class:`StepInput`.

    Raises:
        OrchestratorInputError: If ``mode`` is missing/invalid, or if a
            ``roll-forward`` request omits ``propertyId``.
    """
    if not isinstance(event, dict):
        raise OrchestratorInputError(f"step input must be an object, got {type(event).__name__}")

    mode = event.get("mode")
    if mode not in VALID_MODES:
        raise OrchestratorInputError(
            f"mode must be one of {VALID_MODES}, got {mode!r}"
        )

    property_id = event.get("propertyId")
    if mode == MODE_ROLL_FORWARD and not property_id:
        # roll-forward is always scoped to exactly one property (Requirement 1.4).
        raise OrchestratorInputError("roll-forward requires a non-empty propertyId")

    reference_date = resolve_reference_date(event.get("referenceDate"))

    return StepInput(mode=mode, property_id=property_id, reference_date=reference_date)


def build_step_result(
    step: str,
    step_input: StepInput,
    summary: str,
    details: Optional[Dict[str, Any]] = None,
    status: str = STATUS_OK,
) -> Dict[str, Any]:
    """Build a serialized step-result envelope and emit its summary log.

    This centralizes the per-step summary log line (Requirement 9.2) so every
    step reports consistently with ``propertyId`` and ``referenceDate`` context
    (Requirement 9.1).

    Args:
        step: The step name.
        step_input: The parsed step input for mode/property/date context.
        summary: A short human-readable summary of the step outcome.
        details: Optional step-specific structured detail.
        status: The step status; defaults to :data:`STATUS_OK`.

    Returns:
        The serialized step-result dict for the state payload.
    """
    result = StepResult(
        step=step,
        status=status,
        mode=step_input.mode,
        property_id=step_input.property_id,
        reference_date=step_input.reference_date,
        summary=summary,
        details=details or {},
    )
    logger.info(
        "step summary",
        extra={
            "step": step,
            "status": status,
            "mode": step_input.mode,
            **step_input.to_context(),
            "summary": summary,
        },
    )
    return result.to_dict()


def resolve_target_properties(step_input: StepInput, pilot_property_ids: List[str]) -> List[str]:
    """Resolve which properties a step operates on for the given mode.

    Args:
        step_input: The parsed step input.
        pilot_property_ids: The full list of pilot property identifiers.

    Returns:
        For ``seed``, the full pilot list (fan-out, Requirement 1.3). For
        ``roll-forward``, a single-element list with the scoped property
        (Requirement 1.4).
    """
    if step_input.mode == MODE_SEED:
        return list(pilot_property_ids)
    # roll-forward: property_id is guaranteed non-empty by parse_step_input.
    return [step_input.property_id] if step_input.property_id else []
