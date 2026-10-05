"""Unit tests for LUMI Data Validator.

Tests the AI narrative cross-check logic that prevents hallucinated
statistics from reaching GMs. Validates number extraction, tolerance
matching, and discrepancy reporting.
"""

from typing import Any, Dict

import pytest

from data_validator import validate_narrative


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_source_data() -> Dict[str, Any]:
    """Source data with known KPI values for validation testing.

    Mirrors the structure returned by data_puller.pull_property_data
    with occupancy, ADR, RevPAR, VIP count, and room-related numbers.
    """
    return {
        "property": {
            "propertyId": "ALOHA-CHI-001",
            "propertyName": "Aloha Grand Chicago",
            "totalRooms": 368,
        },
        "dailyKPIs": {
            "date": "2026-08-03",
            "occupancy": {
                "current": 87,
                "vsLastWeek": 4.2,
                "forecast3pm": 91,
            },
            "adr": {
                "current": 248,
                "vsLastWeek": 12,
                "pacePctOfBudget": 103,
            },
            "revPAR": {
                "current": 216,
                "vsYOY": 7.1,
                "budget": 202,
            },
            "arrivals": {
                "total": 142,
                "vipCount": 7,
                "ambassadorCount": 3,
                "titaniumCount": 4,
            },
            "departures": {
                "total": 118,
            },
            "confirmedReservations": 374,
            "availableRooms": 368,
        },
        "actionItems": [
            {
                "type": "OVERBOOKING_RISK",
                "data": {"confirmedCount": 374, "availableRooms": 368, "overage": 6},
            },
            {
                "type": "UPSELL_OPPORTUNITY",
                "data": {"eligibleCount": 28, "avgUpsellValuePerNight": 85},
            },
        ],
        "vipArrivals": [
            {"totalStays": 47, "roomNumber": "2401"},
            {"totalStays": 22, "roomNumber": "1802"},
        ],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestValidateNarrative:
    """Tests for the validate_narrative function."""

    def test_valid_narrative_passes(self, sample_source_data: Dict[str, Any]) -> None:
        """Narrative with all numbers matching source data passes validation."""
        # Narrative uses numbers from the source data (87, 248, 216, 7)
        narrative = (
            "Good morning. Your property is at 87% occupancy today, "
            "with an ADR of 248 dollars. RevPAR stands at 216, and "
            "you have 7 VIP arrivals including 3 Ambassador members."
        )

        is_valid, discrepancies = validate_narrative(narrative, sample_source_data)

        assert is_valid is True
        assert discrepancies == []

    def test_narrative_with_wrong_number_fails(
        self, sample_source_data: Dict[str, Any]
    ) -> None:
        """Narrative containing a number not in source data fails validation."""
        # 95 does not appear anywhere in the source data
        narrative = (
            "Your property is at 95% occupancy today, "
            "with an ADR of 248 dollars."
        )

        is_valid, discrepancies = validate_narrative(narrative, sample_source_data)

        assert is_valid is False
        assert len(discrepancies) >= 1
        assert any("95" in d for d in discrepancies)

    def test_tolerance_allows_rounding(
        self, sample_source_data: Dict[str, Any]
    ) -> None:
        """Numbers within +/- 1 tolerance match source data (rounding).

        Source has occupancy 87.4-equivalent scenario: the validator should
        accept 87 when the source is 87, and also accept values within +/- 1
        tolerance. Here we test that 87 matches source 87 (exact), and also
        that the tolerance mechanism works for decimals (4.2 in source).
        """
        # 4 is within tolerance of source value 4.2
        narrative = (
            "Occupancy is 87 percent, up 4 points from last week. "
            "ADR is 248 dollars."
        )

        is_valid, discrepancies = validate_narrative(narrative, sample_source_data)

        assert is_valid is True
        assert discrepancies == []

    def test_empty_narrative_passes(
        self, sample_source_data: Dict[str, Any]
    ) -> None:
        """Empty narrative has no numbers to validate, so it passes."""
        is_valid, discrepancies = validate_narrative("", sample_source_data)

        assert is_valid is True
        assert discrepancies == []

    def test_narrative_with_only_text_passes(
        self, sample_source_data: Dict[str, Any]
    ) -> None:
        """Narrative with no numbers at all passes validation."""
        narrative = "Good morning. Everything looks great today."

        is_valid, discrepancies = validate_narrative(narrative, sample_source_data)

        assert is_valid is True
        assert discrepancies == []

    def test_decimal_numbers_validated(
        self, sample_source_data: Dict[str, Any]
    ) -> None:
        """Decimal numbers in narrative are checked against source decimals."""
        # 7.1 is the vsYOY value in source data
        narrative = "RevPAR grew 7.1 percent year over year."

        is_valid, discrepancies = validate_narrative(narrative, sample_source_data)

        assert is_valid is True
        assert discrepancies == []

    def test_multiple_discrepancies_reported(
        self, sample_source_data: Dict[str, Any]
    ) -> None:
        """All discrepancies are reported when multiple numbers are wrong."""
        # 95 and 500 do not match any source data
        narrative = "Occupancy is 95 percent with ADR of 500 dollars."

        is_valid, discrepancies = validate_narrative(narrative, sample_source_data)

        assert is_valid is False
        assert len(discrepancies) == 2
