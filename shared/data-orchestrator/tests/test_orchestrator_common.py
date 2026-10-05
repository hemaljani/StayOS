"""Unit tests for the orchestrator shared step contract and helpers.

Covers :mod:`orchestrator_common`: input parsing/validation, reference-date
resolution, the step-result envelope, and target-property fan-out. These back
the orchestration contract (Requirements 1.1, 1.3, 1.4, 1.5) and structured
per-step summary (Requirements 9.1, 9.2).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from orchestrator_common import (
    MODE_ROLL_FORWARD,
    MODE_SEED,
    STATUS_OK,
    OrchestratorInputError,
    build_step_result,
    parse_step_input,
    resolve_reference_date,
    resolve_target_properties,
)

PILOT = ["ALOHA-CHI-001", "ALOHA-MIA-001", "ALOHA-TYO-001", "ALOHA-MAD-001", "ALOHA-BOM-001"]


class TestResolveReferenceDate:
    """resolve_reference_date normalizes/validates the anchor date."""

    def test_none_defaults_to_utc_today(self) -> None:
        expected = datetime.now(tz=timezone.utc).date().isoformat()
        assert resolve_reference_date(None) == expected

    def test_empty_string_defaults_to_utc_today(self) -> None:
        expected = datetime.now(tz=timezone.utc).date().isoformat()
        assert resolve_reference_date("") == expected

    def test_valid_iso_passes_through(self) -> None:
        assert resolve_reference_date("2026-08-17") == "2026-08-17"

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(OrchestratorInputError):
            resolve_reference_date("08/17/2026")

    def test_non_date_raises(self) -> None:
        with pytest.raises(OrchestratorInputError):
            resolve_reference_date("not-a-date")


class TestParseStepInput:
    """parse_step_input enforces the {mode, propertyId, referenceDate} contract."""

    def test_seed_without_property_is_valid(self) -> None:
        parsed = parse_step_input({"mode": MODE_SEED, "referenceDate": "2026-08-17"})
        assert parsed.mode == MODE_SEED
        assert parsed.property_id is None
        assert parsed.reference_date == "2026-08-17"

    def test_roll_forward_requires_property(self) -> None:
        with pytest.raises(OrchestratorInputError):
            parse_step_input({"mode": MODE_ROLL_FORWARD, "referenceDate": "2026-08-17"})

    def test_roll_forward_with_property_is_valid(self) -> None:
        parsed = parse_step_input(
            {"mode": MODE_ROLL_FORWARD, "propertyId": "ALOHA-CHI-001"}
        )
        assert parsed.mode == MODE_ROLL_FORWARD
        assert parsed.property_id == "ALOHA-CHI-001"
        # Missing referenceDate defaults to UTC today.
        assert parsed.reference_date == datetime.now(tz=timezone.utc).date().isoformat()

    def test_invalid_mode_raises(self) -> None:
        with pytest.raises(OrchestratorInputError):
            parse_step_input({"mode": "wipe", "propertyId": "ALOHA-CHI-001"})

    def test_missing_mode_raises(self) -> None:
        with pytest.raises(OrchestratorInputError):
            parse_step_input({"propertyId": "ALOHA-CHI-001"})

    def test_non_dict_event_raises(self) -> None:
        with pytest.raises(OrchestratorInputError):
            parse_step_input("not-a-dict")  # type: ignore[arg-type]

    def test_ignores_extra_accumulated_state(self) -> None:
        # Steps thread a growing state document; extra keys must be tolerated.
        parsed = parse_step_input(
            {
                "mode": MODE_ROLL_FORWARD,
                "propertyId": "ALOHA-MIA-001",
                "referenceDate": "2026-08-17",
                "quiesceResult": {"status": "ok"},
                "priorSteps": [{"step": "Quiesce"}],
            }
        )
        assert parsed.property_id == "ALOHA-MIA-001"


class TestResolveTargetProperties:
    """resolve_target_properties fans out for seed and scopes for roll-forward."""

    def test_seed_fans_out_over_estate(self) -> None:
        parsed = parse_step_input({"mode": MODE_SEED})
        assert resolve_target_properties(parsed, PILOT) == PILOT

    def test_roll_forward_scopes_to_single_property(self) -> None:
        parsed = parse_step_input(
            {"mode": MODE_ROLL_FORWARD, "propertyId": "ALOHA-TYO-001"}
        )
        assert resolve_target_properties(parsed, PILOT) == ["ALOHA-TYO-001"]


class TestBuildStepResult:
    """build_step_result produces the serialized envelope with context."""

    def test_envelope_shape_and_context(self) -> None:
        parsed = parse_step_input(
            {"mode": MODE_ROLL_FORWARD, "propertyId": "ALOHA-CHI-001", "referenceDate": "2026-08-17"}
        )
        result = build_step_result(
            step="Generate",
            step_input=parsed,
            summary="did the thing",
            details={"perTableCounts": {"rooms": 1}},
        )
        assert result["step"] == "Generate"
        assert result["status"] == STATUS_OK
        assert result["mode"] == MODE_ROLL_FORWARD
        assert result["propertyId"] == "ALOHA-CHI-001"
        assert result["referenceDate"] == "2026-08-17"
        assert result["summary"] == "did the thing"
        assert result["details"] == {"perTableCounts": {"rooms": 1}}
