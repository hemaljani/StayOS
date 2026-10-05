"""Unit tests for the per-property concurrency guard (Requirement 1.6).

Prove that a second roll-forward start for the SAME ``propertyId`` on the SAME
reference-date bucket is skipped (the deterministic execution name collides with
``ExecutionAlreadyExists``, which is caught, logged, and returned as a skip
without raising), while a different property or a different date proceeds with a
fresh start.

The Step Functions client is a stateful fake that mirrors the real service's
execution-name uniqueness contract - no AWS call is made. It also mirrors the
boto3 convention that the typed exception is reachable at
``client.exceptions.ExecutionAlreadyExists`` (which the guard catches).
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

import concurrency_guard
import roll_forward_starter
from concurrency_guard import (
    DECISION_SKIPPED,
    DECISION_STARTED,
    build_execution_name,
    start_guarded_roll_forward,
)
from orchestrator_common import MODE_ROLL_FORWARD, MODE_SEED, StepInput

STATE_MACHINE_ARN = (
    "arn:aws:states:us-east-1:123456789012:stateMachine:stayos-data-orchestrator"
)


class _ExecutionAlreadyExists(Exception):
    """Stand-in for ``stepfunctions`` ``ExecutionAlreadyExists`` typed error."""


class _FakeExceptions:
    """Mirror the boto3 ``client.exceptions`` namespace the guard catches on."""

    ExecutionAlreadyExists = _ExecutionAlreadyExists


class FakeStepFunctionsClient:
    """Stateful fake enforcing execution-name uniqueness like the real service.

    Records every ``start_execution`` call and raises
    ``ExecutionAlreadyExists`` when a name is reused, exactly as Step Functions
    does within its retention window. No network access.
    """

    def __init__(self) -> None:
        self.exceptions = _FakeExceptions()
        self.started_names: List[str] = []
        self.calls: List[Dict[str, Any]] = []

    def start_execution(self, *, stateMachineArn: str, name: str, input: str) -> Dict[str, str]:  # noqa: N803 - boto3 kwargs
        """Start an execution unless the name was already used.

        Args:
            stateMachineArn: The target state-machine ARN.
            name: The (deterministic) execution name.
            input: The JSON execution input.

        Returns:
            A dict with a synthetic ``executionArn`` on a fresh start.

        Raises:
            _ExecutionAlreadyExists: If ``name`` was already started.
        """
        self.calls.append({"stateMachineArn": stateMachineArn, "name": name, "input": input})
        if name in self.started_names:
            raise self.exceptions.ExecutionAlreadyExists(name)
        self.started_names.append(name)
        return {"executionArn": f"{stateMachineArn}:exec:{name}"}


def _roll_forward_input(property_id: str, reference_date: str) -> StepInput:
    """Build a single-property roll-forward StepInput for the guard."""
    return StepInput(
        mode=MODE_ROLL_FORWARD, property_id=property_id, reference_date=reference_date
    )


class TestDeterministicName:
    """The execution name is a pure function of property + date bucket."""

    def test_same_property_same_date_yields_same_name(self) -> None:
        first = build_execution_name("ALOHA-CHI-001", "2026-08-17")
        second = build_execution_name("ALOHA-CHI-001", "2026-08-17")
        assert first == second

    def test_different_date_yields_different_name(self) -> None:
        assert build_execution_name("ALOHA-CHI-001", "2026-08-17") != build_execution_name(
            "ALOHA-CHI-001", "2026-08-18"
        )

    def test_name_is_sanitized_and_bounded(self) -> None:
        # An unusual propertyId is sanitized and truncated to <= 80 chars.
        name = build_execution_name("prop/with spaces:and*chars", "2026-08-17")
        assert len(name) <= 80
        assert all(char.isalnum() or char in "-_" for char in name)

    def test_long_property_preserves_date_suffix(self) -> None:
        # CR-7: a long propertyId must NOT cause the trailing YYYY-MM-DD to be
        # sliced off (which would collapse distinct days to one name and skip
        # every subsequent day as ExecutionAlreadyExists).
        long_property = "ALOHA-" + "X" * 120
        name = build_execution_name(long_property, "2026-08-17")
        assert len(name) <= 80
        assert name.endswith("2026-08-17")

    def test_long_property_distinct_dates_yield_distinct_names(self) -> None:
        # CR-7: two different days for the same long property still differ, so
        # the daily roll-forward is not wrongly suppressed.
        long_property = "ALOHA-" + "Y" * 120
        day1 = build_execution_name(long_property, "2026-08-17")
        day2 = build_execution_name(long_property, "2026-08-18")
        assert day1 != day2
        assert day1.endswith("2026-08-17")
        assert day2.endswith("2026-08-18")

    def test_long_distinct_properties_same_date_yield_distinct_names(self) -> None:
        # CR-7: hashing the property portion keeps distinct long properties
        # distinct (no collision) while preserving the date.
        name_a = build_execution_name("ALOHA-" + "A" * 120, "2026-08-17")
        name_b = build_execution_name("ALOHA-" + "B" * 120, "2026-08-17")
        assert name_a != name_b
        assert len(name_a) <= 80 and len(name_b) <= 80
        assert name_a.endswith("2026-08-17") and name_b.endswith("2026-08-17")

    def test_pathological_prefix_falls_back_to_bounded_hash(self) -> None:
        # CR-7 edge case: a prefix long enough to leave no room for the property
        # hash still returns a valid, deterministic, bounded name.
        huge_prefix = "P" * 90
        name = build_execution_name("ALOHA-CHI-001", "2026-08-17", prefix=huge_prefix)
        assert 1 <= len(name) <= 80
        # Deterministic: same inputs -> same name.
        again = build_execution_name("ALOHA-CHI-001", "2026-08-17", prefix=huge_prefix)
        assert name == again
        assert all(char.isalnum() or char in "-_" for char in name)


class TestOverlapSkipped:
    """A second start for the same property/day is skipped, not errored."""

    def test_second_start_same_property_same_date_is_skipped(self) -> None:
        client = FakeStepFunctionsClient()
        step_input = _roll_forward_input("ALOHA-CHI-001", "2026-08-17")

        first = start_guarded_roll_forward(client, STATE_MACHINE_ARN, step_input)
        second = start_guarded_roll_forward(client, STATE_MACHINE_ARN, step_input)

        assert first.decision == DECISION_STARTED
        assert first.started is True
        assert first.execution_arn is not None

        # The overlapping run is skipped - no raise - and recorded as such.
        assert second.decision == DECISION_SKIPPED
        assert second.started is False
        assert second.execution_arn is None
        # Both attempts used the identical deterministic name.
        assert first.execution_name == second.execution_name
        # Only one execution actually started despite two start attempts.
        assert client.started_names == [first.execution_name]


class TestNonOverlapProceeds:
    """A different property or a different date is a fresh start."""

    def test_different_property_same_date_proceeds(self) -> None:
        client = FakeStepFunctionsClient()
        first = start_guarded_roll_forward(
            client, STATE_MACHINE_ARN, _roll_forward_input("ALOHA-CHI-001", "2026-08-17")
        )
        other = start_guarded_roll_forward(
            client, STATE_MACHINE_ARN, _roll_forward_input("ALOHA-MIA-001", "2026-08-17")
        )
        assert first.decision == DECISION_STARTED
        assert other.decision == DECISION_STARTED
        assert first.execution_name != other.execution_name
        assert len(client.started_names) == 2

    def test_same_property_next_date_proceeds(self) -> None:
        client = FakeStepFunctionsClient()
        day_one = start_guarded_roll_forward(
            client, STATE_MACHINE_ARN, _roll_forward_input("ALOHA-CHI-001", "2026-08-17")
        )
        day_two = start_guarded_roll_forward(
            client, STATE_MACHINE_ARN, _roll_forward_input("ALOHA-CHI-001", "2026-08-18")
        )
        assert day_one.decision == DECISION_STARTED
        assert day_two.decision == DECISION_STARTED
        assert day_one.execution_name != day_two.execution_name
        assert len(client.started_names) == 2


class TestGuardRejectsNonRollForward:
    """The guard is only meaningful for a single-property roll-forward."""

    def test_seed_mode_is_rejected(self) -> None:
        client = FakeStepFunctionsClient()
        seed_input = StepInput(mode=MODE_SEED, property_id=None, reference_date="2026-08-17")
        with pytest.raises(ValueError):
            start_guarded_roll_forward(client, STATE_MACHINE_ARN, seed_input)


class TestStarterHandlerAppliesGuard:
    """The roll_forward_starter Lambda routes the schedule through the guard."""

    @pytest.fixture()
    def fake_client(self, monkeypatch: pytest.MonkeyPatch) -> FakeStepFunctionsClient:
        """Replace the starter's module-level SFN client + set the ARN env var."""
        client = FakeStepFunctionsClient()
        monkeypatch.setattr(roll_forward_starter, "_SFN_CLIENT", client)
        monkeypatch.setenv(roll_forward_starter.ENV_STATE_MACHINE_ARN, STATE_MACHINE_ARN)
        return client

    def test_schedule_event_starts_then_skips_on_repeat(
        self, fake_client: FakeStepFunctionsClient, lambda_context: Any
    ) -> None:
        event = {"propertyId": "ALOHA-TYO-001", "referenceDate": "2026-08-17"}

        first = roll_forward_starter.lambda_handler(dict(event), lambda_context)
        second = roll_forward_starter.lambda_handler(dict(event), lambda_context)

        assert first["decision"] == DECISION_STARTED
        assert first["propertyId"] == "ALOHA-TYO-001"
        # A second identical schedule fire (or manual retrigger) is skipped.
        assert second["decision"] == DECISION_SKIPPED
        assert first["executionName"] == second["executionName"]
        assert fake_client.started_names == [first["executionName"]]

    def test_missing_reference_date_defaults_and_still_guards(
        self, fake_client: FakeStepFunctionsClient, lambda_context: Any
    ) -> None:
        # No referenceDate -> resolver defaults to UTC today; two fires in the
        # same UTC day still collide and the second is skipped.
        event = {"propertyId": "ALOHA-MAD-001"}
        first = roll_forward_starter.lambda_handler(dict(event), lambda_context)
        second = roll_forward_starter.lambda_handler(dict(event), lambda_context)
        assert first["decision"] == DECISION_STARTED
        assert second["decision"] == DECISION_SKIPPED
