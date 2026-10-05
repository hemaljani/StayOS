"""End-to-end (in-process) tests for the orchestrator step sequence.

Exercise :mod:`local_runner`, which mirrors the ``stayos-data-orchestrator``
state-machine sequence in-process against the Task 2 stubs. This is the
scaffold verification that "start-execution runs end-to-end with stubs" and
that the ``Catch`` guarantee always Un-Quiesces before failing (Requirement
5.5) and records the failing step rather than masking it (Requirement 9.3).
"""

from __future__ import annotations

import local_runner
import quiesce_handler
from orchestrator_common import (
    MODE_ROLL_FORWARD,
    MODE_SEED,
    STATUS_FAILED,
    STATUS_OK,
    OrchestratorInputError,
)

EXPECTED_ORDER = [
    "Quiesce",
    "Generate",
    "Reconcile",
    "UnQuiesce",
    "RegenerateBrief",
    "PrimeBaseline",
]


class TestHappyPath:
    """A valid input runs all six steps in order and reports success."""

    def test_roll_forward_runs_all_steps_in_order(self, mock_dataset_stack) -> None:
        # Generate/Reconcile now run the real generators, so exercise the full
        # sequence against moto-backed tables. Use the reloaded local_runner so
        # its handler references route through the moto-bound clients.
        import local_runner as runner

        summary = runner.run_execution(
            {"mode": MODE_ROLL_FORWARD, "propertyId": "ALOHA-CHI-001", "referenceDate": "2026-08-17"}
        )
        assert summary["status"] == STATUS_OK
        assert summary["executedSteps"] == EXPECTED_ORDER
        assert [r["step"] for r in summary["stepResults"]] == EXPECTED_ORDER
        assert summary["propertyId"] == "ALOHA-CHI-001"
        assert summary["referenceDate"] == "2026-08-17"

    def test_seed_mode_runs_end_to_end(self, mock_dataset_stack) -> None:
        import local_runner as runner

        summary = runner.run_execution({"mode": MODE_SEED})
        assert summary["status"] == STATUS_OK
        assert summary["executedSteps"] == EXPECTED_ORDER


class TestBadInputFailsFast:
    """A malformed contract fails before any step (nothing to un-quiesce)."""

    def test_missing_property_on_roll_forward(self) -> None:
        try:
            local_runner.run_execution({"mode": MODE_ROLL_FORWARD})
        except OrchestratorInputError:
            return
        raise AssertionError("expected OrchestratorInputError for missing propertyId")


class TestCatchAlwaysUnQuiesces:
    """On a mid-run failure the Catch path runs UnQuiesce before failing."""

    def test_generate_failure_triggers_unquiesce(self, monkeypatch) -> None:
        calls: list[str] = []

        real_unquiesce = local_runner.unquiesce_handler.lambda_handler

        def _tracking_unquiesce(event, context):
            calls.append("unquiesce")
            return real_unquiesce(event, context)

        def _boom(event, context):
            raise RuntimeError("generate exploded")

        monkeypatch.setattr(local_runner.generate_handler, "lambda_handler", _boom)
        monkeypatch.setattr(
            local_runner.unquiesce_handler, "lambda_handler", _tracking_unquiesce
        )

        summary = local_runner.run_execution(
            {"mode": MODE_ROLL_FORWARD, "propertyId": "ALOHA-CHI-001"}
        )

        assert summary["status"] == STATUS_FAILED
        assert summary["failedStep"] == "Generate"
        assert "generate exploded" in summary["reason"]
        # The Catch path must have run UnQuiesce so PULSE is not left suppressed.
        assert calls == ["unquiesce"]
        # Only the completed steps (Quiesce) are recorded before the failure.
        assert summary["executedSteps"] == ["Quiesce"]
