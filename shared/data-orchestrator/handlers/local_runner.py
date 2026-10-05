"""Local end-to-end stub runner for the StayOS Unified Data Orchestrator.

Mirrors the ``stayos-data-orchestrator`` Step Functions sequence
(``Quiesce -> Generate -> Reconcile -> UnQuiesce -> RegenerateBrief ->
PrimeBaseline``) in-process, including the ``Catch`` guarantee that
``UnQuiesce`` always runs before the workflow fails (Requirement 5.5).

Its purpose is to let ``start-execution`` be exercised end to end against the
Task 2 stubs without deploying, and to give the unit tests a single place that
asserts the ordering + Catch contract. It is NOT a Lambda handler and is not
deployed; the real orchestration is the CloudFormation-defined state machine.

Satisfies (scaffold verification): "start-execution runs end-to-end with stubs".
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List

from aws_lambda_powertools import Logger

from orchestrator_common import (
    SERVICE_NAME,
    STATUS_FAILED,
    STATUS_OK,
    parse_step_input,
)
import generate_handler
import prime_baseline_handler
import quiesce_handler
import reconcile_handler
import regenerate_brief_handler
import unquiesce_handler

logger = Logger(service=SERVICE_NAME)

# Ordered happy-path steps, mirroring the state machine definition. Each entry
# is (step name, handler module). The handler is resolved as
# ``module.lambda_handler`` at call time (not bound here at import) so a patched
# or reloaded handler is always the one invoked. UnQuiesce is intentionally
# listed here AND used as the Catch target below.
_HAPPY_PATH: List[tuple[str, Any]] = [
    ("Quiesce", quiesce_handler),
    ("Generate", generate_handler),
    ("Reconcile", reconcile_handler),
    ("UnQuiesce", unquiesce_handler),
    ("RegenerateBrief", regenerate_brief_handler),
    ("PrimeBaseline", prime_baseline_handler),
]

# Steps whose failure must NOT fail the whole execution, mirroring the state
# machine definition. RegenerateBrief has a Catch that transitions to
# PrimeBaseline (a brief failure never rolls back written data - Requirement
# 4.3), so a raise here is caught, recorded, and the run continues. Every other
# post-Quiesce step routes its Catch to CatchUnQuiesce -> Fail, so a failure is
# terminal (after UnQuiesce runs).
_CONTINUE_ON_FAILURE_STEPS: frozenset[str] = frozenset({"RegenerateBrief"})


def _local_context() -> Any:
    """Build a minimal Lambda-context stand-in for in-process step calls.

    The thin handlers are wrapped with Powertools ``inject_lambda_context``,
    which reads a handful of context attributes. When running the sequence
    in-process (not on Lambda) we supply a lightweight object exposing those
    attributes so the decorator does not fail.

    Returns:
        A ``SimpleNamespace`` with the attributes Powertools reads.
    """
    return SimpleNamespace(
        function_name="stayos-data-local-runner",
        memory_limit_in_mb=256,
        invoked_function_arn="arn:aws:lambda:us-east-1:000000000000:function:local-runner",
        aws_request_id="local-runner",
        log_stream_name="local-runner",
    )


def build_execution_summary(
    step_input: Any,
    step_results: List[Dict[str, Any]],
    executed_steps: List[str],
    overall_status: str,
    failed_step: str = "",
    reason: str = "",
) -> Dict[str, Any]:
    """Aggregate per-step outcomes into one structured per-execution summary.

    Requirement 9.2 asks for a per-execution summary indicating the success or
    failure of each step; Requirement 9.3 requires a failing step to be recorded
    and not masked as success. This builds that summary from the step-result
    envelopes already threaded through the run (each carries its own ``step`` and
    ``status``) and emits it as a single structured log line with ``propertyId``
    and ``referenceDate`` context (Requirement 9.1), then returns it.

    Args:
        step_input: The parsed :class:`~orchestrator_common.StepInput`.
        step_results: The ordered per-step result envelopes collected so far.
        executed_steps: The ordered names of steps that ran.
        overall_status: The overall execution status
            (:data:`~orchestrator_common.STATUS_OK` or
            :data:`~orchestrator_common.STATUS_FAILED`).
        failed_step: The name of the terminal failing step, if any.
        reason: The failure reason, if any.

    Returns:
        The per-execution summary dict (also emitted as a structured log).
    """
    # Per-step status roll-up (Requirement 9.2): a step is recorded even when it
    # reported a non-ok status (e.g. RegenerateBrief brief failure), so a failure
    # is never masked as success (Requirement 9.3).
    step_statuses = [
        {"step": result.get("step"), "status": result.get("status")}
        for result in step_results
    ]
    degraded_steps = [
        entry["step"] for entry in step_statuses if entry["status"] != STATUS_OK
    ]

    summary: Dict[str, Any] = {
        "status": overall_status,
        "mode": step_input.mode,
        "propertyId": step_input.property_id,
        "referenceDate": step_input.reference_date,
        "executedSteps": executed_steps,
        "stepStatuses": step_statuses,
        "degradedSteps": degraded_steps,
        "stepResults": step_results,
    }
    if failed_step:
        summary["failedStep"] = failed_step
    if reason:
        summary["reason"] = reason

    logger.info(
        "execution summary",
        extra={
            "status": overall_status,
            "executedSteps": executed_steps,
            "stepStatuses": step_statuses,
            "degradedSteps": degraded_steps,
            "failedStep": failed_step or None,
            **step_input.to_context(),
        },
    )
    return summary


def _run_catch_unquiesce(
    step_name: str, step_input: Any, execution_input: Dict[str, Any], lambda_context: Any
) -> None:
    """Run the Catch UnQuiesce so PULSE is never left suppressed on failure.

    Mirrors the state machine's ``CatchUnQuiesce`` state (Requirement 5.5).
    Skipped when the failing step is ``UnQuiesce`` itself (it already retries).

    Args:
        step_name: The step that failed.
        step_input: The parsed step input (for log context).
        execution_input: The original execution input to re-pass to UnQuiesce.
        lambda_context: The local Lambda-context stand-in.
    """
    if step_name == "UnQuiesce":
        return
    try:
        unquiesce_handler.lambda_handler(dict(execution_input), lambda_context)
    except Exception:  # noqa: BLE001 - Catch path must not itself raise
        logger.critical(
            "Catch UnQuiesce also failed - PULSE may remain suppressed",
            extra={"failedStep": step_name, **step_input.to_context()},
        )


def run_execution(execution_input: Dict[str, Any]) -> Dict[str, Any]:
    """Run the orchestrator step sequence in-process against the handlers.

    Faithfully mirrors the ``stayos-data-orchestrator`` state machine's Catch
    topology:

    * Quiesce / Generate / Reconcile / UnQuiesce / PrimeBaseline failures are
      terminal: the Catch path runs UnQuiesce, then the execution fails with the
      failing step/reason recorded (Requirements 5.5, 9.3).
    * RegenerateBrief is *continue-on-failure*: its state-machine Catch
      transitions to PrimeBaseline, so a raise (or a ``failed`` status envelope)
      is recorded and the run proceeds to PrimeBaseline without rolling back
      written operational data (Requirement 4.3). This is why a missing brief
      configuration degrades the run rather than aborting it.

    Args:
        execution_input: The ``StartExecution`` input carrying ``{mode,
            propertyId, referenceDate}``.

    Returns:
        A per-execution summary (Requirement 9.2) with an ordered list of step
        results, a per-step status roll-up, and an overall status.
    """
    # Validate the top-level contract once up front; a bad input fails fast
    # before any step (and before any quiesce), so nothing needs un-quiescing.
    step_input = parse_step_input(execution_input)

    step_results: List[Dict[str, Any]] = []
    executed_steps: List[str] = []
    lambda_context = _local_context()

    for step_name, handler_module in _HAPPY_PATH:
        try:
            # Each step reads the same contract from the original input; the
            # accumulated results ride alongside for observability parity with
            # the state machine's growing state document. Resolve the handler at
            # call time so a patched/reloaded module is honored.
            state = dict(execution_input)
            state["priorSteps"] = step_results
            result = handler_module.lambda_handler(state, lambda_context)
            step_results.append(result)
            executed_steps.append(step_name)
        except Exception as exc:  # noqa: BLE001 - top-level orchestrator boundary
            if step_name in _CONTINUE_ON_FAILURE_STEPS:
                # Mirror the state machine Catch -> next-step: record the failed
                # step and continue (Requirement 4.3). Never roll back data.
                logger.error(
                    "step failed but is continue-on-failure - recording and proceeding",
                    extra={
                        "failedStep": step_name,
                        "reason": str(exc),
                        **step_input.to_context(),
                    },
                )
                step_results.append(
                    {
                        "step": step_name,
                        "status": STATUS_FAILED,
                        "propertyId": step_input.property_id,
                        "referenceDate": step_input.reference_date,
                        "summary": f"{step_name} failed; continued without rollback",
                        "details": {"reason": str(exc)},
                    }
                )
                executed_steps.append(step_name)
                continue

            logger.error(
                "step failed - running Catch UnQuiesce before failing",
                extra={
                    "failedStep": step_name,
                    "reason": str(exc),
                    **step_input.to_context(),
                },
            )
            _run_catch_unquiesce(step_name, step_input, execution_input, lambda_context)
            return build_execution_summary(
                step_input=step_input,
                step_results=step_results,
                executed_steps=executed_steps,
                overall_status=STATUS_FAILED,
                failed_step=step_name,
                reason=str(exc),
            )

    return build_execution_summary(
        step_input=step_input,
        step_results=step_results,
        executed_steps=executed_steps,
        overall_status=STATUS_OK,
    )
